"""Project an explicit version publication after delivery evidence is valid."""

from __future__ import annotations

import json
from pathlib import Path
from zipfile import is_zipfile

from .artifact_service import ArtifactResolutionError, RunArtifactResolver
from .pipeline_runtime import publish_symlink_atomic
from .run_contract import sha256_file
from .workspace_store import WorkspaceServiceError, WorkspaceStore, utc_now


class VersionPublicationProjectionService:
    """Consume the existing publication outbox without changing review semantics."""

    def __init__(
        self,
        store: WorkspaceStore,
        artifacts: RunArtifactResolver,
        *,
        current_path: str | Path | None = None,
    ) -> None:
        self.store = store
        self.artifacts = artifacts
        self.current_path = Path(
            current_path or artifacts.runs_root.parent / "CURRENT"
        ).expanduser().absolute()
        self.store.initialize()

    def process_event(self, event_id: str) -> dict:
        with self.store.connection() as connection:
            event = connection.execute(
                "SELECT * FROM outbox WHERE event_id=?",
                (str(event_id or "").strip(),),
            ).fetchone()
        if event is None or event["event_type"] != "project.version_published":
            raise WorkspaceServiceError("NOT_FOUND", "版本发布投影事件不存在")
        payload = self._payload(event["payload_json"])
        if event["status"] == "published":
            return {
                "event_id": event["event_id"],
                "publication_id": payload["publication_id"],
                "projection_status": "published",
                "run_id": self._publication_run_id(payload["publication_id"]),
                "replayed": True,
            }
        try:
            result = self._project(event, payload)
        except (ArtifactResolutionError, OSError, WorkspaceServiceError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            with self.store.transaction() as connection:
                connection.execute(
                    """UPDATE outbox SET status='failed', attempts=attempts+1,
                    last_error=?, published_at=NULL WHERE event_id=?""",
                    (str(code), event["event_id"]),
                )
            return {
                "event_id": event["event_id"],
                "publication_id": payload["publication_id"],
                "projection_status": "failed",
                "error_code": str(code),
                "run_id": "",
                "replayed": event["status"] == "failed",
            }
        return result

    def process_publication(self, publication_id: str) -> dict:
        target = str(publication_id or "").strip()
        with self.store.connection() as connection:
            events = connection.execute(
                """SELECT event_id, payload_json FROM outbox
                WHERE event_type='project.version_published'
                ORDER BY sequence_id DESC"""
            ).fetchall()
        for event in events:
            try:
                payload = json.loads(event["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if str(dict(payload or {}).get("publication_id") or "") == target:
                return self.process_event(str(event["event_id"]))
        raise WorkspaceServiceError("OUTBOX_MISSING", "发布登记缺少投影事件")

    def _project(self, event, payload: dict) -> dict:
        with self.store.connection() as connection:
            row = connection.execute(
                """SELECT p.publication_id, p.project_id, p.version_id,
                          v.run_id, v.artifact_ref, v.content_sha256,
                          s.delivery_snapshot_id, s.workbook_ref, s.workbook_sha256,
                          b.availability AS excel_availability,
                          b.artifact_ref AS excel_ref, b.artifact_sha256 AS excel_sha256,
                          b.size_bytes AS excel_size_bytes
                FROM version_publications p
                JOIN project_versions v ON v.version_id=p.version_id
                LEFT JOIN delivery_snapshots s ON s.version_id=p.version_id
                LEFT JOIN delivery_artifact_bindings b
                  ON b.delivery_snapshot_id=s.delivery_snapshot_id
                 AND b.artifact_kind='excel'
                WHERE p.publication_id=?""",
                (payload["publication_id"],),
            ).fetchone()
            if row is None:
                raise WorkspaceServiceError("PUBLICATION_MISSING", "发布登记不存在")
            if (
                row["project_id"] != event["aggregate_id"]
                or row["version_id"] != payload["version_id"]
                or row["artifact_ref"] != payload["artifact_ref"]
                or row["content_sha256"] != payload["content_sha256"]
            ):
                raise WorkspaceServiceError("PUBLICATION_DRIFT", "发布事件与版本登记不一致")
            if (
                not row["delivery_snapshot_id"]
                or row["excel_availability"] != "available"
                or row["artifact_ref"] != row["workbook_ref"]
                or row["content_sha256"] != row["workbook_sha256"]
                or row["excel_ref"] != f"artifact://delivery/{row['delivery_snapshot_id']}/excel"
            ):
                raise WorkspaceServiceError(
                    "DELIVERY_REQUIRED",
                    "发布前必须存在与目标版本一致的有效 Excel 交付快照",
                )
            pdf = connection.execute(
                """SELECT availability, artifact_sha256 FROM delivery_artifact_bindings
                WHERE delivery_snapshot_id=? AND artifact_kind='pdf'""",
                (row["delivery_snapshot_id"],),
            ).fetchone()
        workbook = self.artifacts.resolve_artifact(row["run_id"], "workbook")
        if (
            workbook.ref != row["workbook_ref"]
            or workbook.sha256 != row["workbook_sha256"]
            or workbook.path.suffix.casefold() != ".xlsx"
            or not is_zipfile(workbook.path)
        ):
            raise WorkspaceServiceError("DELIVERY_ARTIFACT_DRIFT", "版本 Excel 无效或已漂移")
        delivery_excel = self.artifacts.runs_root / "_formal_deliveries" / (
            f"{row['delivery_snapshot_id']}.xlsx"
        )
        if (
            not delivery_excel.is_file()
            or delivery_excel.stat().st_size != int(row["excel_size_bytes"] or 0)
            or sha256_file(delivery_excel) != row["excel_sha256"]
            or not is_zipfile(delivery_excel)
        ):
            raise WorkspaceServiceError("DELIVERY_ARTIFACT_DRIFT", "正式 Excel 无效或已漂移")
        if pdf is not None and pdf["availability"] == "available":
            pdf_path = self.artifacts.runs_root / "_formal_deliveries" / (
                f"{row['delivery_snapshot_id']}.pdf"
            )
            if not pdf_path.is_file() or sha256_file(pdf_path) != pdf["artifact_sha256"]:
                raise WorkspaceServiceError("DELIVERY_ARTIFACT_DRIFT", "已登记 PDF 无效或已漂移")
        run_dir = self.artifacts.resolve_manifest(row["run_id"]).path.parent.resolve()
        publish_symlink_atomic(run_dir, self.current_path)
        now = utc_now()
        with self.store.transaction() as connection:
            connection.execute(
                """UPDATE outbox SET status='published', attempts=attempts+1,
                published_at=?, last_error=NULL WHERE event_id=?""",
                (now, event["event_id"]),
            )
        return {
            "event_id": event["event_id"],
            "publication_id": row["publication_id"],
            "projection_status": "published",
            "run_id": row["run_id"],
            "replayed": event["status"] == "failed",
        }

    def _publication_run_id(self, publication_id: str) -> str:
        with self.store.connection() as connection:
            row = connection.execute(
                """SELECT v.run_id FROM version_publications p
                JOIN project_versions v ON v.version_id=p.version_id
                WHERE p.publication_id=?""",
                (publication_id,),
            ).fetchone()
        return str(row["run_id"]) if row is not None else ""

    @staticmethod
    def _payload(raw: str) -> dict:
        try:
            payload = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise WorkspaceServiceError("OUTBOX_INVALID", "发布事件 payload 无效") from exc
        required = ("publication_id", "version_id", "artifact_ref", "content_sha256")
        if not isinstance(payload, dict) or any(not str(payload.get(key) or "") for key in required):
            raise WorkspaceServiceError("OUTBOX_INVALID", "发布事件 payload 缺少必要字段")
        return payload
