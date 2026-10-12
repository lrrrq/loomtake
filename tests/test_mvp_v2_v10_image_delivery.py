from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook
from pypdf import PdfReader

from mvp_v2.artifact_service import RunArtifactResolver
from mvp_v2.identity_service import LocalIdentityAdapter, require_permission
from mvp_v2.project_service import ProjectService
from mvp_v2.version_delivery import VersionDeliveryService
from mvp_v2.version_quality import AuthenticatedFinalConfirmationService
from mvp_v2.version_quality_artifacts import FileVersionQualityArtifactStore
from mvp_v2.version_quality_factory import build_workspace_version_quality_resolver
from mvp_v2.version_review_service import InMemoryVersionReviewRepository, VersionReviewService
from mvp_v2.workspace_store import WorkspaceServiceError, WorkspaceStore
from mvp_v2.v10_image_delivery import (
    V10ImageDeliveryError,
    create_v10_image_delivery,
    create_v10_pdf_delivery,
)


@dataclass(frozen=True)
class _Artifact:
    ref: str
    sha256: str


@dataclass(frozen=True)
class _Artifacts:
    script_plan: _Artifact
    script_draft: _Artifact
    visual_binding: _Artifact


@dataclass(frozen=True)
class _Context:
    schema_version: str
    workspace_id: str
    project_id: str
    project_lock_version: int
    version_id: str
    run_id: str
    parent_version_id: str | None
    version_artifact: _Artifact
    manifest: _Artifact
    artifacts: _Artifacts


@dataclass(frozen=True)
class _Analysis:
    ref: str
    sha256: str


@dataclass(frozen=True)
class _ProjectImage:
    image_id: str
    image_sha256: str
    source_document_id: str
    source_sheet: str
    source_row: str
    analysis: _Analysis
    controlled_path: Path
    source_kind: str
    rights_notice: str


@dataclass(frozen=True)
class _Pool:
    workspace_id: str
    project_id: str
    version_id: str
    run_id: str
    images: tuple[_ProjectImage, ...]


@dataclass(frozen=True)
class _Decision:
    decision_id: str
    shot_no: str
    image_id: str
    actor_id: str
    decided_at: str
    resolution_status: str


@dataclass(frozen=True)
class _DecisionSnapshot:
    workspace_id: str
    project_id: str
    version_id: str
    run_id: str
    snapshot_ref: str
    snapshot_sha256: str
    decisions: tuple[_Decision, ...]


@dataclass(frozen=True)
class _Machine:
    ref: str
    sha256: str
    status: str


@dataclass(frozen=True)
class _Review:
    snapshot_ref: str
    snapshot_sha256: str
    total_count: int
    resolved_count: int
    unresolved_count: int


@dataclass(frozen=True)
class _Confirmation:
    confirmation_id: str
    decision: str
    evidence_ref: str
    evidence_sha256: str
    reviewer_actor_id: str
    confirmed_at: str


@dataclass(frozen=True)
class _Quality:
    schema_version: str
    workspace_id: str
    project_id: str
    version_id: str
    run_id: str
    machine_audit: _Machine
    shot_review: _Review
    final_confirmation: _Confirmation


@dataclass(frozen=True)
class _QualityResolver:
    evidence: _Quality

    def resolve_authoritative_evidence(
        self, auth_token: str, *, project_id: str, version_id: str,
        expected_project_lock_version: int,
    ) -> _Quality:
        if (
            auth_token != "fixture-auth" or project_id != self.evidence.project_id
            or version_id != self.evidence.version_id or expected_project_lock_version != 2
        ):
            raise ValueError("fixture resolver request mismatch")
        return self.evidence


@dataclass(frozen=True)
class _FixtureQualityArtifactReader:
    paths: dict[str, Path]

    def read_json(self, ref: str, sha256: str) -> dict:
        for kind, path in self.paths.items():
            digest = _sha(path)
            if ref == f"artifact://version-quality/{kind}/{digest}" and sha256 == digest:
                return json.loads(path.read_text(encoding="utf-8"))
        raise ValueError("fixture quality artifact mismatch")


class _WorkspaceOwnerReviewer:
    def __init__(self, store: WorkspaceStore):
        self.store = store

    def resolve_reviewer(self, auth_token: str, *, workspace_id: str, project_id: str) -> str:
        with self.store.connection() as connection:
            return require_permission(
                connection, auth_token, workspace_id=workspace_id,
                project_id=project_id, permission="review.record",
            )["actor_id"]


class V10ImageDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mplus-v10-image-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.fixture_root = Path(__file__).parent / "fixtures" / "v10_image_delivery"
        self.output = self.root / "output"
        self.artifacts: dict[str, Path] = {}
        self._write_authority_artifacts()
        self.quality_artifact_reader = _FixtureQualityArtifactReader(self.quality_paths)
        self.context, self.pool, self.snapshot, self.quality = self._authority_objects()

    def test_final_excel_and_pdf_are_same_version_reopenable_and_path_free(self):
        excel = self._deliver()
        pdf = create_v10_pdf_delivery(excel, output_root=self.output)
        self.assertTrue(excel.workbook_path.is_file())
        self.assertTrue(pdf.pdf_path.is_file())
        self.assertEqual((pdf.version_id, pdf.run_id, pdf.workbook_sha256), (excel.version_id, excel.run_id, excel.workbook_sha256))
        workbook = load_workbook(excel.workbook_path, read_only=False, data_only=True)
        try:
            script, detail, audit = workbook["脚本"], workbook["抽调明细"], workbook["交付审核"]
            self.assertEqual(script.max_row, 3)
            self.assertEqual(len(script._images), 2)
            self.assertEqual(
                {image.anchor._from.row + 1: hashlib.sha256(image._data()).hexdigest() for image in script._images},
                {2: self.pool.images[0].image_sha256, 3: self.pool.images[1].image_sha256},
            )
            headers = _headers(script)
            self.assertEqual(script.cell(2, headers["旁白"]).value, "欢迎来到现场")
            self.assertEqual(script.cell(3, headers["字幕"]).value, "全场定格")
            self.assertEqual(script.cell(2, headers["时长(秒)"]).value, 4)
            detail_headers, audit_headers = _headers(detail), _headers(audit)
            embedded_image_shas = set()
            for row in (2, 3):
                self.assertTrue(detail.cell(row, detail_headers["采用来源文件"]).value)
                self.assertTrue(detail.cell(row, detail_headers["采用来源 sheet"]).value)
                self.assertTrue(detail.cell(row, detail_headers["采用来源行"]).value)
                self.assertEqual(audit.cell(row, audit_headers["version_id"]).value, "version-002")
                self.assertEqual(audit.cell(row, audit_headers["run_id"]).value, "run-002")
                self.assertEqual(audit.cell(row, audit_headers["quality_evidence_sha256"]).value, excel.quality_evidence_sha256)
                self.assertTrue(audit.cell(row, audit_headers["人工决定ID"]).value)
                self.assertEqual(audit.cell(row, audit_headers["整片确认审核人"]).value, "human-final")
                self.assertEqual(audit.cell(row, audit_headers["整片确认时间"]).value, "2026-08-13T10:30:00Z")
                embedded_image_shas.add(str(audit.cell(row, audit_headers["图片SHA256"]).value))
            self.assertEqual(len(embedded_image_shas), 2)
        finally:
            workbook.close()
        with ZipFile(excel.workbook_path) as archive:
            content = b"".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(str(self.fixture_root).encode(), content)
        self.assertNotIn(str(self.output).encode(), content)
        self.assertNotIn(b".mplus", content)
        reader = PdfReader(str(pdf.pdf_path))
        self.assertEqual(len(reader.pages), 2)
        subject = str(reader.metadata.get("/Subject"))
        for token in (excel.version_id, excel.run_id, excel.visual_binding_sha256, excel.quality_evidence_sha256, excel.final_reviewer_actor_id, excel.final_confirmed_at, excel.workbook_sha256):
            self.assertIn(token, subject)
        self.assertIn("shot=1 duration=4 image_sha256=", reader.pages[0].extract_text() or "")
        self.assertIn("shot=2 duration=6 image_sha256=", reader.pages[1].extract_text() or "")

    def test_rejects_forged_pool_decision_or_quality_dict_before_output(self):
        with self.assertRaisesRegex(V10ImageDeliveryError, "ProjectImagePoolService"):
            self._deliver(pool=lambda _: {})
        with self.assertRaisesRegex(V10ImageDeliveryError, "ImageDecisionService"):
            self._deliver(decisions=lambda _: {})
        with self.assertRaisesRegex(V10ImageDeliveryError, "T07 VersionQualityResolver"):
            self._deliver(quality={})
        self.assertFalse(self.output.exists())

    def test_delivery_calls_authoritative_quality_resolver_with_context_lock(self):
        calls = []

        class Resolver:
            def resolve_authoritative_evidence(self, auth_token, *, project_id, version_id, expected_project_lock_version):
                calls.append((auth_token, project_id, version_id, expected_project_lock_version))
                return self.quality

        resolver = Resolver()
        resolver.quality = self.quality
        self._deliver(quality=resolver)
        self.assertEqual(calls, [("fixture-auth", "project-001", "version-002", 2)])

    def test_rejects_cross_project_and_old_version_authority_objects(self):
        with self.assertRaisesRegex(V10ImageDeliveryError, "project_id"):
            self._deliver(pool=lambda _: replace(self.pool, project_id="project-other"))
        with self.assertRaisesRegex(V10ImageDeliveryError, "version_id"):
            self._deliver(decisions=lambda _: replace(self.snapshot, version_id="version-old"))
        self.assertFalse(self.output.exists())

    def test_rejects_analysis_artifact_drift_before_output(self):
        first = self.artifacts["artifact://analysis/fixture-arrival-001"]
        first.write_text(json.dumps({"status": "pass", "image_id": "fixture-arrival-001", "image_sha256": "0" * 64}), encoding="utf-8")
        with self.assertRaisesRegex(V10ImageDeliveryError, "SHA 已漂移"):
            self._deliver()
        self.assertFalse(self.output.exists())

    def test_fixture_analysis_semantics_match_each_script_shot(self):
        script_rows = json.loads(
            self.artifacts["artifact://run/run-002/visual_binding"].read_text()
        )["script_rows"]
        arrival = json.loads(
            self.artifacts["artifact://analysis/fixture-arrival-001"].read_text()
        )
        group = json.loads(
            self.artifacts["artifact://analysis/fixture-group-002"].read_text()
        )
        self.assertIn("嘉宾入场", script_rows[0]["画面描述"])
        self.assertEqual(arrival["semantic_status"], "human_confirmed")
        self.assertIn("conference_arrival", arrival["tags"])
        self.assertIn("进入", arrival["caption"])
        self.assertIn("全场合影", script_rows[1]["画面描述"])
        self.assertEqual(group["semantic_status"], "human_confirmed")
        self.assertIn("group_photo", group["tags"])
        self.assertIn("多人", group["caption"])
        self.assertIn("集体合影", group["caption"])

    def test_rejects_tampered_decision_against_authority_snapshot_before_output(self):
        tampered = replace(self.snapshot, decisions=(replace(self.snapshot.decisions[0], actor_id="forged-actor"), self.snapshot.decisions[1]))
        with self.assertRaisesRegex(V10ImageDeliveryError, "持久权威快照"):
            self._deliver(decisions=lambda _: tampered)
        self.assertFalse(self.output.exists())

    def test_rejects_duplicate_image_sha_across_shots_before_output(self):
        authority = json.loads(self.artifacts["artifact://review/decisions-002"].read_text())
        authority["decisions"][1]["image_id"] = "fixture-arrival-001"
        snapshot_path = self.artifacts["artifact://review/decisions-002"]
        snapshot_path.write_text(json.dumps(authority), encoding="utf-8")
        duplicate = replace(
            self.snapshot,
            snapshot_sha256=_sha(snapshot_path),
            decisions=tuple(_Decision(**row) for row in authority["decisions"]),
        )
        with self.assertRaisesRegex(V10ImageDeliveryError, "不同 image_sha256"):
            self._deliver(decisions=lambda _: duplicate)
        self.assertFalse(self.output.exists())

    def test_rejects_non_integer_lock_text_only_and_failed_quality(self):
        with self.assertRaisesRegex(V10ImageDeliveryError, "非负整数"):
            self._deliver(context=replace(self.context, project_lock_version="2"))
        with self.assertRaisesRegex(V10ImageDeliveryError, "非负整数"):
            self._deliver(context=replace(self.context, project_lock_version=True))
        with self.assertRaisesRegex(V10ImageDeliveryError, "text_only"):
            self._deliver(decisions=lambda _: replace(self.snapshot, decisions=(replace(self.snapshot.decisions[0], resolution_status="text_only_approved"), self.snapshot.decisions[1])))
        failed = replace(self.quality, machine_audit=replace(self.quality.machine_audit, status="fail"))
        with self.assertRaisesRegex(V10ImageDeliveryError, "machine_audit"):
            self._deliver(quality=_QualityResolver(failed))

    def test_rejects_memory_review_placeholder_source_and_persistent_quality_drift(self):
        memory = replace(self.quality, shot_review=replace(self.quality.shot_review, snapshot_ref="memory://version-002/review"))
        with self.assertRaisesRegex(V10ImageDeliveryError, "artifact://"):
            self._deliver(quality=_QualityResolver(memory))
        placeholder = replace(self.pool, images=(replace(self.pool.images[0], source_kind="test_placeholder"),))
        with self.assertRaisesRegex(V10ImageDeliveryError, "placeholder"):
            self._deliver(pool=lambda _: placeholder)
        final = self.quality_paths["final-confirmation"]
        final.write_text(json.dumps({"confirmation_id": "forged", "version_id": "version-002", "decision": "quality_pass", "reviewer_actor_id": "human-final", "confirmed_at": "2026-08-13T10:30:00Z"}), encoding="utf-8")
        with self.assertRaisesRegex(V10ImageDeliveryError, "SHA 已漂移"):
            self._deliver()
        self.assertFalse(self.output.exists())

    def test_rejects_sha_synced_cross_project_review_payload_before_output(self):
        review = self.quality_paths["shot-review"]
        payload = json.loads(review.read_text())
        payload["project_id"] = "project-other"
        review.write_text(json.dumps(payload), encoding="utf-8")
        quality = replace(self.quality, shot_review=replace(
            self.quality.shot_review, snapshot_ref=self._quality_ref("shot-review", review),
            snapshot_sha256=_sha(review),
        ))
        with self.assertRaisesRegex(V10ImageDeliveryError, "shot_review"):
            self._deliver(quality=_QualityResolver(quality))
        self.assertFalse(self.output.exists())

    def test_rejects_sha_synced_old_run_final_payload_before_output(self):
        final = self.quality_paths["final-confirmation"]
        payload = json.loads(final.read_text())
        payload["run_id"] = "run-old"
        final.write_text(json.dumps(payload), encoding="utf-8")
        quality = replace(self.quality, final_confirmation=replace(
            self.quality.final_confirmation,
            evidence_ref=self._quality_ref("final-confirmation", final), evidence_sha256=_sha(final),
        ))
        with self.assertRaisesRegex(V10ImageDeliveryError, "final_confirmation"):
            self._deliver(quality=_QualityResolver(quality))
        self.assertFalse(self.output.exists())

    def test_rejects_sha_synced_mismatched_final_snapshot_before_output(self):
        final = self.quality_paths["final-confirmation"]
        payload = json.loads(final.read_text())
        payload["snapshot_sha256"] = "0" * 64
        final.write_text(json.dumps(payload), encoding="utf-8")
        quality = replace(self.quality, final_confirmation=replace(
            self.quality.final_confirmation,
            evidence_ref=self._quality_ref("final-confirmation", final), evidence_sha256=_sha(final),
        ))
        with self.assertRaisesRegex(V10ImageDeliveryError, "final_confirmation"):
            self._deliver(quality=_QualityResolver(quality))
        self.assertFalse(self.output.exists())

    def test_workspace_store_t07_resolver_e2e_exports_same_version_without_provider_or_publication(self):
        root = self.root / "real-t07-e2e"
        runs_root = root / "runs"
        store = WorkspaceStore(root / "workspace.sqlite3")
        owner = LocalIdentityAdapter(store).bootstrap_owner(
            workspace_name="匿名 T08 E2E 工作区", display_name="匿名审核员",
            confirmed=True, token="t08-e2e-token",
        )
        token, workspace_id, actor_id = owner["auth_token"], owner["workspace_id"], owner["actor_id"]
        projects = ProjectService(store)
        workspace = projects.get_workspace(token, workspace_id)
        project = projects.create_project(
            token, workspace_id=workspace_id, name="匿名 T08 E2E 项目",
            category_id="launch", expected_workspace_version=workspace["workspace_version"],
        )
        run_id = "run_t08_e2e_0001"
        run_dir = runs_root / "anonymous" / "runs" / run_id
        run_dir.mkdir(parents=True)
        script_rows = [
            {"镜号": "1", "结构": "开场", "时长(秒)": 4, "画面描述": "冻结项目中的嘉宾入场", "声音": "现场环境声", "旁白": "欢迎来到现场", "字幕": "品牌发布会"},
            {"镜号": "2", "结构": "收束", "时长(秒)": 6, "画面描述": "冻结项目中的全场合影", "声音": "音乐收束", "旁白": "感谢参与", "字幕": "全场定格"},
        ]
        detail_rows = [{"模板镜号": "1"}, {"模板镜号": "2"}]
        stage_paths = {}
        for stage, schema in (
            ("script_plan", "mplus-script-plan/v2"),
            ("script_draft", "mplus-script-draft/v1"),
            ("visual_binding", "mplus-visual-binding/v1"),
        ):
            payload = {"schema_version": schema, "script_rows": script_rows, "detail_rows": detail_rows}
            if stage == "visual_binding":
                payload["bindings"] = [
                    {"shot_no": "1", "image_id": "fixture-arrival-001"},
                    {"shot_no": "2", "image_id": "fixture-group-002"},
                ]
            path = run_dir / f"{stage}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            stage_paths[stage] = path
        workbook = run_dir / "workbook.xlsx"
        workbook.write_bytes(b"anonymous T08 E2E workbook")
        report = run_dir / "run_report.json"
        report.write_text(json.dumps({"run_id": run_id, "machine_audit_status": "pass"}), encoding="utf-8")
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(json.dumps({
            "run_id": run_id,
            "artifacts": {
                "workbook": {"path": "workbook.xlsx", "sha256": _sha(workbook)},
                "run_report": {"path": "run_report.json", "sha256": _sha(report)},
            },
            "stages": {
                stage: {"stage_id": stage, "status": "succeeded", "output_refs": [str(path.resolve())], "output_sha256": _sha(path)}
                for stage, path in stage_paths.items()
            },
        }, sort_keys=True), encoding="utf-8")
        bound = projects.bind_run(
            token, project_id=project["project_id"], run_id=run_id, source_kind="native",
            manifest_ref=f"artifact://run/{run_id}/manifest", manifest_sha256=_sha(manifest),
            expected_project_version=project["project_version"],
        )
        version = projects.create_project_version(
            token, project_id=project["project_id"], run_id=run_id,
            content_sha256=_sha(workbook), artifact_ref=f"artifact://run/{run_id}/workbook",
            expected_project_version=bound["project_version"],
        )
        run_artifacts = RunArtifactResolver(runs_root)
        delivery = VersionDeliveryService(store, run_artifacts)
        reviews = VersionReviewService(InMemoryVersionReviewRepository())
        draft = reviews.create_draft(
            workspace_id=workspace_id, project_id=project["project_id"], base_version_id="parent",
            actor_id=actor_id, shots=[
                {"shot_no": "1", "fields": {"image_id": "fixture-arrival-001"}, "source_refs": [{"source_file": "anonymous", "source_sheet": "fixture_metadata", "source_row": "1"}]},
                {"shot_no": "2", "fields": {"image_id": "fixture-group-002"}, "source_refs": [{"source_file": "anonymous", "source_sheet": "fixture_metadata", "source_row": "2"}]},
            ],
        )
        reviews.submit_draft(draft.draft_id, actor_id=actor_id, formal_version_id=version["version_id"])
        review_items = reviews.add_review_items(version["version_id"], items=[
            {"kind": "image", "shot_no": "1", "severity": "blocker", "evidence_ref": "artifact://review/item-1", "evidence_sha256": "1" * 64},
            {"kind": "image", "shot_no": "2", "severity": "blocker", "evidence_ref": "artifact://review/item-2", "evidence_sha256": "2" * 64},
        ])
        reviews.decide_review_items(
            version["version_id"], item_ids=[item.review_item_id for item in review_items],
            decision="approve", evidence_ref="artifact://review/approve", evidence_sha256="a" * 64,
            reviewer_actor_id=actor_id,
        )
        quality_artifacts = FileVersionQualityArtifactStore(root / "quality-evidence")
        quality_resolver = build_workspace_version_quality_resolver(
            store=store, delivery_service=delivery, run_artifacts=run_artifacts,
            reviews=reviews, evidence_artifacts=quality_artifacts,
        )
        lock = projects.get_project(token, project["project_id"])["project_version"]
        AuthenticatedFinalConfirmationService(
            resolver=quality_resolver, actor_resolver=_WorkspaceOwnerReviewer(store),
        ).confirm(
            token, project_id=project["project_id"], version_id=version["version_id"],
            expected_project_lock_version=lock, decision="quality_pass",
            evidence_ref="artifact://review/final", evidence_sha256="f" * 64,
        )
        context = delivery.resolve(token, project["project_id"], version["version_id"], expected_project_version=lock)
        image_data = self._e2e_image_data(root)
        decision_path = root / "image-decisions.json"
        decisions = [
            {"decision_id": "t08-e2e-decision-1", "shot_no": "1", "image_id": image_data[0]["image_id"], "actor_id": actor_id, "decided_at": "2026-08-13T10:10:00Z", "resolution_status": "approved"},
            {"decision_id": "t08-e2e-decision-2", "shot_no": "2", "image_id": image_data[1]["image_id"], "actor_id": actor_id, "decided_at": "2026-08-13T10:11:00Z", "resolution_status": "approved"},
        ]
        decision_path.write_text(json.dumps({"decisions": decisions}), encoding="utf-8")
        decision_snapshot = _DecisionSnapshot(
            workspace_id, project["project_id"], version["version_id"], run_id,
            "artifact://review/t08-e2e-decisions", _sha(decision_path),
            tuple(_Decision(**row) for row in decisions),
        )
        pool = _Pool(
            workspace_id, project["project_id"], version["version_id"], run_id,
            tuple(_ProjectImage(**image) for image in image_data),
        )
        resolver_paths = {"artifact://review/t08-e2e-decisions": decision_path}
        resolver_paths.update({item["analysis"].ref: root / f"{item['image_id']}-analysis.json" for item in image_data})

        def resolve_artifact(ref, expected_sha256):
            if ref == context.artifacts.visual_binding.ref:
                resolved = run_artifacts.resolve_stage_artifact(
                    run_id, "visual_binding", expected_manifest_sha256=context.manifest.sha256,
                )
                self.assertEqual((resolved.ref, resolved.sha256), (ref, expected_sha256))
                return resolved.path
            if ref == f"artifact://run/{run_id}/run_report":
                resolved = run_artifacts.resolve_artifact(run_id, "run_report")
                self.assertEqual((resolved.ref, resolved.sha256), (ref, expected_sha256))
                return resolved.path
            path = resolver_paths[ref]
            self.assertEqual(_sha(path), expected_sha256)
            return path

        before_publications = self._publication_count(store)
        excel = create_v10_image_delivery(
            context, artifact_resolver=resolve_artifact, quality_artifact_reader=quality_artifacts,
            quality_resolver=quality_resolver, quality_auth_token=token,
            project_image_pool_resolver=lambda _: pool,
            image_decision_resolver=lambda _: decision_snapshot,
            output_root=root / "delivery-output",
        )
        pdf = create_v10_pdf_delivery(excel, output_root=root / "delivery-output")
        self.assertEqual(before_publications, self._publication_count(store))
        self.assertFalse((root / "CURRENT").exists())
        self.assertEqual((excel.version_id, excel.run_id), (version["version_id"], run_id))
        self.assertEqual((pdf.version_id, pdf.run_id, pdf.workbook_sha256), (excel.version_id, excel.run_id, excel.workbook_sha256))
        self._assert_e2e_reopened(excel, pdf, image_data)
        with self.assertRaisesRegex(WorkspaceServiceError, "PROJECT_VERSION_CONFLICT"):
            quality_resolver.resolve_authoritative_evidence(
                token, project_id=project["project_id"], version_id=version["version_id"],
                expected_project_lock_version=lock - 1,
            )

    def _e2e_image_data(self, root: Path) -> list[dict]:
        image_data = []
        for image_id, metadata_name in (("fixture-arrival-001", "anonymous-conference-arrival.json"), ("fixture-group-002", "cc0-participant-group.json")):
            metadata = json.loads((self.fixture_root / metadata_name).read_text(encoding="utf-8"))
            image = self.fixture_root / str(metadata["filename"])
            analysis_path = root / f"{image_id}-analysis.json"
            image_sha = _sha(image)
            analysis_path.write_text(json.dumps({"image_id": image_id, "image_sha256": image_sha, "source_kind": metadata["source_kind"], **metadata["analysis"]}, ensure_ascii=False), encoding="utf-8")
            image_data.append({
                "image_id": image_id, "image_sha256": image_sha,
                "source_document_id": str(metadata["fixture_id"]), "source_sheet": "fixture_metadata", "source_row": "1",
                "analysis": _Analysis(f"artifact://analysis/{image_id}", _sha(analysis_path)),
                "controlled_path": image, "source_kind": str(metadata["source_kind"]), "rights_notice": str(metadata["rights_notice"]),
            })
        return image_data

    def _assert_e2e_reopened(self, excel, pdf, image_data):
        workbook = load_workbook(excel.workbook_path, read_only=False, data_only=True)
        try:
            script, detail, audit = workbook["脚本"], workbook["抽调明细"], workbook["交付审核"]
            self.assertEqual((script.max_row, len(script._images)), (3, 2))
            headers, detail_headers, audit_headers = _headers(script), _headers(detail), _headers(audit)
            self.assertEqual(
                {image.anchor._from.row + 1: hashlib.sha256(image._data()).hexdigest() for image in script._images},
                {row: image["image_sha256"] for row, image in zip((2, 3), image_data)},
            )
            for row, image in zip((2, 3), image_data):
                self.assertTrue(script.cell(row, headers["画面描述"]).value)
                self.assertTrue(script.cell(row, headers["声音"]).value)
                self.assertTrue(script.cell(row, headers["时长(秒)"]).value)
                self.assertTrue(detail.cell(row, detail_headers["采用来源文件"]).value)
                self.assertTrue(detail.cell(row, detail_headers["采用来源 sheet"]).value)
                self.assertTrue(detail.cell(row, detail_headers["采用来源行"]).value)
                self.assertEqual(audit.cell(row, audit_headers["图片SHA256"]).value, image["image_sha256"])
                self.assertTrue(audit.cell(row, audit_headers["人工决定ID"]).value)
                self.assertTrue(audit.cell(row, audit_headers["整片确认审核人"]).value)
                self.assertTrue(audit.cell(row, audit_headers["整片确认时间"]).value)
                self.assertEqual(audit.cell(row, audit_headers["version_id"]).value, excel.version_id)
                self.assertEqual(audit.cell(row, audit_headers["run_id"]).value, excel.run_id)
                self.assertEqual(audit.cell(row, audit_headers["visual_binding_sha256"]).value, excel.visual_binding_sha256)
                self.assertEqual(audit.cell(row, audit_headers["quality_evidence_sha256"]).value, excel.quality_evidence_sha256)
        finally:
            workbook.close()
        with ZipFile(excel.workbook_path) as archive:
            content = b"".join(archive.read(name) for name in archive.namelist())
        self.assertNotIn(str(excel.workbook_path.parent).encode(), content)
        self.assertNotIn(b".mplus", content)
        reader = PdfReader(str(pdf.pdf_path))
        self.assertEqual(len(reader.pages), 2)
        subject = str(reader.metadata.get("/Subject"))
        for value in (excel.version_id, excel.run_id, excel.visual_binding_sha256, excel.quality_evidence_sha256, excel.workbook_sha256):
            self.assertIn(value, subject)
        self.assertIn("shot=2 duration=6 image_sha256=", reader.pages[1].extract_text() or "")

    @staticmethod
    def _publication_count(store: WorkspaceStore) -> int:
        with store.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM version_publications").fetchone()[0])

    def _deliver(self, *, context=None, pool=None, decisions=None, quality=None):
        return create_v10_image_delivery(
            self.context if context is None else context, artifact_resolver=self._resolve,
            quality_artifact_reader=self.quality_artifact_reader,
            quality_resolver=_QualityResolver(self.quality) if quality is None else quality, quality_auth_token="fixture-auth",
            project_image_pool_resolver=(lambda _: self.pool) if pool is None else pool,
            image_decision_resolver=(lambda _: self.snapshot) if decisions is None else decisions, output_root=self.output,
        )

    def _write_authority_artifacts(self):
        binding = self.root / "visual_binding.json"
        binding.write_text(json.dumps({"schema_version": "mplus-visual-binding/v1", "script_rows": [
            {"镜号": "1", "结构": "开场", "时长(秒)": 4, "画面描述": "冻结项目中的嘉宾入场", "声音": "现场环境声", "旁白": "欢迎来到现场", "字幕": "品牌发布会"},
            {"镜号": "2", "结构": "收束", "时长(秒)": 6, "画面描述": "冻结项目中的全场合影", "声音": "音乐收束", "旁白": "感谢参与", "字幕": "全场定格"},
        ], "detail_rows": [], "bindings": [{"shot_no": "1", "image_id": "old-image-1"}, {"shot_no": "2", "image_id": "old-image-2"}]}, ensure_ascii=False), encoding="utf-8")
        self.artifacts["artifact://run/run-002/visual_binding"] = binding
        self.image_data = []
        for image_id, metadata_name in (("fixture-arrival-001", "anonymous-conference-arrival.json"), ("fixture-group-002", "cc0-participant-group.json")):
            metadata = json.loads((self.fixture_root / metadata_name).read_text(encoding="utf-8"))
            image = self.fixture_root / str(metadata["filename"])
            image_sha = _sha(image)
            self.assertEqual(image_sha, metadata["sha256"])
            analysis = self.root / f"analysis-{image_id}.json"
            analysis.write_text(json.dumps({"image_id": image_id, "image_sha256": image_sha, "source_kind": metadata["source_kind"], **metadata["analysis"]}, ensure_ascii=False), encoding="utf-8")
            ref = f"artifact://analysis/{image_id}"
            self.artifacts[ref] = analysis
            self.image_data.append((image_id, image, image_sha, metadata, ref, _sha(analysis)))
        decisions = [{"decision_id": "decision-1", "shot_no": "1", "image_id": "fixture-arrival-001", "actor_id": "human-a", "decided_at": "2026-08-13T10:10:00Z", "resolution_status": "approved"}, {"decision_id": "decision-2", "shot_no": "2", "image_id": "fixture-group-002", "actor_id": "human-b", "decided_at": "2026-08-13T10:11:00Z", "resolution_status": "approved"}]
        snapshot = self.root / "decisions.json"
        snapshot.write_text(json.dumps({"decisions": decisions}), encoding="utf-8")
        self.artifacts["artifact://review/decisions-002"] = snapshot
        machine = self.root / "run-report.json"
        machine.write_text(json.dumps({"run_id": "run-002", "machine_audit_status": "pass"}), encoding="utf-8")
        self.artifacts["artifact://run/run-002/run_report"] = machine
        review = self.root / "shot-review.json"
        review.write_text(json.dumps({"workspace_id": "workspace-001", "project_id": "project-001", "version_id": "version-002", "run_id": "run-002", "total_count": 2, "resolved_count": 2, "unresolved_count": 0}), encoding="utf-8")
        final = self.root / "final-confirmation.json"
        final.write_text(json.dumps({"schema_version": "mplus-final-confirmation-evidence/v1", "workspace_id": "workspace-001", "project_id": "project-001", "version_id": "version-002", "run_id": "run-002", "snapshot_ref": self._quality_ref("shot-review", review), "snapshot_sha256": _sha(review), "confirmation": {"confirmation_id": "confirmation-002", "decision": "quality_pass", "reviewer_actor_id": "human-final", "confirmed_at": "2026-08-13T10:30:00Z"}}), encoding="utf-8")
        self.quality_paths = {"shot-review": review, "final-confirmation": final}

    def _authority_objects(self):
        visual = _Artifact("artifact://run/run-002/visual_binding", _sha(self.artifacts["artifact://run/run-002/visual_binding"]))
        context = _Context("mplus-version-delivery-context/v1", "workspace-001", "project-001", 2, "version-002", "run-002", "version-001", _Artifact("artifact://run/run-002/version", "a" * 64), _Artifact("artifact://run/run-002/manifest", "b" * 64), _Artifacts(_Artifact("artifact://run/run-002/script_plan", "c" * 64), _Artifact("artifact://run/run-002/script_draft", "d" * 64), visual))
        pool = _Pool("workspace-001", "project-001", "version-002", "run-002", tuple(
            _ProjectImage(image_id, image_sha, str(metadata["fixture_id"]), "fixture_metadata", "1", _Analysis(ref, analysis_sha), image, str(metadata["source_kind"]), str(metadata["rights_notice"]))
            for image_id, image, image_sha, metadata, ref, analysis_sha in self.image_data
        ))
        raw = json.loads(self.artifacts["artifact://review/decisions-002"].read_text())["decisions"]
        decisions = tuple(_Decision(**row) for row in raw)
        snapshot = _DecisionSnapshot("workspace-001", "project-001", "version-002", "run-002", "artifact://review/decisions-002", _sha(self.artifacts["artifact://review/decisions-002"]), decisions)
        quality = _Quality("mplus-version-quality-evidence/v1", "workspace-001", "project-001", "version-002", "run-002", _Machine("artifact://run/run-002/run_report", _sha(self.artifacts["artifact://run/run-002/run_report"]), "pass"), _Review(self._quality_ref("shot-review", self.quality_paths["shot-review"]), _sha(self.quality_paths["shot-review"]), 2, 2, 0), _Confirmation("confirmation-002", "quality_pass", self._quality_ref("final-confirmation", self.quality_paths["final-confirmation"]), _sha(self.quality_paths["final-confirmation"]), "human-final", "2026-08-13T10:30:00Z"))
        return context, pool, snapshot, quality

    def _resolve(self, ref, expected_sha):
        return self.artifacts[ref]

    @staticmethod
    def _quality_ref(kind: str, path: Path) -> str:
        return f"artifact://version-quality/{kind}/{_sha(path)}"


def _headers(sheet):
    return {str(cell.value): cell.column for cell in sheet[1]}


def _sha(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest()
