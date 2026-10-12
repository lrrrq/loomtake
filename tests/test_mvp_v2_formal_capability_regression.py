from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mvp_v2.artifact_service import ArtifactResolutionError
from mvp_v2.formal_default_images import FormalDefaultImageService
from mvp_v2.formal_image_candidates import (
    resolve_legacy_visual_identity,
    search_package_candidates,
    search_package_library_candidates,
)
from mvp_v2.visual_assets import _strict_primary_visual_missing
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore, _MIGRATIONS


class FormalCapabilityRegressionTests(unittest.TestCase):
    def test_default_image_resolver_failure_is_a_workspace_error(self):
        service = object.__new__(FormalDefaultImageService)
        service.package_registry = None

        def failed_resolver(_script_type):
            raise ValueError("anonymous resolver failure")

        service.asset_package_resolver = failed_resolver
        with self.assertRaises(WorkspaceServiceError) as caught:
            service._package_media(
                {"category_id": "launch"}, {"脚本类型": "发布会"},
            )
        self.assertEqual(caught.exception.code, "ASSET_PACKAGE_RESOLVER_INVALID")

    def test_default_image_preview_keeps_21_ready_shots_when_one_of_22_has_a_gap(self):
        service = FormalDefaultImageService(
            projects=None, tasks=None, delivery=None, artifacts=None,
            image_pool=None, image_workspace=None,
            asset_package_resolver=lambda _script_type: None, writes_enabled=False,
        )

        def initial_version_has_no_revision_report(_run_id, _artifact_key):
            raise ArtifactResolutionError("NOT_FOUND", "run artifact 不存在")

        service.artifacts = SimpleNamespace(
            resolve_artifact=initial_version_has_no_revision_report,
        )
        project = {
            "project_id": "project-anonymous",
            "workspace_id": "workspace-anonymous",
            "project_version": 7,
            "category_id": "star_activity",
        }
        rows = [
            {"镜号": str(index), "画面描述": f"匿名镜头 {index}"}
            for index in range(1, 23)
        ]
        media = [{
            "media_id": f"media-{index}",
            "content_sha256": f"{index:064x}",
            "path": f"/anonymous/media-{index}.png",
            "caption": f"匿名素材 {index}",
            "tags": ["匿名"],
            "source": {"file": "匿名.xlsx", "sheet": "素材", "row": index},
            "source_links": [{
                "source_file": "匿名脚本库.xlsx",
                "source_sheet": "创意库",
                "source_row": index,
                "shot_no": str(index),
                "structure": "主体",
                "shot_function": "产品展示",
            }],
        } for index in range(1, 78)]
        package = {
            "package_id": "anonymous-star",
            "package_version": 4,
            "category_id": "star_activity",
            "manifest_path": Path(__file__),
            "verified_manifest_sha256": "f" * 64,
        }
        service._context = lambda *_args: (
            project,
            SimpleNamespace(
                project_id=project["project_id"], version_id="version-anonymous", run_id="run-anonymous",
            ),
            SimpleNamespace(brief={"脚本类型": "明星活动"}),
            SimpleNamespace(script_rows=rows),
            SimpleNamespace(bindings=[]),
        )
        service._package_media = lambda *_args: (package, media)

        def authoritative_result(*, slot, **_kwargs):
            if slot["镜号"] == "14":
                return {
                    "candidates": [],
                    "rejected": [
                        {"reason": "缺少必须画面主体: 集体合影"},
                        {"reason": "缺少必须画面主体: 集体合影"},
                        {"reason": "来源品牌与 Brief 冲突"},
                    ],
                    "gap_reason": "没有达到自动配图阈值的素材",
                }
            item = media[int(slot["镜号"]) - 1]
            return {
                "candidates": [{
                    "image_id": item["media_id"],
                    "duplicate_group_id": item["media_id"],
                    "summary": item["caption"],
                    "tags": item["tags"],
                    "score": 88,
                    "match_reason": "权威匹配依据",
                    "hits": ["产品展示"],
                    "missing": [],
                    "conflicts": [],
                    "brand_warning": "",
                    "requires_human_review": True,
                    "rank_components": {},
                }],
                "rejected": [],
                "gap_reason": "",
            }

        with patch(
            "mvp_v2.formal_default_images.search_package_candidates",
            side_effect=authoritative_result,
        ):
            preview = service.candidates(
                "token",
                workspace_id="workspace-anonymous",
                project_id="project-anonymous",
                expected_project_version=7,
            )
        self.assertEqual(preview["state"], "partial")
        self.assertEqual(preview["base_version_id"], "version-anonymous")
        self.assertEqual(len({item["shot_id"] for item in preview["shots"]}), 22)
        self.assertEqual(
            (preview["shot_count"], preview["ready_shot_count"], preview["gap_shot_count"]),
            (22, 21, 1),
        )
        gap = next(item for item in preview["shots"] if item["shot_no"] == "14")
        self.assertEqual(gap["state"], "gap")
        self.assertEqual(gap["candidates"], [])
        self.assertIn("集体合影", gap["gap_reason"])
        self.assertIn("2 项", gap["gap_reason"])
        self.assertTrue(gap["supplement_upload_available"])
        self.assertTrue(gap["temporary_text_basis_available"])
        candidate = next(item for item in preview["shots"] if item["state"] == "ready")["candidates"][0]
        self.assertEqual(len(candidate["content_sha256"]), 64)
        self.assertEqual(candidate["source_category"], "创意库")
        self.assertEqual(candidate["sources"][0], {
            "file": "匿名脚本库.xlsx", "sheet": "创意库", "row": 1,
        })
        self.assertEqual(candidate["hits"], ["产品展示"])
        self.assertEqual(candidate["missing"], [])
        explanation = candidate["match_explanation"]
        self.assertEqual(explanation["total_score"], 88)
        self.assertEqual(explanation["components"], {})
        self.assertEqual(explanation["source_category"], "创意库")
        self.assertEqual(explanation["sources"], candidate["sources"])
        for field in (
            "semantic", "safety", "reuse", "near_duplicate", "recommendation",
        ):
            self.assertTrue(explanation[field])

    def test_group_photo_contract_accepts_multiple_people_but_not_generic_two_person_photo(self):
        self.assertEqual(
            _strict_primary_visual_missing(
                "艺人与嘉宾完成集体合影",
                "多人合影形成活动高潮",
                ["多人合影"],
            ),
            [],
        )
        self.assertEqual(
            _strict_primary_visual_missing(
                "艺人与嘉宾完成集体合影",
                "两位嘉宾进行普通合影",
                ["双人", "合影"],
            ),
            ["集体合影"],
        )

    def test_migrations_17_and_18_upgrade_1_through_16_and_reinitialize_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                for version, name, statements in _MIGRATIONS[:16]:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, "2026-08-25T00:00:00Z"),
                    )
                connection.commit()
            finally:
                connection.close()

            store = WorkspaceStore(path)
            store.initialize()
            store.initialize()
            with store.connection() as connection:
                versions = [row[0] for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )]
            self.assertEqual(versions, list(range(1, 28)))

    def test_migration_17_adds_immutable_completion_feedback_and_v3_decisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp) / "workspace.sqlite3")
            store.initialize()
            with store.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT name FROM schema_migrations WHERE version=17").fetchone()[0],
                    "formal-capability-regression-v3",
                )
                objects = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
                    )
                }
            for name in (
                "version_completion_states", "version_image_decision_snapshots_v3",
                "version_image_decisions_v3", "formal_version_feedback_records",
                "formal_visual_identity_mappings", "version_completion_states_no_update",
                "version_completion_states_no_delete",
            ):
                self.assertIn(name, objects)

    def test_migration_18_upgrades_an_existing_schema_17_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                for version, name, statements in _MIGRATIONS[:17]:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, "2026-08-25T00:00:00Z"),
                    )
                connection.commit()
            finally:
                connection.close()

            store = WorkspaceStore(path)
            store.initialize()
            with store.connection() as connection:
                self.assertEqual(
                    connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0],
                    26,
                )
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertIn("task_attempts", tables)
            self.assertIn("stage_checkpoints", tables)

    def test_migration_19_upgrades_an_existing_schema_18_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "workspace.sqlite3"
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
                )
                for version, name, statements in _MIGRATIONS[:18]:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                        (version, name, "2026-08-25T00:00:00Z"),
                    )
                connection.commit()
            finally:
                connection.close()
            store = WorkspaceStore(path)
            store.initialize()
            with store.connection() as connection:
                version = connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
                tables = {
                    row[0] for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(version, 26)
            self.assertIn("delivery_snapshots", tables)
            self.assertIn("delivery_artifact_bindings", tables)

    def test_migration_17_completion_state_rejects_update_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkspaceStore(Path(tmp) / "workspace.sqlite3")
            store.initialize()
            now = "2026-08-25T00:00:00Z"
            with store.connection() as connection:
                connection.execute("INSERT INTO workspaces VALUES ('workspace_test', '匿名工作区', 0, ?, ?)", (now, now))
                connection.execute("INSERT INTO actors VALUES ('actor_test', '匿名用户', 'local', 'active', ?, ?)", (now, now))
                connection.execute(
                    """INSERT INTO projects(project_id, workspace_id, name, category_id, status, lock_version,
                    created_by_actor_id, created_at, updated_at) VALUES ('project_test', 'workspace_test',
                    '匿名项目', 'launch', 'active', 0, 'actor_test', ?, ?)""",
                    (now, now),
                )
                connection.execute(
                    "INSERT INTO project_runs VALUES ('run_test', 'project_test', NULL, 'native', 'artifact://run/test/manifest', ?, 'actor_test', ?)",
                    ("a" * 64, now),
                )
                connection.execute(
                    "INSERT INTO project_versions VALUES ('version_test', 'project_test', 'run_test', NULL, ?, 'artifact://run/test/version', 'actor_test', ?)",
                    ("b" * 64, now),
                )
                connection.execute(
                    """INSERT INTO version_completion_states VALUES (
                    'completion_test', 'workspace_test', 'project_test', 'version_test',
                    'text_only_draft', 'images_pending', 0, 0,
                    'artifact://run/test/completion', ?, 'actor_test', ?)""",
                    ("c" * 64, now),
                )
                connection.commit()
                with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                    connection.execute(
                        "UPDATE version_completion_states SET image_state='temporary_text_basis' WHERE completion_state_id='completion_test'"
                    )
                with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                    connection.execute(
                        "DELETE FROM version_completion_states WHERE completion_state_id='completion_test'"
                    )

    def test_formal_candidates_delegate_ranking_and_exclusion_to_authoritative_search(self):
        media = [{
            "media_id": "media-1", "content_sha256": "a" * 64,
            "path": "/tmp/media-1.png", "caption": "安全现场", "tags": ["现场"],
            "subjects": ["嘉宾"], "scene": "会场", "brand": "匿名品牌",
            "source": {"file": "匿名.xlsx", "sheet": "模板", "row": 7},
        }]
        authoritative = {
            "status": "review", "gap_reason": "", "rejected": [],
            "candidates": [{
                "image_id": "media-1", "summary": "安全现场", "tags": ["现场"],
                "score": 70, "match_reason": "权威匹配依据", "conflicts": [],
                "brand_warning": "", "requires_human_review": True, "rank_components": {},
            }],
        }
        with patch(
            "mvp_v2.formal_image_candidates.search_images_for_slot",
            return_value=authoritative,
        ) as search:
            result = search_package_candidates(
                slot={"slot_index": 1, "画面描述": "会场"},
                brief={"脚本类型": "发布会", "禁忌或不能出现的内容": "烟酒"},
                media=media, excluded_visuals=("media-old",),
            )
        self.assertEqual(result, authoritative)
        self.assertEqual(search.call_args.kwargs["excluded_visuals"], ("media-old",))
        manifest = search.call_args.args[2][0]
        self.assertEqual((manifest["image_id"], manifest["来源文件"], manifest["来源 sheet"], manifest["来源行"]), (
            "media-1", "匿名.xlsx", "模板", 7,
        ))

    def test_preplan_library_uses_source_links_and_limits_visible_candidates(self):
        media = [{
            "media_id": f"media-{index:02d}",
            "content_sha256": f"{index:064x}",
            "path": f"/anonymous/media-{index:02d}.png",
            "caption": f"匿名创意画面 {index}",
            "tags": ["匿名", "创意"],
            "source": {"file": "匿名脚本库.xlsx", "sheet": "创意库", "row": index + 1},
            "source_links": [{
                "source_file": "匿名脚本库.xlsx",
                "source_sheet": "创意库",
                "source_row": index + 1,
                "structure": "开场",
                "shot_no": str(index),
                "visual_desc": f"匿名创意画面 {index}",
                "shot_function": "主视觉",
            }],
        } for index in range(1, 21)]

        def authoritative(slot, _brief, manifest, _analysis, *, top_k, **_kwargs):
            self.assertEqual(slot["采用来源 sheet"], "创意库")
            self.assertEqual(len(manifest), 1)
            self.assertEqual(manifest[0]["来源行"], slot["采用来源行"])
            return {
                "status": "review", "gap_reason": "", "rejected": [],
                "candidates": [{
                    "image_id": item["image_id"], "summary": "匿名安全候选",
                    "tags": ["匿名"], "score": 70,
                    "match_reason": "权威匹配依据", "conflicts": [],
                    "brand_warning": "", "requires_human_review": True,
                    "rank_components": {},
                } for item in manifest[:top_k]],
            }

        with patch(
            "mvp_v2.formal_image_candidates.search_images_for_slot",
            side_effect=authoritative,
        ) as search:
            result = search_package_library_candidates(
                brief={"脚本类型": "发布会"}, media=media, top_k=12,
            )

        self.assertGreater(search.call_count, 0)
        self.assertEqual(len(result["candidates"]), 12)
        self.assertTrue(all(len(item["content_sha256"]) == 64 for item in result["candidates"]))

    def test_legacy_identity_never_uses_basename_or_fuzzy_path(self):
        media = [{
            "media_id": "stable-media", "content_sha256": "b" * 64,
            "path": "/controlled/exact/image.png",
        }]
        self.assertEqual(
            resolve_legacy_visual_identity({"image_id": "stable-media"}, media)["resolution_kind"],
            "media_id",
        )
        self.assertEqual(
            resolve_legacy_visual_identity({"content_sha256": "b" * 64}, media)["resolution_kind"],
            "content_sha256",
        )
        fuzzy = resolve_legacy_visual_identity({"original_path": "/other/image.png"}, media)
        self.assertEqual((fuzzy["status"], fuzzy["media_id"]), ("ambiguous", ""))

    def test_page_local_character_matchers_are_removed(self):
        root = Path(__file__).parents[1] / "mvp_v2"
        self.assertNotIn("_match_score", (root / "formal_default_images.py").read_text(encoding="utf-8"))
        self.assertNotIn("_business_match_count", (root / "formal_image_workspace.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
