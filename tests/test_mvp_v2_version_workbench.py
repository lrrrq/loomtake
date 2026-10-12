from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from mvp_v2.action_confirmation import ActionConfirmationService, ManualPageConfirmationService
from mvp_v2.artifact_service import ArtifactResolutionError, RunArtifactResolver
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.tool_idempotency import ToolIdempotencyService
from mvp_v2.version_delivery import VersionDeliveryService, resolve_version_shot_ids
from mvp_v2.version_service import VersionService
from mvp_v2.version_workbench import (
    VersionWorkbenchRecoveryRequired,
    VersionWorkbenchService,
    VersionWorkbenchServices,
)
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore


LINEAGE_FIXTURE = (
    Path(__file__).with_name("fixtures") / "revision_lineage_cases.json"
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorkbenchFixture:
    def __init__(self, root: Path, *, checkpoint=None):
        self.root = root
        self.runs_root = root / "runs"
        self.store = WorkspaceStore(root / "workspace.sqlite3")
        self.identity = LocalIdentityAdapter(self.store)
        owner = self.identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="anonymous-workbench-owner-token",
        )
        self.auth_token = owner["auth_token"]
        self.workspace_id = owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.versions = VersionService(self.store)
        self.tasks = TaskService(self.store)
        self.project = self.projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="匿名主项目",
            category_id="launch",
            expected_workspace_version=0,
        )
        self.other_project = self.projects.create_project(
            self.auth_token,
            workspace_id=self.workspace_id,
            name="匿名隔离项目",
            category_id="launch",
            expected_workspace_version=1,
        )
        self.agent_session_id = self.tasks.create_agent_session(
            self.auth_token, project_id=self.project["project_id"]
        )["agent_session_id"]
        self.confirmations = ActionConfirmationService(self.store, b"w" * 32)
        self.services = VersionWorkbenchServices(
            store=self.store,
            projects=self.projects,
            versions=self.versions,
            tasks=self.tasks,
            artifacts=RunArtifactResolver(self.runs_root),
            confirmations=self.confirmations,
            idempotency=ToolIdempotencyService(self.store),
        )
        self.workbench = VersionWorkbenchService(
            self.services, checkpoint=checkpoint
        )
        self._run_index = 0

    def project_version(self, project_id=None) -> int:
        return self.projects.get_project(
            self.auth_token, project_id or self.project["project_id"]
        )["project_version"]

    def add_version(
        self,
        shots: list[dict],
        *,
        project_id=None,
        parent_version: dict | None = None,
        invalid_artifact: str = "",
        human_review_status: str = "pending",
        machine_audit_status: str = "pass",
        report_run_id: str = "",
        script_plan_schema: str = "mplus-script-plan/v2",
        revision_report: dict | None = None,
    ) -> dict:
        project_id = project_id or self.project["project_id"]
        self._run_index += 1
        run_id = f"run_anon_{self._run_index:04d}"
        run_dir = self.runs_root / "匿名项目" / "runs" / run_id
        run_dir.mkdir(parents=True)
        script_rows = [
            {"镜号": str(shot["shot_no"]), **dict(shot.get("fields") or {})}
            for shot in shots
        ]
        detail_rows = [
            {"镜号": str(shot["shot_no"]), "source_refs": list(shot.get("source_refs") or [])}
            for shot in shots
        ]
        common = {"script_rows": script_rows, "detail_rows": detail_rows}
        payloads = {
            "script_plan": {
                "schema_version": script_plan_schema,
                **common,
            },
            "script_draft": {
                "schema_version": "mplus-script-draft/v1",
                **common,
            },
            "visual_binding": {
                "schema_version": "mplus-visual-binding/v1",
                **common,
                "bindings": [
                    {
                        "shot_no": str(shot["shot_no"]),
                        "image_id": f"media_anonymous_{shot['shot_no']}",
                        "original_path": str(
                            (self.root / "private-assets" / "anonymous.png").resolve()
                        ),
                    }
                    for shot in shots
                ],
            },
        }
        artifact_paths = {}
        for name, payload in payloads.items():
            path = run_dir / f"{name}.json"
            if invalid_artifact == name:
                path.write_text("not-json", encoding="utf-8")
            else:
                path.write_text(
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
            artifact_paths[name] = path
        workbook = run_dir / "anonymous-workbook.xlsx"
        workbook.write_bytes(b"anonymous workbook fixture")
        run_report = run_dir / "run_report.json"
        run_report.write_text(
            json.dumps(
                {
                    "run_id": report_run_id or run_id,
                    "machine_audit_status": machine_audit_status,
                    "human_review_status": human_review_status,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        report_artifacts = {}
        if revision_report is not None:
            revision_path = run_dir / "revision_report.json"
            revision_path.write_text(
                json.dumps(revision_report, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            report_artifacts["revision_report"] = {
                "path": str(revision_path.resolve()),
                "sha256": _sha(revision_path),
                "size_bytes": revision_path.stat().st_size,
            }
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(
            json.dumps(
                {
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
                        **report_artifacts,
                    },
                    "stages": {
                        name: {
                            "stage_id": name,
                            "status": "succeeded",
                            "output_refs": [str(path.resolve())],
                            "output_sha256": _sha(path),
                        }
                        for name, path in artifact_paths.items()
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        bound = self.projects.bind_run(
            self.auth_token,
            project_id=project_id,
            run_id=run_id,
            parent_run_id=parent_version["run_id"] if parent_version else None,
            source_kind="native",
            manifest_ref=f"artifact://run/{run_id}/manifest",
            manifest_sha256=_sha(manifest),
            expected_project_version=self.project_version(project_id),
        )
        return self.projects.create_project_version(
            self.auth_token,
            project_id=project_id,
            run_id=run_id,
            parent_version_id=(
                parent_version["version_id"] if parent_version else None
            ),
            content_sha256=_sha(workbook),
            artifact_ref=f"artifact://run/{run_id}/workbook",
            expected_project_version=bound["project_version"],
        )

    def issue(self, tool_name: str, parameters: dict) -> str:
        return ManualPageConfirmationService(self.store, self.confirmations.signing_key).issue(
            self.auth_token,
            agent_session_id=self.agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
        )["confirmation_token"]


class VersionWorkbenchReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        payload = json.loads(LINEAGE_FIXTURE.read_text(encoding="utf-8"))
        cls.lineage_cases = payload["cases"]

    def test_v4_parent_and_child_read_preserves_all_shots_and_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            shot_count = 17
            parent_shots = [
                {
                    "shot_no": str(index),
                    "fields": {
                        "结构": "开场" if index == 1 else "活动展示",
                        "画面描述": f"匿名活动画面 {index}",
                        "声音": f"匿名旁白 {index}",
                    },
                    "source_refs": [{
                        "source_file": "anonymous-event.xlsx",
                        "source_sheet": "脚本模板",
                        "source_row": index + 6,
                    }],
                }
                for index in range(1, shot_count + 1)
            ]
            child_shots = json.loads(json.dumps(parent_shots))
            child_shots[2]["fields"]["声音"] = "第三镜修订旁白"
            parent = fixture.add_version(
                parent_shots, script_plan_schema="mplus-script-plan/v4"
            )
            child = fixture.add_version(
                child_shots,
                parent_version=parent,
                script_plan_schema="mplus-script-plan/v4",
            )

            tree = fixture.workbench.list_version_tree(
                fixture.auth_token, fixture.project["project_id"]
            )
            self.assertEqual(tree["version_count"], 2)
            self.assertEqual(tree["roots"][0]["version_id"], parent["version_id"])
            self.assertEqual(
                tree["roots"][0]["children"][0]["version_id"], child["version_id"]
            )
            for version_no, version, expected_shots in (
                (1, parent, parent_shots), (2, child, child_shots)
            ):
                with self.subTest(version_no=version_no):
                    content = fixture.workbench.read_version(
                        fixture.auth_token,
                        project_id=fixture.project["project_id"],
                        version_id=version["version_id"],
                    )
                    self.assertEqual(content["project_id"], fixture.project["project_id"])
                    self.assertEqual(content["version_id"], version["version_id"])
                    self.assertEqual(content["version_no"], version_no)
                    self.assertTrue(content["verified"])
                    self.assertTrue(content["read_only"])
                    self.assertEqual(len(content["shots"]), len(expected_shots))
                    for actual, expected in zip(content["shots"], expected_shots):
                        self.assertEqual(actual["shot_no"], expected["shot_no"])
                        self.assertEqual(actual["source_refs"], expected["source_refs"])
                        self.assertEqual(
                            {key: actual["fields"][key] for key in expected["fields"]},
                            expected["fields"],
                        )

    def test_legacy_versions_without_lineage_do_not_share_number_based_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = WorkbenchFixture(Path(directory))
            shots = [{'shot_no': str(i), 'fields': {'画面描述': '同来源匿名画面', '声音': '现场声'},
                'source_refs': [{'source_file': 'anonymous.xlsx', 'source_sheet': '模版', 'source_row': 7}]}
                for i in range(1, 4)]
            parent = fixture.add_version(shots)
            child = fixture.add_version(shots[:2], parent_version=parent)
            resolver = fixture.services.artifacts
            delivery = VersionDeliveryService(fixture.store, resolver)
            contexts = [delivery.resolve(fixture.auth_token, fixture.project['project_id'],
                version['version_id'], ancestor_of=child['version_id']) for version in (parent, child)]
            before = {path: path.read_bytes() for path in fixture.runs_root.rglob('*') if path.is_file()}
            old_ids, new_ids = [resolve_version_shot_ids(resolver, context) for context in contexts]
            self.assertEqual(set(old_ids), {'1', '2', '3'})
            self.assertEqual(set(new_ids), {'1', '2'})
            self.assertTrue(set(old_ids.values()).isdisjoint(new_ids.values()))
            self.assertEqual(resolve_version_shot_ids(RunArtifactResolver(fixture.runs_root), contexts[1]), new_ids)
            self.assertEqual(before, {path: path.read_bytes() for path in before})
            self.assertFalse(list(fixture.runs_root.rglob('revision_report.json')))

    def test_v4_compare_uses_saved_lineage_for_sound_change_with_shared_source(self):
        self._compare_saved_lineage_sound_change(parent_report=True)

    def test_v4_compare_initial_version_with_saved_child_uses_original_shot_ids(self):
        self._compare_saved_lineage_sound_change(parent_report=False)

    def _compare_saved_lineage_sound_change(self, *, parent_report):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            parent_id = "version_anonymous_saved_parent"
            child_id = "version_anonymous_saved_child"
            shot_ids = {str(index): f"shot_anonymous_stable_{index}" for index in range(1, 18)}
            parent_shots = [{
                "shot_no": str(index),
                "fields": {
                    "结构": "开场" if index <= 2 else "活动展示",
                    "画面描述": f"匿名活动画面 {index}",
                    "声音": "现场音乐推进",
                    "字幕": "", "旁白": "", "备注": "建议时长：4s",
                    "时长(秒)": 4, "景别": "全景", "类型": "平视", "运镜": "固定",
                    "效果": "", "效果示意": "", "画面示意": "anonymous.png",
                },
                "source_refs": [{
                    "source_file": "anonymous-event.xlsx", "source_sheet": "脚本模板",
                    # Repeated source rows are valid; source equality is not shot identity.
                    "source_row": 8 if index <= 2 else index + 6,
                }],
            } for index in range(1, 18)]
            child_shots = json.loads(json.dumps(parent_shots))
            child_shots[1]["fields"]["声音"] = "现场掌声后衔接克制的节奏音乐"

            def frozen_lineage(version_id, parent_version_id):
                return {
                    "schema_version": "mplus-revision-report/v1",
                    "project_id": fixture.project["project_id"],
                    "parent_version_id": parent_version_id,
                    "child_version_id": version_id,
                    "version_diff": {"shot_diffs": [{
                        "relation": "preserved", "child_shot_no": shot_no,
                        "child_shot_id": shot_id, "parent_shot_ids": [shot_id],
                        "parent_shot_nos": [shot_no],
                    } for shot_no, shot_id in shot_ids.items()]},
                }

            with patch("mvp_v2.project_service.new_id", return_value=parent_id):
                parent = fixture.add_version(
                    parent_shots, script_plan_schema="mplus-script-plan/v4",
                    revision_report=frozen_lineage(parent_id, None) if parent_report else None,
                )
            if not parent_report:
                # The first version uses the same authority as the real script workspace.
                context = VersionDeliveryService(fixture.store, fixture.services.artifacts).resolve(
                    fixture.auth_token, fixture.project["project_id"], parent_id,
                )
                shot_ids = resolve_version_shot_ids(fixture.services.artifacts, context)
            with patch("mvp_v2.project_service.new_id", return_value=child_id):
                child = fixture.add_version(
                    child_shots, parent_version=parent, script_plan_schema="mplus-script-plan/v4",
                    revision_report=frozen_lineage(child_id, parent_id),
                )
            diff = fixture.workbench.compare_versions(
                fixture.auth_token, project_id=fixture.project["project_id"],
                left_version_id=parent["version_id"], right_version_id=child["version_id"],
            )
            changed_sound = [
                (shot["child_shot_no"], shot["child_shot_id"], shot["parent_shot_ids"],
                 field["before"], field["after"])
                for shot in diff["shot_diffs"] for field in shot["field_diffs"]
                if field["field"] == "声音" and field["status"] == "changed"
            ]
            self.assertEqual(changed_sound, [(
                "2", shot_ids["2"], [shot_ids["2"]],
                "现场音乐推进", "现场掌声后衔接克制的节奏音乐",
            )])
            self.assertEqual(diff["summary"]["parent_shot_count"], 17)
            self.assertEqual(diff["summary"]["child_shot_count"], 17)
            self.assertEqual(diff["summary"]["relation_counts"], {"preserved": 17})
            self.assertEqual(diff["summary"]["changed_shot_count"], 1)
            self.assertEqual(diff["summary"]["changed_field_count"], 1)
            self.assertEqual(diff["summary"]["field_change_counts"], {"声音": 1})
            self.assertFalse(diff["lineage_needs_review"])

    def test_read_version_rejects_unknown_plan_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            version = fixture.add_version(
                self.lineage_cases[1]["parent"],
                script_plan_schema="mplus-script-plan/v999",
            )
            with self.assertRaises(ArtifactResolutionError) as invalid:
                fixture.workbench.read_version(
                    fixture.auth_token,
                    project_id=fixture.project["project_id"],
                    version_id=version["version_id"],
                )
            self.assertEqual(invalid.exception.code, "ARTIFACT_INVALID")
            self.assertIn("script_plan schema_version 不受支持", str(invalid.exception))

    def test_tree_is_project_scoped_and_compare_rejects_cross_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            root = fixture.add_version(self.lineage_cases[0]["parent"])
            child = fixture.add_version(
                self.lineage_cases[0]["child"], parent_version=root
            )
            other = fixture.add_version(
                self.lineage_cases[0]["parent"],
                project_id=fixture.other_project["project_id"],
            )

            tree = fixture.workbench.list_version_tree(
                fixture.auth_token, fixture.project["project_id"]
            )
            self.assertEqual(tree["version_count"], 2)
            self.assertEqual(tree["roots"][0]["version_id"], root["version_id"])
            self.assertEqual(
                tree["roots"][0]["children"][0]["version_id"],
                child["version_id"],
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                fixture.workbench.compare_versions(
                    fixture.auth_token,
                    project_id=fixture.project["project_id"],
                    left_version_id=root["version_id"],
                    right_version_id=other["version_id"],
                )

    def test_service_adapter_covers_all_six_lineage_relations(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            covered = set()
            for case in self.lineage_cases:
                if case["name"] == "ambiguous":
                    continue
                parent = fixture.add_version(case["parent"])
                child = fixture.add_version(case["child"], parent_version=parent)
                diff = fixture.workbench.compare_versions(
                    fixture.auth_token,
                    project_id=fixture.project["project_id"],
                    left_version_id=parent["version_id"],
                    right_version_id=child["version_id"],
                )
                covered.update(diff["summary"]["relation_counts"])
                self.assertEqual(
                    diff["summary"]["relation_counts"], case["expected"]
                )
                serialized = json.dumps(diff, ensure_ascii=False)
                self.assertNotIn(str(fixture.root.resolve()), serialized)
                self.assertEqual(
                    set(diff["verified_artifacts"]["left"]["artifacts"]),
                    {"script_plan", "script_draft", "visual_binding"},
                )
            self.assertEqual(
                covered,
                {"preserved", "moved", "split_from", "merged_from", "new", "deleted"},
            )

    def test_hash_drift_and_malformed_artifact_block_with_stable_codes(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            parent = fixture.add_version(self.lineage_cases[0]["parent"])
            child = fixture.add_version(self.lineage_cases[0]["child"])
            draft = (
                fixture.runs_root
                / "匿名项目"
                / "runs"
                / child["run_id"]
                / "script_draft.json"
            )
            draft.write_text("{}", encoding="utf-8")

            with self.assertRaises(ArtifactResolutionError) as drift:
                fixture.workbench.compare_versions(
                    fixture.auth_token,
                    project_id=fixture.project["project_id"],
                    left_version_id=parent["version_id"],
                    right_version_id=child["version_id"],
                )
            self.assertEqual(drift.exception.code, "ARTIFACT_HASH_MISMATCH")

            malformed = fixture.add_version(
                self.lineage_cases[0]["child"], invalid_artifact="script_plan"
            )
            with self.assertRaises(ArtifactResolutionError) as invalid:
                fixture.workbench.compare_versions(
                    fixture.auth_token,
                    project_id=fixture.project["project_id"],
                    left_version_id=parent["version_id"],
                    right_version_id=malformed["version_id"],
                )
            self.assertEqual(invalid.exception.code, "ARTIFACT_INVALID")

    def test_revision_report_preserves_waiting_human_review_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            case = self.lineage_cases[0]
            parent = fixture.add_version(case["parent"])
            child = fixture.add_version(case["child"], parent_version=parent)
            report = fixture.workbench.build_revision_report(
                fixture.auth_token,
                project_id=fixture.project["project_id"],
                parent_version_id=parent["version_id"],
                child_version_id=child["version_id"],
                original_feedback="只修改镜 1 文案",
                structured_intent={"requested_fields": ["copy"]},
                expected_impact={"fields": ["copy"], "shot_nos": ["1"]},
                locked_fields=[{"shot_no": "1", "field": "structure"}],
                rerun_nodes=["script_draft"],
                unresolved_items=[],
                machine_review={"status": "pass"},
                human_review={"status": "waiting_human_review"},
                publication={"status": "unpublished"},
            )

            self.assertEqual(report["completion"]["status"], "waiting_human_review")
            self.assertFalse(report["completion"]["completed"])
            self.assertEqual(
                report["statuses"]["human_review"]["status"],
                "waiting_human_review",
            )


class VersionWorkbenchControlledWriteTests(unittest.TestCase):
    def _record_parameters(self, fixture, version, *, key="review-key-0001"):
        return {
            "project_id": fixture.project["project_id"],
            "version_id": version["version_id"],
            "expected_project_version": fixture.project_version(),
            "idempotency_key": key,
        }

    def _select_parameters(self, fixture, version, *, key="candidate-key-0001"):
        return {
            "project_id": fixture.project["project_id"],
            "version_id": version["version_id"],
            "expected_project_version": fixture.project_version(),
            "idempotency_key": key,
        }

    def _select(self, fixture, parameters):
        token = fixture.issue("select_candidate_version", parameters)
        return fixture.workbench.select_candidate_version(
            fixture.auth_token,
            agent_session_id=fixture.agent_session_id,
            confirmation_token=token,
            **parameters,
        ), token

    def test_candidate_and_publication_are_once_only_and_never_write_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            version = fixture.add_version([{
                "shot_no": "1",
                "fields": {"copy": "anonymous"},
                "source_refs": [{
                    "source_file": "anonymous.xlsx",
                    "source_sheet": "template",
                    "source_row": 7,
                }],
            }])
            select_parameters = self._select_parameters(fixture, version)
            selected, select_token = self._select(fixture, select_parameters)
            replayed_selection = fixture.workbench.select_candidate_version(
                fixture.auth_token,
                agent_session_id=fixture.agent_session_id,
                confirmation_token=select_token,
                **select_parameters,
            )
            self.assertEqual(selected["result"], replayed_selection["result"])
            self.assertTrue(replayed_selection["replayed"])

            review = fixture.versions.record_review(
                fixture.auth_token,
                version_id=version["version_id"],
                decision="quality_pass",
                evidence_ref="artifact://review/anonymous/quality-pass",
                expected_project_version=selected["result"]["project_version"],
            )
            publish_parameters = {
                "project_id": fixture.project["project_id"],
                "version_id": version["version_id"],
                "review_id": review["review_id"],
                "expected_project_version": review["project_version"],
                "idempotency_key": "publication-key-0001",
            }
            publish_token = fixture.issue("publish_version", publish_parameters)
            published = fixture.workbench.publish_version(
                fixture.auth_token,
                agent_session_id=fixture.agent_session_id,
                confirmation_token=publish_token,
                **publish_parameters,
            )
            replayed_publication = fixture.workbench.publish_version(
                fixture.auth_token,
                agent_session_id=fixture.agent_session_id,
                confirmation_token=publish_token,
                **publish_parameters,
            )

            self.assertEqual(published["result"], replayed_publication["result"])
            self.assertEqual(published["result"]["projection_status"], "pending")
            self.assertTrue(replayed_publication["replayed"])
            self.assertFalse((fixture.runs_root / "CURRENT").exists())
            with fixture.store.connection() as connection:
                counts = {
                    table: connection.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    for table in (
                        "candidate_selections",
                        "version_publications",
                    )
                }
                consumed = connection.execute(
                    "SELECT COUNT(*) FROM action_confirmations WHERE status = 'consumed'"
                ).fetchone()[0]
            self.assertEqual(counts, {
                "candidate_selections": 1,
                "version_publications": 1,
            })
            self.assertEqual(consumed, 2)

    def test_record_review_is_derived_from_verified_run_report_and_replayed_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            version = fixture.add_version(
                [{"shot_no": "1", "fields": {}, "source_refs": []}],
                human_review_status="quality_pass",
            )
            parameters = self._record_parameters(fixture, version)
            confirmation = fixture.workbench.issue_confirmation(
                fixture.auth_token,
                agent_session_id=fixture.agent_session_id,
                tool_name="record_version_review",
                parameters=parameters,
            )
            recorded = fixture.workbench.record_version_review(
                fixture.auth_token,
                agent_session_id=fixture.agent_session_id,
                confirmation_token=confirmation["confirmation_token"],
                **parameters,
            )
            replayed = fixture.workbench.record_version_review(
                fixture.auth_token,
                agent_session_id=fixture.agent_session_id,
                confirmation_token=confirmation["confirmation_token"],
                **parameters,
            )

            self.assertEqual(recorded["status"], "completed")
            self.assertEqual(recorded["result"], replayed["result"])
            self.assertTrue(replayed["replayed"])
            self.assertEqual(recorded["result"]["decision"], "quality_pass")
            self.assertEqual(
                recorded["result"]["evidence_ref"],
                f"artifact://run/{version['run_id']}/run_report",
            )
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM version_reviews"
                    ).fetchone()[0],
                    1,
                )

    def test_pending_cross_project_and_stale_project_are_blocked_before_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            pending = fixture.add_version(
                [{"shot_no": "1", "fields": {}, "source_refs": []}]
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "REVIEW_REQUIRED"):
                fixture.workbench.issue_confirmation(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    tool_name="record_version_review",
                    parameters=self._record_parameters(fixture, pending),
                )

            machine_blocked = fixture.add_version(
                [{"shot_no": "1", "fields": {}, "source_refs": []}],
                human_review_status="quality_pass",
                machine_audit_status="fail",
            )
            with self.assertRaisesRegex(WorkspaceServiceError, "REVIEW_REQUIRED"):
                fixture.workbench.issue_confirmation(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    tool_name="record_version_review",
                    parameters=self._record_parameters(fixture, machine_blocked),
                )

            mismatched_report = fixture.add_version(
                [{"shot_no": "1", "fields": {}, "source_refs": []}],
                human_review_status="quality_pass",
                report_run_id="run_other_0001",
            )
            with self.assertRaisesRegex(
                ArtifactResolutionError, "ARTIFACT_ID_MISMATCH"
            ):
                fixture.workbench.issue_confirmation(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    tool_name="record_version_review",
                    parameters=self._record_parameters(fixture, mismatched_report),
                )

            other = fixture.add_version(
                [{"shot_no": "1", "fields": {}, "source_refs": []}],
                project_id=fixture.other_project["project_id"],
                human_review_status="quality_pass",
            )
            cross_project = {
                "project_id": fixture.other_project["project_id"],
                "version_id": other["version_id"],
                "expected_project_version": fixture.project_version(
                    fixture.other_project["project_id"]
                ),
                "idempotency_key": "review-cross-project-0001",
            }
            with self.assertRaisesRegex(WorkspaceServiceError, "PERMISSION_DENIED"):
                fixture.workbench.issue_confirmation(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    tool_name="record_version_review",
                    parameters=cross_project,
                )

            passed = fixture.add_version(
                [{"shot_no": "1", "fields": {}, "source_refs": []}],
                human_review_status="quality_pass",
            )
            stale = self._record_parameters(fixture, passed)
            stale["expected_project_version"] -= 1
            with self.assertRaisesRegex(
                WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"
            ):
                fixture.workbench.issue_confirmation(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    tool_name="record_version_review",
                    parameters=stale,
                )

    def test_verified_quality_fail_is_recorded_but_never_publishable(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            version = fixture.add_version(
                [{"shot_no": "1", "fields": {}, "source_refs": []}],
                human_review_status="quality_fail",
            )
            reviewed = self._execute_prepared(
                fixture,
                "record_version_review",
                self._record_parameters(
                    fixture, version, key="review-quality-fail-0001"
                ),
            )["result"]
            candidate = self._execute_prepared(
                fixture,
                "select_candidate_version",
                self._select_parameters(
                    fixture, version, key="candidate-quality-fail-0001"
                ),
            )["result"]
            self.assertEqual(reviewed["decision"], "quality_fail")
            with self.assertRaisesRegex(WorkspaceServiceError, "REVIEW_REQUIRED"):
                fixture.workbench.issue_confirmation(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    tool_name="publish_version",
                    parameters={
                        "project_id": fixture.project["project_id"],
                        "version_id": version["version_id"],
                        "review_id": reviewed["review_id"],
                        "expected_project_version": candidate["project_version"],
                        "idempotency_key": "publish-quality-fail-0001",
                    },
                )

    def test_report_tamper_and_manifest_drift_after_confirmation_fail_closed(self):
        for drift in ("run_report", "manifest"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as tmp:
                fixture = WorkbenchFixture(Path(tmp))
                version = fixture.add_version(
                    [{"shot_no": "1", "fields": {}, "source_refs": []}],
                    human_review_status="quality_pass",
                )
                parameters = self._record_parameters(
                    fixture, version, key=f"review-drift-{drift}-0001"
                )
                confirmation = fixture.workbench.issue_confirmation(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    tool_name="record_version_review",
                    parameters=parameters,
                )
                run_dir = next(
                    fixture.runs_root.glob(f"*/runs/{version['run_id']}")
                )
                target = (
                    run_dir / "run_report.json"
                    if drift == "run_report"
                    else run_dir / "run_manifest.json"
                )
                target.write_text("tampered", encoding="utf-8")

                result = fixture.workbench.record_version_review(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    confirmation_token=confirmation["confirmation_token"],
                    **parameters,
                )
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"]["code"], "REVIEW_REQUIRED")
                self.assertEqual(
                    result["error"]["details"]["source_code"],
                    "ARTIFACT_HASH_MISMATCH",
                )
                self.assertEqual(fixture.versions.list_reviews(
                    fixture.auth_token, version["version_id"]
                ), [])

    def test_verified_pass_candidate_and_publication_form_one_zero_provider_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            version = fixture.add_version(
                [{"shot_no": "1", "fields": {}, "source_refs": []}],
                human_review_status="quality_pass",
            )
            with patch(
                "mvp_v2.image_understanding.generate_minimax_visual_analysis"
            ) as provider:
                review = self._execute_prepared(
                    fixture,
                    "record_version_review",
                    self._record_parameters(fixture, version),
                )["result"]
                candidate = self._execute_prepared(
                    fixture,
                    "select_candidate_version",
                    self._select_parameters(
                        fixture, version, key="candidate-chain-0001"
                    ),
                )["result"]
                publish_parameters = {
                    "project_id": fixture.project["project_id"],
                    "version_id": version["version_id"],
                    "review_id": review["review_id"],
                    "expected_project_version": candidate["project_version"],
                    "idempotency_key": "publication-chain-0001",
                }
                published = self._execute_prepared(
                    fixture, "publish_version", publish_parameters
                )
                replayed = fixture.workbench.publish_version(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    confirmation_token=published["confirmation_token"],
                    **publish_parameters,
                )
            provider.assert_not_called()
            self.assertEqual(published["result"]["projection_status"], "pending")
            self.assertTrue(replayed["replayed"])
            self.assertFalse((fixture.runs_root / "CURRENT").exists())
            duplicate = {
                **publish_parameters,
                "expected_project_version": published["result"]["project_version"],
                "idempotency_key": "publication-chain-0002",
            }
            with self.assertRaisesRegex(WorkspaceServiceError, "REVIEW_REQUIRED"):
                fixture.workbench.issue_confirmation(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    tool_name="publish_version",
                    parameters=duplicate,
                )
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM version_publications"
                    ).fetchone()[0],
                    1,
                )

    def _execute_prepared(self, fixture, tool_name, parameters):
        confirmation = fixture.workbench.issue_confirmation(
            fixture.auth_token,
            agent_session_id=fixture.agent_session_id,
            tool_name=tool_name,
            parameters=parameters,
        )
        method = getattr(fixture.workbench, tool_name)
        result = method(
            fixture.auth_token,
            agent_session_id=fixture.agent_session_id,
            confirmation_token=confirmation["confirmation_token"],
            **parameters,
        )
        return {**result, "confirmation_token": confirmation["confirmation_token"]}

    def test_same_idempotency_key_rejects_parameter_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            version = fixture.add_version([{"shot_no": "1", "fields": {}, "source_refs": []}])
            parameters = self._select_parameters(fixture, version)
            _, token = self._select(fixture, parameters)
            changed = {
                **parameters,
                "expected_project_version": parameters["expected_project_version"] + 1,
            }
            with self.assertRaisesRegex(WorkspaceServiceError, "IDEMPOTENCY_CONFLICT"):
                fixture.workbench.select_candidate_version(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    confirmation_token=token,
                    **changed,
                )

    def test_publish_keeps_latest_human_quality_pass_and_candidate_gates(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            version = fixture.add_version([{"shot_no": "1", "fields": {}, "source_refs": []}])
            selected, _ = self._select(
                fixture, self._select_parameters(fixture, version)
            )
            passed = fixture.versions.record_review(
                fixture.auth_token,
                version_id=version["version_id"],
                decision="quality_pass",
                evidence_ref="artifact://review/anonymous/pass",
                expected_project_version=selected["result"]["project_version"],
            )
            failed = fixture.versions.record_review(
                fixture.auth_token,
                version_id=version["version_id"],
                decision="quality_fail",
                evidence_ref="artifact://review/anonymous/fail",
                expected_project_version=passed["project_version"],
            )
            parameters = {
                "project_id": fixture.project["project_id"],
                "version_id": version["version_id"],
                "review_id": passed["review_id"],
                "expected_project_version": failed["project_version"],
                "idempotency_key": "publication-gate-0001",
            }
            result = fixture.workbench.publish_version(
                fixture.auth_token,
                agent_session_id=fixture.agent_session_id,
                confirmation_token=fixture.issue("publish_version", parameters),
                **parameters,
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "REVIEW_REQUIRED")
            with fixture.store.connection() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM version_publications"
                ).fetchone()[0]
            self.assertEqual(count, 0)

    def test_publish_without_current_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            fixture = WorkbenchFixture(Path(tmp))
            version = fixture.add_version([{"shot_no": "1", "fields": {}, "source_refs": []}])
            review = fixture.versions.record_review(
                fixture.auth_token,
                version_id=version["version_id"],
                decision="quality_pass",
                evidence_ref="artifact://review/anonymous/pass",
                expected_project_version=fixture.project_version(),
            )
            parameters = {
                "project_id": fixture.project["project_id"],
                "version_id": version["version_id"],
                "review_id": review["review_id"],
                "expected_project_version": review["project_version"],
                "idempotency_key": "publication-no-candidate-0001",
            }
            result = fixture.workbench.publish_version(
                fixture.auth_token,
                agent_session_id=fixture.agent_session_id,
                confirmation_token=fixture.issue("publish_version", parameters),
                **parameters,
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "REVIEW_REQUIRED")
            self.assertEqual(
                result["error"]["details"]["source_code"],
                "CANDIDATE_REQUIRED",
            )
            with fixture.store.connection() as connection:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM version_publications"
                    ).fetchone()[0],
                    0,
                )

    def test_crash_after_domain_write_blocks_replay_instead_of_duplicating(self):
        with tempfile.TemporaryDirectory() as tmp:
            def crash(name):
                if name == "after_domain_write":
                    raise RuntimeError("simulated process crash")

            fixture = WorkbenchFixture(Path(tmp), checkpoint=crash)
            version = fixture.add_version([{"shot_no": "1", "fields": {}, "source_refs": []}])
            parameters = self._select_parameters(
                fixture, version, key="candidate-crash-0001"
            )
            token = fixture.issue("select_candidate_version", parameters)
            with self.assertRaisesRegex(RuntimeError, "simulated process crash"):
                fixture.workbench.select_candidate_version(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    confirmation_token=token,
                    **parameters,
                )

            restarted = VersionWorkbenchService(fixture.services)
            with self.assertRaises(VersionWorkbenchRecoveryRequired) as recovery:
                restarted.select_candidate_version(
                    fixture.auth_token,
                    agent_session_id=fixture.agent_session_id,
                    confirmation_token=token,
                    **parameters,
                )
            self.assertFalse(recovery.exception.retryable)
            self.assertFalse(recovery.exception.recovery["automatic_retry"])
            with fixture.store.connection() as connection:
                candidates = connection.execute(
                    "SELECT COUNT(*) FROM candidate_selections"
                ).fetchone()[0]
                in_progress = connection.execute(
                    "SELECT COUNT(*) FROM tool_idempotency_records "
                    "WHERE status = 'in_progress'"
                ).fetchone()[0]
            self.assertEqual(candidates, 1)
            self.assertEqual(in_progress, 1)


if __name__ == "__main__":
    unittest.main()
