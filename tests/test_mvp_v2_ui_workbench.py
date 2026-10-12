from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import Workbook
from PIL import Image

from mvp_v2.action_confirmation import ActionConfirmationService
from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.asset_center import AssetCenterService
from mvp_v2.asset_center_tools import (
    AssetCenterToolService,
    AssetCenterToolServices,
    AssetPackageResolver,
    AssetUploadStore,
)
from mvp_v2.asset_package import create_asset_package
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.local_ui import create_app
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.ui_workbench import UiWorkbenchService, UiWorkbenchServices
from mvp_v2.version_service import VersionService
from mvp_v2.version_workbench import VersionWorkbenchService, VersionWorkbenchServices
from mvp_v2.manual_version_commands import ManualVersionCommandService, ManualVersionCommandServices
from mvp_v2.manual_asset_commands import ManualAssetPublishCommandService, ManualAssetPublishCommandServices
from mvp_v2.workspace_store import WorkspaceStore


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_workbook(path: Path) -> None:
    workbook = Workbook()
    template = workbook.active
    template.title = "模版"
    template.cell(6, 2, "镜号")
    template.cell(7, 2, 1)
    template.cell(7, 9, "匿名镜头")
    creative = workbook.create_sheet("创意库")
    creative.cell(6, 1, "镜号")
    creative.cell(7, 1, "C001")
    creative.cell(7, 9, "匿名创意")
    workbook.save(path)


class UiWorkbenchFixture:
    def __init__(self, root: Path):
        self.root = root
        self.data_root = root / "runtime"
        self.runs_root = root / "runs"
        self.store = WorkspaceStore(self.data_root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="anonymous-ui-workbench-owner",
        )
        self.auth_token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.tasks = TaskService(self.store)
        self.versions = VersionService(self.store)
        self.project = self.projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="匿名受管项目",
            category_id="launch",
            expected_workspace_version=0,
        )
        self.session_id = self.tasks.create_agent_session(
            self.auth_token, project_id=self.project["project_id"]
        )["agent_session_id"]
        self.confirmations = ActionConfirmationService(self.store, b"u" * 32)

        uploads = AssetUploadStore(self.data_root)
        source = root / "source"
        source.mkdir()
        _source_workbook(source / "M+发布会类通用脚本库.xlsx")
        manifest = create_asset_package(
            root / "package",
            package_id="launch-ui-package",
            source_root=source,
            category_id="launch",
        )
        center = AssetCenterService(manifest, storage=uploads.storage)
        asset_tools = AssetCenterToolService(
            AssetCenterToolServices(
                store=self.store,
                projects=self.projects,
                tasks=self.tasks,
                confirmations=self.confirmations,
                idempotency=ToolIdempotencyService(self.store),
                uploads=uploads,
                packages=AssetPackageResolver([center]),
            )
        )
        version_workbench = VersionWorkbenchService(
            VersionWorkbenchServices(
                store=self.store,
                projects=self.projects,
                versions=self.versions,
                tasks=self.tasks,
                artifacts=RunArtifactResolver(self.runs_root),
                confirmations=self.confirmations,
                idempotency=ToolIdempotencyService(self.store),
            )
        )
        self.workbench = UiWorkbenchService(
            UiWorkbenchServices(
                versions=version_workbench,
                assets=asset_tools,
                manual_versions=ManualVersionCommandService(
                    ManualVersionCommandServices(
                        store=self.store,
                        projects=self.projects,
                        versions=self.versions,
                        artifacts=RunArtifactResolver(self.runs_root),
                    )
                ),
                manual_asset_publish=ManualAssetPublishCommandService(
                    ManualAssetPublishCommandServices(
                        store=self.store,
                        projects=self.projects,
                        packages=asset_tools.services.packages,
                    )
                ),
            )
        )
        self.asset_tools = asset_tools
        self.package_id = "launch-ui-package"
        self.package_version = 0
        self._run_index = 0

        self.app = create_app(ui_workbench=self.workbench)
        self.client = TestClient(self.app)
        self.headers = {
            "X-MPlus-UI-Token": self.app.state.session_token,
            "X-MPlus-Agent-Auth": self.auth_token,
        }
        self.manual_headers = {
            "X-MPlus-UI-Token": self.app.state.session_token,
            "X-MPlus-Manual-Auth": self.auth_token,
        }

    def add_version(
        self,
        copy: str,
        *,
        parent=None,
        human_review_status: str = "pending",
    ):
        self._run_index += 1
        run_id = f"run_ui_{self._run_index:04d}"
        run_dir = self.runs_root / "anonymous" / "runs" / run_id
        run_dir.mkdir(parents=True)
        common = {
            "script_rows": [{"镜号": "1", "copy": copy}],
            "detail_rows": [{
                "source_refs": [{
                    "source_file": "anonymous.xlsx",
                    "source_sheet": "template",
                    "source_row": 7,
                }]
            }],
        }
        payloads = {
            "script_plan": {"schema_version": "mplus-script-plan/v2", **common},
            "script_draft": {"schema_version": "mplus-script-draft/v1", **common},
            "visual_binding": {
                "schema_version": "mplus-visual-binding/v1",
                **common,
                "bindings": [{"shot_no": "1", "image_id": "media_anonymous"}],
            },
        }
        paths = {}
        for name, payload in payloads.items():
            path = run_dir / f"{name}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            paths[name] = path
        workbook = run_dir / "anonymous.xlsx"
        workbook.write_bytes(b"anonymous workbook")
        run_report = run_dir / "run_report.json"
        run_report.write_text(
            json.dumps({
                "run_id": run_id,
                "machine_audit_status": "pass",
                "human_review_status": human_review_status,
            }),
            encoding="utf-8",
        )
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(
            json.dumps({
                "run_id": run_id,
                "status": "waiting_human_review",
                "artifacts": {
                    "workbook": {
                        "path": str(workbook.resolve()),
                        "sha256": _sha(workbook),
                        "size_bytes": workbook.stat().st_size,
                    },
                    "run_report": {
                        "path": str(run_report.resolve()),
                        "sha256": _sha(run_report),
                        "size_bytes": run_report.stat().st_size,
                    },
                },
                "stages": {name: {
                    "stage_id": name,
                    "status": "succeeded",
                    "output_refs": [str(path.resolve())],
                    "output_sha256": _sha(path),
                } for name, path in paths.items()},
            }),
            encoding="utf-8",
        )
        project = self.projects.get_project(self.auth_token, self.project["project_id"])
        binding = self.projects.bind_run(
            self.auth_token,
            project_id=self.project["project_id"],
            run_id=run_id,
            parent_run_id=parent["run_id"] if parent else None,
            source_kind="native",
            manifest_ref=f"artifact://run/{run_id}/manifest",
            manifest_sha256=_sha(manifest),
            expected_project_version=project["project_version"],
        )
        return self.projects.create_project_version(
            self.auth_token,
            project_id=self.project["project_id"],
            run_id=run_id,
            parent_version_id=parent["version_id"] if parent else None,
            content_sha256=_sha(workbook),
            artifact_ref=f"artifact://run/{run_id}/workbook",
            expected_project_version=binding["project_version"],
        )

    def post(self, path: str, payload: dict):
        return self.client.post(path, json=payload, headers=self.headers)

    def manual_post(self, path: str, payload: dict):
        return self.client.post(path, json=payload, headers=self.manual_headers)


class UiWorkbenchApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = UiWorkbenchFixture(Path(self.temporary.name))

    def tearDown(self):
        self.fixture.client.close()
        self.temporary.cleanup()

    def test_version_tree_and_diff_require_auth_and_csrf_and_return_verified_changes(self):
        parent = self.fixture.add_version("旧文案")
        child = self.fixture.add_version("新文案", parent=parent)
        path = f"/api/agent/workbench/versions?project_id={self.fixture.project['project_id']}"

        self.assertEqual(self.fixture.client.get(path).status_code, 403)
        self.assertEqual(
            self.fixture.client.get(
                path,
                headers={"X-MPlus-UI-Token": self.fixture.app.state.session_token},
            ).status_code,
            401,
        )
        tree = self.fixture.client.get(path, headers=self.fixture.headers)
        self.assertEqual(tree.status_code, 200)
        self.assertEqual(tree.json()["version_count"], 2)
        flattened = [tree.json()["roots"][0], tree.json()["roots"][0]["children"][0]]
        self.assertEqual([item["version_no"] for item in flattened], [1, 2])
        self.assertTrue(flattened[0]["read_only"])
        self.assertFalse(flattened[0]["is_current"])
        self.assertTrue(flattened[1]["is_current"])

        preview = self.fixture.client.get(
            "/api/agent/workbench/versions/read",
            params={
                "project_id": self.fixture.project["project_id"],
                "version_id": parent["version_id"],
            },
            headers=self.fixture.headers,
        )
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()["read_only"])
        self.assertFalse(preview.json()["is_current"])
        self.assertEqual(preview.json()["version_no"], 1)
        self.assertEqual(preview.json()["shots"][0]["fields"]["copy"], "旧文案")
        self.assertNotIn(str(Path(self.temporary.name)), preview.text)

        payload = {
            "project_id": self.fixture.project["project_id"],
            "left_version_id": parent["version_id"],
            "right_version_id": child["version_id"],
        }
        no_csrf = self.fixture.client.post(
            "/api/agent/workbench/versions/compare",
            json=payload,
            headers={"X-MPlus-Agent-Auth": self.fixture.auth_token},
        )
        self.assertEqual(no_csrf.status_code, 403)
        compared = self.fixture.post(
            "/api/agent/workbench/versions/compare", payload
        )
        self.assertEqual(compared.status_code, 200)
        result = compared.json()
        self.assertEqual(result["summary"]["changed_field_count"], 1)
        self.assertEqual(result["shot_diffs"][0]["field_diffs"][0]["field"], "copy")
        self.assertNotIn(str(Path(self.temporary.name)), json.dumps(result, ensure_ascii=False))

    def test_asset_freeze_create_review_publish_and_search_are_confirmed_and_zero_provider(self):
        image_path = Path(self.temporary.name) / "anonymous-search.png"
        buffer = BytesIO()
        Image.new("RGB", (64, 64), "green").save(buffer, format="PNG")
        image_path.write_bytes(buffer.getvalue())
        base = {"agent_session_id": self.fixture.session_id}

        frozen = self.fixture.post(
            "/api/agent/workbench/assets/freeze",
            {**base, "path": str(image_path)},
        )
        self.assertEqual(frozen.status_code, 200)
        self.assertNotIn(str(image_path), json.dumps(frozen.json(), ensure_ascii=False))
        record = None
        with patch("mvp_v2.image_understanding.generate_minimax_visual_analysis") as provider:
            create = {
                "package_id": self.fixture.package_id,
                "expected_active_package_version": self.fixture.package_version,
                "idempotency_key": "ui-create-asset-0001",
                "category_id": "launch",
                "scope": "project",
                "file_refs": [frozen.json()["artifact_ref"]],
            }
            record = self._confirmed_action("create_asset_import", create)
            self.assertEqual(record["state"], "staged")
            self.assertEqual(record["machine_review_status"], "passed")

            review = {
                "package_id": self.fixture.package_id,
                "expected_active_package_version": self.fixture.package_version,
                "idempotency_key": "ui-review-asset-0001",
                "import_id": record["import_id"],
                "expected_import_revision": record["revision"],
            }
            record = self._confirmed_action("review_asset_import", review)
            self.assertEqual(record["state"], "reviewed")

            publish = {
                "package_id": self.fixture.package_id,
                "expected_active_package_version": self.fixture.package_version,
                "idempotency_key": "ui-publish-asset-0001",
                "import_id": record["import_id"],
                "expected_import_revision": record["revision"],
            }
            rejected = self.fixture.post(
                "/api/agent/workbench/assets/actions/prepare",
                {"agent_session_id": self.fixture.session_id, "tool_name": "publish_asset_import", "parameters": publish},
            )
            self.assertEqual(rejected.status_code, 422)
            with self.fixture.store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM manual_command_audits").fetchone()[0], 0)
            record = self._confirmed_asset_publish(publish)
            self.assertEqual(record["state"], "published")

            search = self.fixture.post(
                "/api/agent/workbench/assets/search",
                {
                    **base,
                    "category_id": "launch",
                    "query": "anonymous search",
                    "kind": "image",
                },
            )
        provider.assert_not_called()
        self.assertEqual(search.status_code, 200)
        self.assertEqual(len(search.json()["results"]), 1)
        self.assertEqual(search.json()["results"][0]["workflow_state"], "published")

        context = self.fixture.client.get(
            f"/api/agent/workbench/assets?agent_session_id={self.fixture.session_id}",
            headers=self.fixture.headers,
        )
        self.assertEqual(context.status_code, 200)
        self.assertEqual(context.json()["provider_calls"], 0)
        self.assertEqual(context.json()["imports"][0]["state"], "published")

    def test_version_review_candidate_and_publication_are_explicit_zero_model_actions(self):
        version = self.fixture.add_version(
            "人工已核对版本", human_review_status="quality_pass"
        )
        first_parameters = {
            "project_id": self.fixture.project["project_id"],
            "version_id": version["version_id"],
            "expected_project_version": self.fixture.projects.get_project(
                self.fixture.auth_token, self.fixture.project["project_id"]
            )["project_version"],
            "idempotency_key": "ui-version-review-0001",
        }
        prepare_payload = {
            "agent_session_id": self.fixture.session_id,
            "tool_name": "record_version_review",
            "parameters": first_parameters,
        }
        self.assertEqual(
            self.fixture.client.post(
                "/api/agent/workbench/versions/actions/prepare",
                json=prepare_payload,
                headers={"X-MPlus-Agent-Auth": self.fixture.auth_token},
            ).status_code,
            404,
        )
        with self.fixture.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM manual_command_audits").fetchone()[0], 0)
        self.assertEqual(
            self.fixture.client.post(
                "/api/agent/workbench/versions/actions/prepare",
                json=prepare_payload,
                headers={
                    "X-MPlus-UI-Token": self.fixture.app.state.session_token
                },
            ).status_code,
            404,
        )
        with patch(
            "mvp_v2.image_understanding.generate_minimax_visual_analysis"
        ) as provider:
            review = self._confirmed_version_action(
                "record_version_review",
                first_parameters,
            )
            candidate = self._confirmed_version_action(
                "select_candidate_version",
                {
                    "project_id": self.fixture.project["project_id"],
                    "version_id": version["version_id"],
                    "expected_project_version": review["project_version"],
                    "idempotency_key": "ui-version-candidate-0001",
                },
            )
            publication = self._confirmed_version_action(
                "publish_version",
                {
                    "project_id": self.fixture.project["project_id"],
                    "version_id": version["version_id"],
                    "review_id": review["review_id"],
                    "expected_project_version": candidate["project_version"],
                    "idempotency_key": "ui-version-publication-0001",
                },
            )
        provider.assert_not_called()
        self.assertEqual(publication["projection_status"], "pending")
        tree = self.fixture.workbench.version_tree(
            self.fixture.auth_token, self.fixture.project["project_id"]
        )
        governance = tree["roots"][0]["governance"]
        self.assertEqual(governance["latest_review"]["decision"], "quality_pass")
        self.assertTrue(governance["is_current_candidate"])
        self.assertEqual(
            governance["latest_publication"]["status"], "published"
        )

    def _confirmed_version_action(self, tool_name: str, parameters: dict) -> dict:
        prepared_response = self.fixture.manual_post(
            "/api/manual/versions/actions/prepare",
            {
                "tool_name": tool_name,
                "parameters": parameters,
            },
        )
        self.assertEqual(prepared_response.status_code, 200, prepared_response.text)
        prepared = prepared_response.json()
        self.assertEqual(prepared["risk"], "R3")
        self.assertFalse(prepared["calls_models"])
        for field in (
            "will_change",
            "will_not_change",
            "calls_models",
            "impact_scope",
        ):
            self.assertIn(field, prepared)
        executed_response = self.fixture.manual_post(
            "/api/manual/versions/actions/execute",
            {
                "tool_name": tool_name,
                "parameters": parameters,
                "command_token": prepared["command_token"],
            },
        )
        self.assertEqual(executed_response.status_code, 200, executed_response.text)
        executed = executed_response.json()
        self.assertEqual(executed["status"], "completed")
        return executed["result"]

    def _confirmed_action(self, tool_name: str, parameters: dict) -> dict:
        base = {"agent_session_id": self.fixture.session_id}
        prepared = self.fixture.post(
            "/api/agent/workbench/assets/actions/prepare",
            {**base, "tool_name": tool_name, "parameters": parameters},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        executed = self.fixture.post(
            "/api/agent/workbench/assets/actions/execute",
            {
                **base,
                "tool_name": tool_name,
                "parameters": parameters,
                "confirmation_token": prepared.json()["confirmation_token"],
            },
        )
        self.assertEqual(executed.status_code, 200, executed.text)
        self.assertEqual(executed.json()["status"], "completed")
        return executed.json()["result"]

    def _confirmed_asset_publish(self, parameters: dict) -> dict:
        prepared = self.fixture.manual_post(
            "/api/manual/assets/publish/prepare",
            {"project_id": self.fixture.project["project_id"], **parameters},
        )
        self.assertEqual(prepared.status_code, 200, prepared.text)
        executed = self.fixture.manual_post(
            "/api/manual/assets/publish/execute",
            {
                "project_id": self.fixture.project["project_id"],
                "parameters": parameters,
                "command_token": prepared.json()["command_token"],
            },
        )
        self.assertEqual(executed.status_code, 200, executed.text)
        self.assertEqual(executed.json()["status"], "completed")
        return executed.json()["result"]


if __name__ == "__main__":
    unittest.main()
