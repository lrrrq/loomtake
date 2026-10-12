from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from PIL import Image

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.asset_center import AssetCenterService
from mvp_v2.asset_center_tools import (
    AssetCenterToolService,
    AssetCenterToolServices,
    AssetPackageResolver,
    AssetToolRecoveryRequired,
    AssetUploadStore,
)
from mvp_v2.asset_package import create_asset_package
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


class AssetCenterToolFixture:
    def __init__(self, root: Path, *, checkpoint=None):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.data_root = root / "runtime"
        self.uploads = AssetUploadStore(self.data_root)
        source = root / "anonymous-source"
        source.mkdir()
        _write_source_workbook(source / "M+发布会类通用脚本库.xlsx")
        manifest = create_asset_package(
            root / "package",
            package_id="launch-tool-package",
            source_root=source,
            category_id="launch",
        )
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        payload["package_version"] = 3
        manifest.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        self.center = AssetCenterService(manifest, storage=self.uploads.storage)
        self.store = WorkspaceStore(self.data_root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="anonymous-asset-owner-token",
        )
        self.auth_token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.project_a = self.projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="匿名项目 A",
            category_id="launch",
            expected_workspace_version=0,
        )
        self.project_b = self.projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="匿名项目 B",
            category_id="launch",
            expected_workspace_version=1,
        )
        self.project_other_category = self.projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="匿名其他类别",
            category_id="exhibition",
            expected_workspace_version=2,
        )
        self.session_a = self.tasks.create_agent_session(
            self.auth_token, project_id=self.project_a["project_id"]
        )["agent_session_id"]
        self.session_b = self.tasks.create_agent_session(
            self.auth_token, project_id=self.project_b["project_id"]
        )["agent_session_id"]
        self.session_other = self.tasks.create_agent_session(
            self.auth_token, project_id=self.project_other_category["project_id"]
        )["agent_session_id"]
        self.confirmations = ActionConfirmationService(self.store, b"a" * 32)
        self.services = AssetCenterToolServices(
            store=self.store,
            projects=self.projects,
            tasks=self.tasks,
            confirmations=self.confirmations,
            idempotency=ToolIdempotencyService(self.store),
            uploads=self.uploads,
            packages=AssetPackageResolver([self.center]),
        )
        self.tools = AssetCenterToolService(self.services, checkpoint=checkpoint)
        self._counter = 0

    def freeze_image(self, name="anonymous.png", color="blue") -> dict:
        path = self.root / name
        path.write_bytes(_image_bytes(color))
        return self.tools.freeze_upload(path)

    def freeze_workbook(self, name="anonymous.xlsx") -> dict:
        path = self.root / name
        path.write_bytes(_workbook_bytes())
        return self.tools.freeze_upload(path)

    def create_parameters(self, refs, *, scope="project", key=None):
        self._counter += 1
        return {
            "package_id": "launch-tool-package",
            "expected_active_package_version": 3,
            "idempotency_key": key or f"asset-create-{self._counter:08d}",
            "category_id": "launch",
            "scope": scope,
            "file_refs": list(refs),
        }

    def transition_parameters(self, record, *, key, publish=False):
        payload = {
            "package_id": "launch-tool-package",
            "expected_active_package_version": 3,
            "idempotency_key": key,
            "import_id": record["import_id"],
            "expected_import_revision": record["revision"],
        }
        if publish:
            confirmation = self.tools.issue_confirmation(
                self.auth_token,
                agent_session_id=self.session_a,
                tool_name="publish_asset_import",
                parameters=payload,
            )
            payload["confirmation_token"] = confirmation["confirmation_token"]
        return payload

    def execute_create(self, parameters, *, session=None):
        agent_session_id = session or self.session_a
        confirmation = self.tools.issue_confirmation(
            self.auth_token,
            agent_session_id=agent_session_id,
            tool_name="create_asset_import",
            parameters=parameters,
        )
        return self.tools.create_asset_import(
            self.auth_token,
            agent_session_id=agent_session_id,
            parameters=parameters,
            confirmation_token=confirmation["confirmation_token"],
        )

    def execute_review(self, record, *, session=None, key="asset-review-00000001"):
        agent_session_id = session or self.session_a
        parameters = self.transition_parameters(record, key=key)
        confirmation = self.tools.issue_confirmation(
            self.auth_token,
            agent_session_id=agent_session_id,
            tool_name="review_asset_import",
            parameters=parameters,
        )
        return self.tools.review_asset_import(
            self.auth_token,
            agent_session_id=agent_session_id,
            parameters=parameters,
            confirmation_token=confirmation["confirmation_token"],
        )

    def execute_publish(self, record, *, key="asset-publish-00000001"):
        parameters = self.transition_parameters(record, key=key, publish=True)
        return self.tools.publish_asset_import(
            self.auth_token,
            agent_session_id=self.session_a,
            parameters=parameters,
        )


class AssetCenterToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = AssetCenterToolFixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_empty_runtime_asset_packages_keep_asset_service_constructible(self):
        runtime_root = self.root / "empty-runtime"
        service = AssetCenterToolService.from_runtime_config(
            confirmation_signing_key=b"a" * 32,
            config={
                "data_root": str(runtime_root),
                "asset_packages": {},
                "asset_package": "",
            },
        )
        self.assertEqual(service.services.packages._centers, ())
        with self.assertRaisesRegex(WorkspaceServiceError, "当前类别没有可用资产包"):
            service.services.packages.resolve_active("launch")

    def test_freezes_real_png_and_xlsx_as_content_addressed_logical_uploads(self):
        image = self.fixture.freeze_image("匿名图片.png")
        workbook = self.fixture.freeze_workbook("匿名数据.xlsx")

        self.assertEqual(image["kind"], "image")
        self.assertEqual(workbook["kind"], "workbook")
        self.assertEqual(
            self.fixture.uploads.storage.root,
            (self.fixture.data_root / "artifacts" / "assets").resolve(),
        )
        for item in (image, workbook):
            self.assertTrue(item["artifact_ref"].startswith(
                f"artifact://upload/{item['content_sha256']}/"
            ))
            self.assertEqual(item["storage_ref"], f"sha256://{item['content_sha256']}")
            self.assertNotIn(str(self.root), json.dumps(item, ensure_ascii=False))
            self.assertEqual(self.fixture.uploads.resolve(item["artifact_ref"]), item)

    def test_upload_tampering_is_rejected_before_confirmation_or_domain_write(self):
        image = self.fixture.freeze_image()
        object_path = self.fixture.uploads.storage.path_for(image["storage_ref"])
        object_path.write_bytes(b"tampered")
        parameters = self.fixture.create_parameters([image["artifact_ref"]])

        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self.fixture.tools.issue_confirmation(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_a,
                tool_name="create_asset_import",
                parameters=parameters,
            )
        with sqlite3.connect(self.fixture.center.catalog_path) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='asset_imports'"
            ).fetchone()
        self.assertIsNone(exists)

    def test_project_scope_binds_session_project_and_shared_scope_has_no_target(self):
        first = self.fixture.freeze_image("project.png", "green")
        project_result = self.fixture.execute_create(
            self.fixture.create_parameters([first["artifact_ref"]], scope="project")
        )["result"]
        second = self.fixture.freeze_image("shared.png", "yellow")
        shared_parameters = self.fixture.create_parameters(
            [second["artifact_ref"]], scope="shared"
        )
        shared_result = self.fixture.execute_create(shared_parameters)["result"]

        self.assertEqual(
            project_result["target_project_id"], self.fixture.project_a["project_id"]
        )
        self.assertEqual(shared_result["target_project_id"], "")
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self.fixture.tools.issue_confirmation(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_a,
                tool_name="create_asset_import",
                parameters={**shared_parameters, "project_id": self.fixture.project_a["project_id"]},
            )

    def test_project_a_import_cannot_be_reviewed_from_project_b_session(self):
        image = self.fixture.freeze_image("project-a-only.png", "purple")
        staged = self.fixture.execute_create(
            self.fixture.create_parameters([image["artifact_ref"]])
        )["result"]
        parameters = self.fixture.transition_parameters(
            staged, key="asset-review-project-b"
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.tools.issue_confirmation(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_b,
                tool_name="review_asset_import",
                parameters=parameters,
            )

    def test_create_review_publish_has_confirmation_idempotency_tasks_and_no_provider(self):
        image = self.fixture.freeze_image("controlled.png", "navy")
        workbook = self.fixture.freeze_workbook("controlled.xlsx")
        create_parameters = self.fixture.create_parameters(
            [image["artifact_ref"], workbook["artifact_ref"]]
        )
        confirmation = self.fixture.tools.issue_confirmation(
            self.fixture.auth_token,
            agent_session_id=self.fixture.session_a,
            tool_name="create_asset_import",
            parameters=create_parameters,
        )
        with patch("mvp_v2.image_understanding.generate_minimax_visual_analysis") as provider:
            created = self.fixture.tools.create_asset_import(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_a,
                parameters=create_parameters,
                confirmation_token=confirmation["confirmation_token"],
            )
            replayed = self.fixture.tools.create_asset_import(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_a,
                parameters=create_parameters,
                confirmation_token=confirmation["confirmation_token"],
            )
        provider.assert_not_called()
        self.assertTrue(replayed["replayed"])
        self.assertEqual(created["task_id"], replayed["task_id"])
        self.assertEqual(created["result"]["state"], "staged")
        self.assertEqual(created["result"]["machine_review_status"], "passed")

        reviewed = self.fixture.execute_review(created["result"])
        self.assertEqual(reviewed["result"]["state"], "reviewed")
        published = self.fixture.execute_publish(reviewed["result"])
        self.assertEqual(published["result"]["state"], "published")
        self.assertTrue(all(
            item["availability"] == "available" for item in published["result"]["items"]
        ))
        with self.fixture.store.connection() as connection:
            tasks = connection.execute(
                "SELECT task_type, status FROM tasks ORDER BY created_at, task_id"
            ).fetchall()
            confirmations = connection.execute(
                "SELECT tool_name, status FROM action_confirmations ORDER BY created_at"
            ).fetchall()
            records = connection.execute(
                "SELECT tool_name, status FROM tool_idempotency_records ORDER BY created_at"
            ).fetchall()
        self.assertEqual([row[0] for row in tasks], [
            "create_asset_import", "review_asset_import", "publish_asset_import"
        ])
        self.assertTrue(all(row[1] == "completed" for row in tasks))
        self.assertTrue(all(row[1] == "consumed" for row in confirmations))
        self.assertTrue(all(row[1] == "completed" for row in records))

    def test_machine_pass_cannot_publish_without_review(self):
        image = self.fixture.freeze_image("needs-review.png", "orange")
        staged = self.fixture.execute_create(
            self.fixture.create_parameters([image["artifact_ref"]])
        )["result"]
        publish_parameters = self.fixture.transition_parameters(
            staged, key="asset-publish-too-early", publish=True
        )
        failed = self.fixture.tools.publish_asset_import(
            self.fixture.auth_token,
            agent_session_id=self.fixture.session_a,
            parameters=publish_parameters,
        )

        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "REVIEW_REQUIRED")
        replayed = self.fixture.tools.publish_asset_import(
            self.fixture.auth_token,
            agent_session_id=self.fixture.session_a,
            parameters=publish_parameters,
        )
        self.assertTrue(replayed["replayed"])
        self.assertEqual(replayed["task_id"], failed["task_id"])
        self.assertEqual(self.fixture.center.get_import(staged["import_id"])["state"], "staged")

    def test_same_key_parameter_change_conflicts_and_does_not_duplicate_domain_record(self):
        first = self.fixture.freeze_image("first.png", "red")
        second = self.fixture.freeze_image("second.png", "black")
        key = "asset-create-same-key"
        original = self.fixture.create_parameters([first["artifact_ref"]], key=key)
        self.fixture.execute_create(original)
        changed = self.fixture.create_parameters([second["artifact_ref"]], key=key)
        confirmation = self.fixture.tools.issue_confirmation(
            self.fixture.auth_token,
            agent_session_id=self.fixture.session_a,
            tool_name="create_asset_import",
            parameters=changed,
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
            self.fixture.tools.create_asset_import(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_a,
                parameters=changed,
                confirmation_token=confirmation["confirmation_token"],
            )
        with sqlite3.connect(self.fixture.center.catalog_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM asset_imports").fetchone()[0]
        self.assertEqual(count, 1)

    def test_crash_after_domain_write_blocks_replay_for_manual_reconciliation(self):
        fired = {"value": False}

        def checkpoint(name):
            if name == "after_domain_write" and not fired["value"]:
                fired["value"] = True
                raise RuntimeError("simulated crash")

        fixture = AssetCenterToolFixture(self.root / "crash", checkpoint=checkpoint)
        image = fixture.freeze_image("crash.png", "teal")
        parameters = fixture.create_parameters([image["artifact_ref"]])
        confirmation = fixture.tools.issue_confirmation(
            fixture.auth_token,
            agent_session_id=fixture.session_a,
            tool_name="create_asset_import",
            parameters=parameters,
        )
        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            fixture.tools.create_asset_import(
                fixture.auth_token,
                agent_session_id=fixture.session_a,
                parameters=parameters,
                confirmation_token=confirmation["confirmation_token"],
            )
        with self.assertRaisesRegex(
            AssetToolRecoveryRequired, "RECOVERY_RECONCILIATION_REQUIRED"
        ):
            fixture.tools.create_asset_import(
                fixture.auth_token,
                agent_session_id=fixture.session_a,
                parameters=parameters,
                confirmation_token=confirmation["confirmation_token"],
            )
        with sqlite3.connect(fixture.center.catalog_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM asset_imports").fetchone()[0]
        self.assertEqual(count, 1)

    def test_permissions_and_category_are_checked_before_asset_center_calls(self):
        workspace = self.fixture.projects.get_workspace(
            self.fixture.auth_token, self.fixture.workspace_id
        )
        viewer = self.fixture.identity.create_actor(
            self.fixture.auth_token,
            workspace_id=self.fixture.workspace_id,
            project_id=self.fixture.project_a["project_id"],
            display_name="匿名查看者",
            auth_source="local",
            role="viewer",
            expected_workspace_version=workspace["workspace_version"],
        )
        viewer_auth = self.fixture.identity.issue_auth_session(
            actor_id=viewer["actor_id"], token="anonymous-viewer-token"
        )
        viewer_session = self.fixture.tasks.create_agent_session(
            viewer_auth["auth_token"], project_id=self.fixture.project_a["project_id"]
        )["agent_session_id"]
        image = self.fixture.freeze_image("permission.png", "silver")
        parameters = self.fixture.create_parameters([image["artifact_ref"]])

        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.tools.issue_confirmation(
                viewer_auth["auth_token"],
                agent_session_id=viewer_session,
                tool_name="create_asset_import",
                parameters=parameters,
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.tools.search_assets(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_other,
                parameters={"category_id": "launch", "query": "anything", "filters": {}},
            )

    def test_editor_cannot_review_or_publish_and_r3_requires_contract_token(self):
        image = self.fixture.freeze_image("role-boundary.png", "maroon")
        staged = self.fixture.execute_create(
            self.fixture.create_parameters([image["artifact_ref"]])
        )["result"]
        workspace = self.fixture.projects.get_workspace(
            self.fixture.auth_token, self.fixture.workspace_id
        )
        editor = self.fixture.identity.create_actor(
            self.fixture.auth_token,
            workspace_id=self.fixture.workspace_id,
            project_id=self.fixture.project_a["project_id"],
            display_name="匿名编辑者",
            auth_source="local",
            role="editor",
            expected_workspace_version=workspace["workspace_version"],
        )
        editor_auth = self.fixture.identity.issue_auth_session(
            actor_id=editor["actor_id"], token="anonymous-editor-token"
        )
        editor_session = self.fixture.tasks.create_agent_session(
            editor_auth["auth_token"], project_id=self.fixture.project_a["project_id"]
        )["agent_session_id"]
        review_parameters = self.fixture.transition_parameters(
            staged, key="asset-editor-review"
        )
        publish_parameters = self.fixture.transition_parameters(
            staged, key="asset-editor-publish"
        )

        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.tools.issue_confirmation(
                editor_auth["auth_token"],
                agent_session_id=editor_session,
                tool_name="review_asset_import",
                parameters=review_parameters,
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
            self.fixture.tools.issue_confirmation(
                editor_auth["auth_token"],
                agent_session_id=editor_session,
                tool_name="publish_asset_import",
                parameters=publish_parameters,
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "INVALID_INPUT"):
            self.fixture.tools.publish_asset_import(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_a,
                parameters=publish_parameters,
            )

    def test_package_version_and_import_revision_drift_are_rejected(self):
        image = self.fixture.freeze_image("drift.png", "olive")
        staged = self.fixture.execute_create(
            self.fixture.create_parameters([image["artifact_ref"]])
        )["result"]
        stale_package = self.fixture.transition_parameters(
            staged, key="asset-review-stale-package"
        )
        stale_package["expected_active_package_version"] = 2
        with self.assertRaisesRegex(WorkspaceServiceError, "PACKAGE_VERSION_CONFLICT"):
            self.fixture.tools.issue_confirmation(
                self.fixture.auth_token,
                agent_session_id=self.fixture.session_a,
                tool_name="review_asset_import",
                parameters=stale_package,
            )

        stale_revision = self.fixture.transition_parameters(
            staged, key="asset-review-stale-revision"
        )
        stale_revision["expected_import_revision"] += 1
        confirmation = self.fixture.tools.issue_confirmation(
            self.fixture.auth_token,
            agent_session_id=self.fixture.session_a,
            tool_name="review_asset_import",
            parameters=stale_revision,
        )
        failed = self.fixture.tools.review_asset_import(
            self.fixture.auth_token,
            agent_session_id=self.fixture.session_a,
            parameters=stale_revision,
            confirmation_token=confirmation["confirmation_token"],
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "PACKAGE_VERSION_CONFLICT")
        self.assertEqual(self.fixture.center.get_import(staged["import_id"])["state"], "staged")

    def test_search_returns_only_shared_and_current_project_published_without_paths(self):
        shared = self.fixture.freeze_image("shared-searchable.png", "white")
        shared_staged = self.fixture.execute_create(
            self.fixture.create_parameters([shared["artifact_ref"]], scope="shared")
        )["result"]
        shared_reviewed = self.fixture.execute_review(
            shared_staged, key="asset-review-shared-search"
        )["result"]
        self.fixture.execute_publish(shared_reviewed, key="asset-publish-shared-search")

        private = self.fixture.freeze_image("private-searchable.png", "gray")
        private_staged = self.fixture.execute_create(
            self.fixture.create_parameters([private["artifact_ref"]], scope="project")
        )["result"]
        private_reviewed = self.fixture.execute_review(
            private_staged, key="asset-review-private-search"
        )["result"]
        self.fixture.execute_publish(private_reviewed, key="asset-publish-private-search")

        result_a = self.fixture.tools.search_assets(
            self.fixture.auth_token,
            agent_session_id=self.fixture.session_a,
            parameters={
                "category_id": "launch",
                "query": "searchable",
                "filters": {"kind": "image", "limit": 20},
            },
        )
        result_b = self.fixture.tools.search_assets(
            self.fixture.auth_token,
            agent_session_id=self.fixture.session_b,
            parameters={
                "category_id": "launch",
                "query": "searchable",
                "filters": {"kind": "image", "limit": 20},
            },
        )
        titles_a = {item["title"] for item in result_a["results"]}
        titles_b = {item["title"] for item in result_b["results"]}
        self.assertEqual(titles_a, {"shared-searchable.png", "private-searchable.png"})
        self.assertEqual(titles_b, {"shared-searchable.png"})
        serialized = json.dumps([result_a, result_b], ensure_ascii=False, sort_keys=True)
        self.assertNotIn(str(self.root), serialized)
        for item in result_a["results"]:
            self.assertEqual(item["availability"], "available")
            self.assertEqual(item["source"]["package_version"], 3)


def _image_bytes(color: str) -> bytes:
    stream = BytesIO()
    Image.new("RGB", (40, 30), color).save(stream, format="PNG")
    return stream.getvalue()


def _workbook_bytes() -> bytes:
    stream = BytesIO()
    workbook = Workbook()
    workbook.active.title = "匿名素材"
    workbook.active.append(["匿名编号", "说明"])
    workbook.active.append(["A-001", "匿名测试"])
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _write_source_workbook(path: Path) -> None:
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    template.cell(5, 1, "结构")
    template.cell(5, 2, "镜号")
    for column, value in enumerate(
        ["画面示意", "效果示意", "效果", "类型", "景别", "运镜", "画面描述", "声音", "备注"],
        3,
    ):
        template.cell(6, column, value)
    template.cell(7, 1, "开场造势")
    template.cell(7, 2, 1)
    template.cell(7, 5, "转场")
    template.cell(7, 6, "实拍")
    template.cell(7, 7, "全景")
    template.cell(7, 8, "推进")
    template.cell(7, 9, "匿名镜头画面")
    creative = workbook.create_sheet("创意库")
    creative.cell(5, 1, "镜号")
    creative.cell(5, 2, "结构")
    for column, value in enumerate(
        ["画面示意", "效果", "机位", "景别", "运镜", "画面描述", "声音", "备注"],
        4,
    ):
        creative.cell(6, column, value)
    creative.cell(7, 1, "C001")
    creative.cell(7, 2, "开场造势")
    creative.cell(7, 5, "转场")
    creative.cell(7, 6, "实拍")
    creative.cell(7, 7, "全景")
    creative.cell(7, 8, "推进")
    creative.cell(7, 9, "匿名创意画面")
    workbook.save(path)
    workbook.close()


if __name__ == "__main__":
    unittest.main()
