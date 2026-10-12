from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from mvp_v2.creative_standard import ConfirmedCreativeStandardInput
from mvp_v2.formal_brief_domain import ArtifactPayload, SourceManifest
from mvp_v2.formal_brief_input_adapter import (
    FormalBriefInputAdapter, UploadedDocument, _employee_creative_standard,
)
from mvp_v2.formal_brief_workspace_service import (
    FormalBriefArtifactStore, FormalBriefWorkspaceService,
)
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.project_service import ProjectService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore, _MIGRATIONS, sha256_text


class CreativeStandardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mplus-cstd-")
        self.root = Path(self.temporary.name)
        self.store = WorkspaceStore(self.root / "runtime" / "workspace.sqlite3")
        owner = LocalIdentityAdapter(self.store).bootstrap_owner(
            workspace_name="匿名工作区", display_name="匿名 owner", confirmed=True,
            token="creative-standard-owner-token",
        )
        self.token, self.workspace_id = owner["auth_token"], owner["workspace_id"]
        self.projects = ProjectService(self.store)
        self.first = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名项目甲", category_id="发布会",
            expected_workspace_version=0, brief_ref="artifact://brief/" + "a" * 64,
            brief_sha256="a" * 64,
        )
        self.second = self.projects.create_project(
            self.token, workspace_id=self.workspace_id, name="匿名项目乙", category_id="发布会",
            expected_workspace_version=1, brief_ref="artifact://brief/" + "b" * 64,
            brief_sha256="b" * 64,
        )
        self.service = FormalBriefWorkspaceService(self.store, artifact_root=self.root / "runtime")

    def tearDown(self):
        self.temporary.cleanup()

    def test_import_creates_evidence_bound_draft_and_additive_migration_keeps_legacy_pointer_null(self):
        pending = self._import(self.first, expected=0)
        with self.store.connection() as connection:
            migration = connection.execute("SELECT name FROM schema_migrations WHERE version=11").fetchone()[0]
            row = connection.execute(
                "SELECT * FROM creative_standard_versions WHERE brief_version_id=?",
                (pending.brief_version_id,),
            ).fetchone()
            pointer = connection.execute(
                "SELECT current_creative_standard_version_id FROM projects WHERE project_id=?",
                (self.first["project_id"],),
            ).fetchone()[0]
        self.assertEqual(migration, "creative-standard-cards")
        self.assertIsNone(pointer)
        self.assertEqual(row["brief_version_id"], pending.brief_version_id)
        card = self.service.creative_standards.read(
            self._standard(pending.brief_version_id)
        )
        self.assertEqual(
            set(card), {"tone_keywords", "expression_focus", "avoid_expressions", "references"}
        )
        self.assertEqual(card["references"], [])
        for field in ("tone_keywords", "expression_focus", "avoid_expressions"):
            self.assertTrue(card[field])
            self.assertEqual(set(card[field][0]), {"value", "fact_id", "evidence_refs"})
            self.assertTrue(card[field][0]["evidence_refs"])
        with self.store.connection() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                connection.execute(
                    "UPDATE creative_standard_versions SET created_at=created_at WHERE creative_standard_version_id=?",
                    (row["creative_standard_version_id"],),
                )

    def test_legacy_database_upgrades_from_migrations_one_to_ten_with_null_pointer(self):
        database_path = self.root / "legacy" / "workspace.sqlite3"
        database_path.parent.mkdir()
        connection = sqlite3.connect(database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for version, name, statements in _MIGRATIONS[:10]:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, name, applied_at) VALUES (?, ?, ?)",
                    (version, name, "2026-08-13T00:00:00Z"),
                )
            connection.execute("INSERT INTO workspaces VALUES ('legacy_workspace', '匿名旧工作区', 0, '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')")
            connection.execute("INSERT INTO actors VALUES ('legacy_actor', '匿名 owner', 'local', 'active', '2026-08-13T00:00:00Z', '2026-08-13T00:00:00Z')")
            connection.execute(
                """INSERT INTO projects(
                    project_id, workspace_id, name, category_id, status, lock_version,
                    initial_brief_snapshot_id, current_brief_snapshot_id, created_by_actor_id,
                    created_at, updated_at, current_version_id
                ) VALUES ('legacy_project', 'legacy_workspace', '匿名旧项目', '发布会', 'active', 0, NULL, NULL, 'legacy_actor', ?, ?, NULL)""",
                ("2026-08-13T00:00:00Z", "2026-08-13T00:00:00Z"),
            )
            connection.commit()
        finally:
            connection.close()
        upgraded = WorkspaceStore(database_path)
        upgraded.initialize()
        with upgraded.connection() as connection:
            versions = [row[0] for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version")]
            pointer = connection.execute(
                "SELECT current_creative_standard_version_id FROM projects WHERE project_id='legacy_project'"
            ).fetchone()[0]
        self.assertEqual(versions, list(range(1, 28)))
        self.assertIsNone(pointer)

    def test_migration_eight_to_ten_pending_brief_remains_readable_but_cstd_is_unavailable_and_fail_closed(self):
        service, adapter, token, workspace_id, project_id, brief_version_id = self._upgraded_legacy_formal(confirmed=False)
        value = service.read_structured(
            token, workspace_id=workspace_id, project_id=project_id,
            brief_version_id=brief_version_id,
        )
        self.assertEqual(value["creative_standard"]["status"], "unavailable")
        self.assertEqual(value["creative_standard"]["reason"], "requires_reimport")
        self.assertTrue(value["facts"])
        employee = adapter.read_structured(
            token, workspace_id=workspace_id, project_id=project_id,
            brief_version_id=brief_version_id,
        )
        self.assertEqual(employee["creative_standard"], {
            "status": "unavailable", "needs_clarification": True,
            "reason": "requires_reimport", "message": "请重新导入资料以生成标准卡",
            "confirmed_at": None,
        })
        self.assertTrue(employee["brief"]["facts"])
        self.assertTrue(employee["brief"]["documents"])
        before = self._legacy_cstd_counts(service.store, project_id)
        self.assertEqual(before, (0, 0, None))
        with self.assertRaisesRegex(WorkspaceServiceError, "CREATIVE_STANDARD_MISSING"):
            service.confirm(
                token, workspace_id=workspace_id, project_id=project_id,
                brief_version_id=brief_version_id, confirmed_at="2026-08-13T00:00:00Z",
                expected_project_version=0,
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "CREATIVE_STANDARD_MISSING"):
            service.resolve_current_paired(token, workspace_id=workspace_id, project_id=project_id)
        self.assertEqual(self._legacy_cstd_counts(service.store, project_id), before)

    def test_migration_eight_to_ten_confirmed_brief_is_read_only_but_pair_and_current_read_remain_blocked(self):
        service, adapter, token, workspace_id, project_id, brief_version_id = self._upgraded_legacy_formal(confirmed=True)
        employee = adapter.read_structured(
            token, workspace_id=workspace_id, project_id=project_id,
            brief_version_id=brief_version_id,
        )
        self.assertEqual(employee["confirmation_status"], "confirmed")
        self.assertEqual(employee["creative_standard"]["status"], "unavailable")
        before = self._legacy_cstd_counts(service.store, project_id)
        self.assertEqual(before, (0, 0, None))
        with self.assertRaisesRegex(WorkspaceServiceError, "ALREADY_CONFIRMED"):
            service.confirm(
                token, workspace_id=workspace_id, project_id=project_id,
                brief_version_id=brief_version_id, confirmed_at="2026-08-13T00:00:00Z",
                expected_project_version=0,
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "CREATIVE_STANDARD_DRIFT"):
            service.resolve_current_paired(token, workspace_id=workspace_id, project_id=project_id)
        with self.assertRaisesRegex(WorkspaceServiceError, "CREATIVE_STANDARD_DRIFT"):
            service.read_current_formal(token, workspace_id=workspace_id, project_id=project_id)
        self.assertEqual(self._legacy_cstd_counts(service.store, project_id), before)

    def test_confirm_allows_missing_optional_avoid_expression_and_blocks_tampered_card(self):
        incomplete = self._import(self.first, expected=0, include_avoid=False)
        self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=incomplete.brief_version_id, confirmed_at="2026-08-13T00:00:00Z",
            expected_project_version=1,
        )
        paired = self.service.resolve_current_paired(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
        )
        self.assertEqual(
            self.service.creative_standard_resolver().resolve(paired.creative_standard)["avoid_expressions"],
            [],
        )

        pending = self._import(self.second, expected=0)
        standard = self._standard(pending.brief_version_id)
        path = self.service.creative_standard_artifacts.root / standard.payload.sha256[:2] / f"{standard.payload.sha256}.json"
        path.write_bytes(b"tampered")
        before = self._counts()
        with self.assertRaisesRegex(WorkspaceServiceError, "ARTIFACT_HASH_MISMATCH"):
            self.service.confirm(
                self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
                brief_version_id=pending.brief_version_id, confirmed_at="2026-08-13T00:00:01Z",
                expected_project_version=1,
            )
        self.assertEqual(self._counts(), before)
        self.assertIsNone(self.projects.get_project(self.token, self.second["project_id"])["current_creative_standard_version_id"])

    def test_paired_resolver_restarts_rejects_mapping_and_detects_project_drift(self):
        pending = self._import(self.first, expected=0)
        self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=pending.brief_version_id, confirmed_at="2026-08-13T00:00:00Z",
            expected_project_version=1,
        )
        paired = self.service.resolve_current_paired(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"]
        )
        self.assertEqual(paired.brief.brief_version_id, pending.brief_version_id)
        self.assertEqual(paired.creative_standard.confirmation_status, "confirmed")
        authority_mapping = paired.creative_standard.as_mapping()
        self.assertIn("schema_version", authority_mapping)
        self.assertEqual(authority_mapping["confirmation_status"], "confirmed")
        self.assertNotIn("status", authority_mapping)
        with self.store.connection() as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "IMMUTABLE_RECORD"):
                connection.execute(
                    "UPDATE creative_standard_confirmations SET confirmed_at=confirmed_at WHERE creative_standard_version_id=?",
                    (paired.creative_standard.creative_standard_version_id,),
                )
            brief_confirmation = connection.execute(
                "SELECT confirmation_id FROM brief_confirmations WHERE brief_version_id=?",
                (pending.brief_version_id,),
            ).fetchone()[0]
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CREATIVE_STANDARD_CONFIRMATION_BINDING_MISMATCH"):
                connection.execute(
                    """INSERT INTO creative_standard_confirmations(
                        creative_standard_confirmation_id, creative_standard_version_id,
                        brief_confirmation_id, brief_snapshot_id, workspace_id, project_id,
                        confirmed_by_actor_id, confirmed_at
                    ) VALUES ('forged_standard_confirmation', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        paired.creative_standard.creative_standard_version_id,
                        brief_confirmation, paired.brief.brief_snapshot_id,
                        self.workspace_id, self.second["project_id"], paired.creative_standard.confirmed_by_actor_id,
                        "2026-08-13T00:00:01Z",
                    ),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "CREATIVE_STANDARD_CURRENT_BINDING_MISMATCH"):
                connection.execute(
                    "UPDATE projects SET current_creative_standard_version_id=? WHERE project_id=?",
                    (paired.creative_standard.creative_standard_version_id, self.second["project_id"]),
                )
        with self.assertRaises(TypeError):
            self.service.creative_standard_resolver().resolve(  # type: ignore[arg-type]
                paired.creative_standard.as_mapping()
            )
        restarted = FormalBriefWorkspaceService(
            WorkspaceStore(self.store.database_path), artifact_root=self.root / "runtime"
        )
        self.assertEqual(
            restarted.resolve_current_paired(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"]
            ).creative_standard.creative_standard_version_id,
            paired.creative_standard.creative_standard_version_id,
        )
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE projects SET current_creative_standard_version_id=NULL WHERE project_id=?",
                (self.first["project_id"],),
            )
        with self.assertRaisesRegex(WorkspaceServiceError, "CREATIVE_STANDARD_DRIFT"):
            self.service.resolve_current_paired(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"]
            )

    def test_new_brief_supersedes_old_card_and_employee_dto_excludes_internal_fields(self):
        first = self._import(self.first, expected=0)
        self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=first.brief_version_id, confirmed_at="2026-08-13T00:00:00Z",
            expected_project_version=1,
        )
        old = self._standard(first.brief_version_id)
        second = self._import(self.first, expected=2, suffix="二")
        self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=second.brief_version_id, confirmed_at="2026-08-13T00:00:01Z",
            expected_project_version=3,
        )
        self.assertEqual(
            self.service.creative_standard_status(
                self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
                creative_standard_version_id=old.creative_standard_version_id,
            ),
            "superseded",
        )
        value = self.service.read_current_formal(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"]
        )
        dto = _employee_creative_standard(value["creative_standard"], value["brief"])
        self.assertEqual(set(dto), {"tone_keywords", "expression_focus", "avoid_expressions", "references", "references_note", "status", "confirmed_at"})
        rendered = json.dumps(dto, ensure_ascii=False)
        for forbidden in ("workspace", "project", "actor", "_id", "artifact://", "sha256", "path", "provider", "model"):
            self.assertNotIn(forbidden, rendered)

    def test_employee_dto_shows_pending_draft_and_confirmation_changes_same_shape(self):
        adapter = FormalBriefInputAdapter(
            self.service, data_root=self.root / "runtime",
        )
        common = (
            "风格调性: 克制可信\n视频目标: 说明重点\n"
            "必须出现的内容: 正式产品演示\n关键事件与流程: 入场与发布\n"
            "禁止或不能出现的内容: 避免无依据夸张"
        )
        pending = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            expected_project_version=0,
            uploads=[
                UploadedDocument("brief-a.txt", BytesIO(("项目名称: 匿名甲\n" + common).encode("utf-8"))),
                UploadedDocument("brief-b.txt", BytesIO(("项目名称: 匿名乙\n" + common).encode("utf-8"))),
            ],
        )
        draft = pending["creative_standard"]
        self.assertEqual(draft["status"], "draft")
        self.assertIsNone(draft["confirmed_at"])
        self.assertTrue(draft["tone_keywords"][0]["sources"])
        with self.store.connection() as connection:
            first_card = connection.execute(
                "SELECT creative_standard_version_id FROM creative_standard_versions WHERE brief_version_id=?",
                (pending["brief_key"],),
            ).fetchone()[0]
        conflict = pending["brief"]["conflicts"][0]
        resolved = adapter.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=pending["brief_key"], conflict_id=conflict["conflict_key"],
            selected_fact_id=conflict["choices"][0]["fact_key"], expected_project_version=1,
        )
        self.assertNotEqual(resolved["brief_key"], pending["brief_key"])
        self.assertEqual(resolved["creative_standard"]["status"], "draft")
        with self.store.connection() as connection:
            resolved_card = connection.execute(
                "SELECT creative_standard_version_id FROM creative_standard_versions WHERE brief_version_id=?",
                (resolved["brief_key"],),
            ).fetchone()[0]
        self.assertNotEqual(resolved_card, first_card)
        confirmed = adapter.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=resolved["brief_key"], confirmed_at="2026-08-13T00:00:00Z",
            expected_project_version=2,
        )
        self.assertEqual(set(confirmed["creative_standard"]), set(draft))
        self.assertEqual(confirmed["creative_standard"]["status"], "confirmed")

        incomplete = adapter.import_uploads(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
            expected_project_version=0,
            uploads=[UploadedDocument("incomplete.txt", BytesIO("项目名称: 匿名第二项目".encode("utf-8")))],
        )
        self.assertEqual(incomplete["creative_standard"]["status"], "draft")
        self.assertTrue(incomplete["creative_standard"]["needs_clarification"])
        self.assertEqual(
            set(incomplete["creative_standard"]["missing_fields"]),
            {"tone_keywords", "expression_focus"},
        )
        before = self._counts()
        with self.assertRaisesRegex(WorkspaceServiceError, "CREATIVE_STANDARD_NEEDS_CLARIFICATION"):
            adapter.confirm(
                self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
                brief_version_id=incomplete["brief_key"], confirmed_at="2026-08-13T00:00:01Z",
                expected_project_version=1,
            )
        self.assertEqual(self._counts(), before)

    def test_reference_projection_keeps_evidence_and_blocks_a_supplied_reference_without_confirmed_evidence(self):
        pending = self._import(self.first, expected=0, reference=("参考方案", "匿名既有脚本"))
        card = self.service.creative_standards.read(self._standard(pending.brief_version_id))
        self.assertEqual(card["references"][0]["value"], "匿名既有脚本")
        self.assertTrue(card["references"][0]["evidence_refs"])
        pending_unverified = self._import(
            self.second, expected=0,
            reference=("参考图片", "匿名参考图片"), reference_confidence="inferred",
        )
        value = self.service.read_structured(
            self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
            brief_version_id=pending_unverified.brief_version_id,
        )
        self.assertIn("references", value["creative_standard"]["missing_fields"])
        before = self._counts()
        with self.assertRaisesRegex(WorkspaceServiceError, "CREATIVE_STANDARD_NEEDS_CLARIFICATION"):
            self.service.confirm(
                self.token, workspace_id=self.workspace_id, project_id=self.second["project_id"],
                brief_version_id=pending_unverified.brief_version_id,
                confirmed_at="2026-08-13T00:00:02Z", expected_project_version=1,
            )
        self.assertEqual(self._counts(), before)

    def test_resolved_reference_conflict_requires_only_selected_fact_and_unprojected_selection_blocks(self):
        pending = self._import(
            self.first, expected=0, reference_choices=("匿名方案甲", "匿名方案乙"),
        )
        conflict = pending.payload_data["conflicts"][0]
        resolved = self.service.resolve_conflict(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=pending.brief_version_id, conflict_id=conflict["conflict_id"],
            selected_fact_id=conflict["fact_ids"][0], expected_project_version=1,
        )
        standard = self._standard(resolved.brief_version_id)
        self.assertNotIn("references", self.service.creative_standards.confirmation_gaps(standard))
        self.service.confirm(
            self.token, workspace_id=self.workspace_id, project_id=self.first["project_id"],
            brief_version_id=resolved.brief_version_id, confirmed_at="2026-08-13T00:00:03Z",
            expected_project_version=2,
        )
        unprojected = self._import(
            self.second, expected=0, reference=("参考方案", "匿名待核参考"), reference_confidence="inferred",
        )
        self.assertIn(
            "references", self.service.creative_standards.confirmation_gaps(
                self._standard(unprojected.brief_version_id)
            ),
        )

    def _import(self, project: dict, *, expected: int, include_avoid: bool = True, suffix: str = "", reference: tuple[str, str] | None = None, reference_confidence: str = "direct", reference_choices: tuple[str, str] | None = None):
        digest = ("d" if suffix else "c") * 64
        manifest = SourceManifest.from_mapping({
            "manifest": {"ref": "artifact://manifest/" + digest, "sha256": digest},
            "documents": [{"source_document_id": "anonymous_doc" + suffix, "relative_path": f"inputs/anonymous{suffix}.txt", "content_sha256": digest, "critical": True}],
        })
        document_id = "anonymous_doc" + suffix
        evidence = f"artifact://evidence/anonymous/page/{1 if not suffix else 2}"
        facts = [
            ("项目名称", "匿名项目" + suffix), ("风格调性", "克制可信"),
            ("视频目标", "说明正式重点"), ("必须出现的内容", "正式产品演示"),
            ("关键事件与流程", "入场与发布"),
        ]
        if include_avoid:
            facts.append(("禁止或不能出现的内容", "避免无依据夸张"))
        if reference is not None:
            facts.append(reference)
        if reference_choices is not None:
            facts.extend(("参考方案", value) for value in reference_choices)
        formal_facts = [{"fact_id": f"fact_{index}_{project['project_id'][-8:]}_{suffix or 'one'}", "field": field, "value": value, "confidence": reference_confidence if reference is not None and (field, value) == reference else "direct", "evidence_refs": [evidence]} for index, (field, value) in enumerate(facts)]
        conflicts = []
        if reference_choices is not None:
            selected = [item["fact_id"] for item in formal_facts if item["field"] == "参考方案"]
            conflicts.append({"conflict_id": "reference_conflict", "field": "参考方案", "fact_ids": selected, "status": "unresolved"})
        extracted = {
            "project_id": project["project_id"],
            "source_documents": [{"document_id": document_id, "project_id": project["project_id"], "filename": "anonymous.txt", "mime_type": "text/plain", "size_bytes": 24, "sha256": digest, "critical": True}],
            "extractions": [{"document_id": document_id, "status": "succeeded", "critical": True, "error": None, "locations": [{"evidence_ref": evidence, "location": {"page": 1}, "text": "匿名正式 Brief"}]}],
            "images": [],
            "facts": formal_facts,
            "conflicts": conflicts,
        }
        return self.service.import_frozen(
            self.token, workspace_id=self.workspace_id, project_id=project["project_id"],
            source_manifest=manifest, source_artifacts={document_id: ArtifactPayload("artifact://source/" + digest, digest)},
            extracted=extracted, expected_project_version=expected,
        )

    def _standard(self, brief_version_id: str):
        with self.store.connection() as connection:
            row = connection.execute(
                "SELECT * FROM creative_standard_versions WHERE brief_version_id=?", (brief_version_id,)
            ).fetchone()
        from mvp_v2.creative_standard import CreativeStandardVersion
        return CreativeStandardVersion(
            row["workspace_id"], row["project_id"], row["creative_standard_version_id"],
            row["brief_version_id"], ArtifactPayload(row["brief_payload_ref"], row["brief_payload_sha256"]),
            ArtifactPayload(row["payload_ref"], row["payload_sha256"]), row["content_sha256"], {},
            row["created_by_actor_id"], row["created_at"],
        )

    def _counts(self):
        with self.store.connection() as connection:
            return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in (
                "project_brief_snapshots", "brief_confirmations", "creative_standard_confirmations"
            ))

    def _legacy_cstd_counts(self, store: WorkspaceStore, project_id: str) -> tuple[int, int, object]:
        with store.connection() as connection:
            return (
                connection.execute("SELECT COUNT(*) FROM creative_standard_versions").fetchone()[0],
                connection.execute("SELECT COUNT(*) FROM creative_standard_confirmations").fetchone()[0],
                connection.execute("SELECT current_creative_standard_version_id FROM projects WHERE project_id=?", (project_id,)).fetchone()[0],
            )

    def _upgraded_legacy_formal(self, *, confirmed: bool):
        root = self.root / ("legacy-confirmed" if confirmed else "legacy-pending")
        database_path = root / "runtime" / "workspace.sqlite3"
        database_path.parent.mkdir(parents=True)
        connection = sqlite3.connect(database_path)
        timestamp = "2026-08-13T00:00:00Z"
        token, workspace_id, actor_id, project_id, brief_version_id = (
            "legacy-token", "legacy_workspace", "legacy_actor", "legacy_project", "legacy_brief_version",
        )
        source_bytes = b"anonymous legacy brief"
        source_sha = sha256(source_bytes).hexdigest()
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
            for version, name, statements in _MIGRATIONS[:10]:
                for statement in statements:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, name, timestamp))
            WorkspaceStore._seed_permissions(connection)
            connection.execute("INSERT INTO workspaces VALUES (?, '匿名旧工作区', 0, ?, ?)", (workspace_id, timestamp, timestamp))
            connection.execute("INSERT INTO actors VALUES (?, '匿名 owner', 'local', 'active', ?, ?)", (actor_id, timestamp, timestamp))
            connection.execute("INSERT INTO auth_sessions VALUES ('legacy_auth', ?, ?, ?, NULL, NULL)", (actor_id, sha256_text(token), timestamp))
            connection.execute("INSERT INTO role_assignments VALUES ('legacy_assignment', ?, ?, NULL, 'administrator', ?, ?)", (actor_id, workspace_id, actor_id, timestamp))
            connection.execute("""INSERT INTO projects(
                project_id, workspace_id, name, category_id, status, lock_version,
                initial_brief_snapshot_id, current_brief_snapshot_id, created_by_actor_id,
                created_at, updated_at, current_version_id
            ) VALUES (?, ?, '匿名旧项目', '发布会', 'active', 0, NULL, NULL, ?, ?, ?, NULL)""", (project_id, workspace_id, actor_id, timestamp, timestamp))
            connection.execute("""INSERT INTO project_files VALUES (
                'legacy_file', ?, ?, 'legacy_doc', 'inputs/legacy.txt', 'legacy.txt', 'text/plain',
                ?, ?, 'artifact://source/" + source_sha + "', 'extracted', 1, ?, ?
            )""", (workspace_id, project_id, len(source_bytes), source_sha, actor_id, timestamp))
            connection.execute("INSERT INTO document_extractions VALUES ('legacy_extraction', 'legacy_file', 'succeeded', 1, NULL, NULL, ?)", (timestamp,))
            evidence = {"artifact_ref": "artifact://evidence/legacy/page/1", "source_document_id": "legacy_doc", "location": {"page": 1}, "content_sha256": source_sha}
            brief = {
                "project_id": project_id,
                "source_documents": [{"document_id": "legacy_doc", "project_id": project_id, "filename": "legacy.txt", "mime_type": "text/plain", "size_bytes": len(source_bytes), "sha256": source_sha, "critical": True}],
                "extractions": [{"document_id": "legacy_doc", "status": "succeeded", "critical": True, "error": None, "locations": [{"evidence": evidence, "text": "匿名旧 Brief"}]}],
                "images": [], "conflicts": [],
                "facts": [{"fact_id": "legacy_fact", "field_path": "项目名称", "value_json": '"匿名旧项目"', "certainty": "confirmed", "confidence": "high", "evidence_refs": [evidence]}],
            }
            raw = json.dumps({"schema_version": "mplus-formal-brief/v1", "workspace_id": workspace_id, "project_id": project_id, "brief_version_id": brief_version_id, "brief": brief}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
            payload = FormalBriefArtifactStore(root / "runtime").write(raw)
            connection.execute(
                "INSERT INTO project_brief_snapshots VALUES ('legacy_initial_snapshot', ?, ?, ?, ?, ?)",
                (project_id, payload.ref, payload.sha256, actor_id, timestamp),
            )
            connection.execute(
                "UPDATE projects SET initial_brief_snapshot_id='legacy_initial_snapshot' WHERE project_id=?",
                (project_id,),
            )
            connection.execute("""INSERT INTO brief_versions VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)""", (
                brief_version_id, workspace_id, project_id, "artifact://manifest/" + source_sha, source_sha,
                '["legacy_doc"]', payload.ref, payload.sha256, actor_id, timestamp,
            ))
            connection.execute("INSERT INTO brief_facts VALUES ('legacy_fact', ?, '项目名称', '" + '\"匿名旧项目\"' + "', 'confirmed', 'high', ?)", (brief_version_id, json.dumps([evidence], ensure_ascii=False)))
            if confirmed:
                connection.execute("INSERT INTO brief_confirmations VALUES ('legacy_confirmation', ?, 'legacy_initial_snapshot', ?, ?, 'confirmed', 0, ?, ?)", (brief_version_id, workspace_id, project_id, actor_id, timestamp))
                connection.execute("UPDATE projects SET current_brief_snapshot_id='legacy_initial_snapshot' WHERE project_id=?", (project_id,))
            connection.commit()
        finally:
            connection.close()
        store = WorkspaceStore(database_path)
        store.initialize()
        service = FormalBriefWorkspaceService(store, artifact_root=root / "runtime")
        adapter = FormalBriefInputAdapter(service, data_root=root / "runtime")
        adapter.store.file_path(source_sha, ".txt").parent.mkdir(parents=True, exist_ok=True)
        adapter.store.file_path(source_sha, ".txt").write_bytes(source_bytes)
        return service, adapter, token, workspace_id, project_id, brief_version_id


if __name__ == "__main__":
    unittest.main()
