from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from zipfile import ZipFile, is_zipfile

from openpyxl import load_workbook
from pypdf import PdfReader

from . import __version__
from .artifact_service import RunArtifactResolver
from .cloud_packages import materialize_package
from .cloud_runtime import CloudRuntimeError, PostgresCloudRunRepository
from .cloud_storage import CosObjectStorage
from .portable_paths import resolve_run_local_path
from .run_contract import sha256_file


class CloudWorker:
    def __init__(
        self,
        repository,
        storage: CosObjectStorage,
        *,
        worker_id: str,
        package_cache: str | Path,
        lease_seconds: int = 120,
        max_run_seconds: int = 3 * 60 * 60,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.worker_id = str(worker_id or "").strip()
        self.package_cache = Path(package_cache).expanduser().resolve()
        self.lease_seconds = int(lease_seconds)
        self.max_run_seconds = int(max_run_seconds)
        if not self.worker_id:
            raise CloudRuntimeError("worker_id 不得为空")
        if self.max_run_seconds < 60:
            raise CloudRuntimeError("max_run_seconds 不得小于 60")

    @classmethod
    def from_environment(cls, *, worker_id: str, package_cache: str | Path):
        repository = PostgresCloudRunRepository.from_environment()
        repository.check_schema()
        return cls(
            repository,
            CosObjectStorage.from_environment(),
            worker_id=worker_id,
            package_cache=package_cache,
            max_run_seconds=int(os.environ.get("MPLUS_MAX_RUN_SECONDS") or 3 * 60 * 60),
        )

    def run_once(self) -> dict | None:
        claimed = self.repository.claim(self.worker_id, lease_seconds=self.lease_seconds)
        if claimed is None:
            return None
        stop_heartbeat = threading.Event()
        heartbeat_errors = []

        def heartbeat() -> None:
            interval = max(10, self.lease_seconds // 3)
            while not stop_heartbeat.wait(interval):
                try:
                    self.repository.heartbeat(
                        claimed["run_id"],
                        worker_id=self.worker_id,
                        lease_token=claimed["lease_token"],
                        lease_seconds=self.lease_seconds,
                    )
                except Exception as exc:
                    heartbeat_errors.append(exc)
                    return

        heartbeat_thread = threading.Thread(target=heartbeat, daemon=True)
        heartbeat_thread.start()
        try:
            return self._execute(claimed, heartbeat_errors=heartbeat_errors)
        except Exception as exc:
            try:
                return self.repository.finish(
                    claimed["run_id"],
                    worker_id=self.worker_id,
                    lease_token=claimed["lease_token"],
                    status="failed",
                    exit_code=1,
                    artifact_availability="metadata_only",
                    error_code=type(exc).__name__,
                )
            except CloudRuntimeError:
                raise exc
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2)

    def serve_forever(self, *, poll_seconds: float = 2.0) -> None:
        while True:
            if self.run_once() is None:
                time.sleep(max(0.25, poll_seconds))

    def _execute(self, run: dict, *, heartbeat_errors: list[Exception]) -> dict:
        if run.get("runner_version") != __version__:
            raise CloudRuntimeError("RUNNER_VERSION_MISMATCH")
        package_root = materialize_package(run, self.storage, self.package_cache)
        with tempfile.TemporaryDirectory(prefix=f"mplus-cloud-{run['run_id']}-") as tmp:
            work_root = Path(tmp)
            input_path = self.storage.download_file(
                run["input_ref"],
                work_root / "input" / run["input_filename"],
                expected_sha256=run["input_sha256"],
            )
            output_root = work_root / "output"
            data_root = work_root / "data"
            output_root.mkdir()
            data_root.mkdir()
            command = [
                sys.executable,
                "-m",
                "mvp_v2.launch_entry",
                "--asset-package",
                str(package_root),
                "--source-root",
                str(package_root / "sources"),
                "--output-dir",
                str(output_root),
                "--max-loops",
                str(int(dict(run.get("request") or {}).get("max_loops", 3))),
                "--quality-profile",
                str(dict(run.get("request") or {}).get("quality_profile") or "draft"),
                "--text-model",
                str(dict(run.get("request") or {})["ai_model"]),
                "--writer-provider",
                str(dict(run.get("request") or {})["ai_provider"]),
                "--editor-provider",
                str(dict(run.get("request") or {})["ai_provider"]),
            ]
            if input_path.suffix.casefold() in {".xlsx", ".xlsm", ".xls"}:
                command.extend(["--brief", str(input_path)])
            else:
                command.extend(["--brief-file", str(input_path)])
            environment = dict(os.environ)
            environment.update({
                "MPLUS_DATA_ROOT": str(data_root),
                "MPLUS_SOURCE_ROOT": str(package_root / "sources"),
                "MPLUS_ASSET_PACKAGE": str(package_root),
                "MPLUS_SOURCE_COMMIT": run["source_commit"],
                "MPLUS_LOCKED_RUN_PROVENANCE": json.dumps({
                    "schema_version": run["run_schema_version"],
                    "cloud_run_id": run["run_id"],
                    "parent_run_id": run.get("parent_run_id"),
                    "source_run_id": run.get("source_run_id"),
                    "runner_version": run["runner_version"],
                    "source_commit": run["source_commit"],
                    "package_id": run["package_id"],
                    "package_version": run["package_version"],
                    "package_sha256": run["package_sha256"],
                    "package_schema_version": run["package_schema_version"],
                    "input_ref": run["input_ref"],
                    "input_sha256": run["input_sha256"],
                    "contract": run.get("contract"),
                }, ensure_ascii=False, sort_keys=True),
            })
            if run.get("parent_run_id"):
                command.extend(["--parent-run-id", str(run["parent_run_id"])])
            if heartbeat_errors:
                raise heartbeat_errors[0]
            process = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            process_started = time.monotonic()
            while True:
                try:
                    stdout, _ = process.communicate(timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    if heartbeat_errors:
                        _terminate_process_group(process)
                        raise heartbeat_errors[0]
                    if time.monotonic() - process_started > self.max_run_seconds:
                        _terminate_process_group(process)
                        raise CloudRuntimeError("RUN_TIMEOUT")
            if heartbeat_errors:
                raise heartbeat_errors[0]
            lines = [stdout or ""]
            exit_code = int(process.returncode or 0)
            log_path = work_root / "worker.log"
            log_path.write_text("".join(lines), encoding="utf-8")
            delivery_available = self._validate_delivery(run, output_root, data_root)
            result = self._upload_results(run, output_root, data_root, log_path, exit_code)
            if heartbeat_errors:
                raise heartbeat_errors[0]
            status, availability = _terminal_status(
                exit_code, delivery_available=delivery_available,
            )
            return self.repository.finish(
                run["run_id"],
                worker_id=self.worker_id,
                lease_token=run["lease_token"],
                status=status,
                exit_code=exit_code,
                artifact_availability=availability,
                result_ref=result["storage_ref"],
                result_sha256=result["sha256"],
                error_code=(
                    None
                    if status in {"succeeded", "waiting_human_review"}
                    else "DELIVERY_VALIDATION_FAILED"
                    if exit_code == 0
                    else "PIPELINE_EXIT_NONZERO"
                ),
            )

    @staticmethod
    def _validate_delivery(run: dict, output_root: Path, data_root: Path) -> bool:
        return _validate_delivery(run, output_root, data_root)

    def _upload_results(
        self,
        run: dict,
        output_root: Path,
        data_root: Path,
        log_path: Path,
        exit_code: int,
    ) -> dict:
        records = []
        attempt_prefix = f"runs/{run['run_id']}/attempts/{int(run['attempts'])}"
        sources = [("output", output_root), ("runs", data_root / "runs")]
        for namespace, root in sources:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.is_symlink() or path.name.endswith(".lock"):
                    continue
                relative = f"{namespace}/{path.relative_to(root).as_posix()}"
                digest = sha256_file(path)
                cloud = self.storage.put_file(
                    path,
                    key=self.storage.key(f"{attempt_prefix}/{relative}"),
                    expected_sha256=digest,
                )
                records.append({
                    "path": relative,
                    "sha256": digest,
                    "size_bytes": path.stat().st_size,
                    "storage_ref": cloud.storage_ref,
                })
        log_digest = sha256_file(log_path)
        log_cloud = self.storage.put_file(
            log_path,
            key=self.storage.key(f"{attempt_prefix}/worker.log"),
            expected_sha256=log_digest,
        )
        records.append({
            "path": "worker.log",
            "sha256": log_digest,
            "size_bytes": log_path.stat().st_size,
            "storage_ref": log_cloud.storage_ref,
        })
        catalog = {
            "schema_version": "mplus-cloud-run-result/v1",
            "run_id": run["run_id"],
            "attempt": int(run["attempts"]),
            "exit_code": exit_code,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "locked_versions": {
                "runner_version": run["runner_version"],
                "source_commit": run["source_commit"],
                "package_id": run["package_id"],
                "package_version": run["package_version"],
                "package_sha256": run["package_sha256"],
                "run_schema_version": run["run_schema_version"],
                "input_sha256": run["input_sha256"],
                "parent_run_id": run.get("parent_run_id"),
                "source_run_id": run.get("source_run_id"),
                "package_schema_version": run["package_schema_version"],
                "container_image_digest": dict(run.get("contract") or {}).get(
                    "container_image_digest"
                ),
                "input_bundle_sha256": dict(
                    dict(run.get("contract") or {}).get("input_bundle") or {}
                ).get("sha256"),
            },
            "files": records,
        }
        payload = (json.dumps(catalog, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        return self.storage.put_bytes(
            payload,
            key=self.storage.key(f"{attempt_prefix}/result.json"),
        ).__dict__


def _terminate_process_group(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)


def _terminal_status(
    exit_code: int, *, delivery_available: bool = False,
) -> tuple[str, str]:
    if exit_code == 0 and delivery_available:
        return "succeeded", "delivery_available"
    if exit_code == 0:
        return "failed", "metadata_only"
    if exit_code == 3:
        return "waiting_human_review", "review_available"
    if exit_code == 2:
        return "blocked", "metadata_only"
    return "failed", "metadata_only"


def _validate_delivery(run: dict, output_root: Path, data_root: Path) -> bool:
    """Validate the canonical finalized run before exposing delivery availability."""
    try:
        run_id = str(run.get("run_id") or "")
        resolver = RunArtifactResolver(Path(data_root) / "runs")
        manifest_ref = resolver.resolve_manifest(run_id)
        manifest = resolver.read_manifest(run_id, expected_sha256=manifest_ref.sha256)
        run_dir = manifest_ref.path.parent
        if manifest.get("status") != "succeeded" or manifest.get("bundle_finalized") is not True:
            return False
        if manifest.get("source_commit") != run.get("source_commit"):
            return False
        locked = manifest.get("run_provenance")
        expected_locked = {
            "cloud_run_id": run_id,
            "runner_version": run.get("runner_version"),
            "source_commit": run.get("source_commit"),
            "package_id": run.get("package_id"),
            "package_version": run.get("package_version"),
            "package_sha256": run.get("package_sha256"),
            "package_schema_version": run.get("package_schema_version"),
            "input_sha256": run.get("input_sha256"),
        }
        if not isinstance(locked, dict) or any(
            locked.get(key) != value for key, value in expected_locked.items()
        ):
            return False

        workbook = resolver.resolve_artifact(run_id, "workbook")
        audit = resolver.resolve_artifact(run_id, "audit")
        report_artifact = resolver.resolve_artifact(run_id, "run_report")
        if workbook.path.suffix.casefold() != ".xlsx" or not _valid_xlsx(workbook.path):
            return False
        audit_payload = _read_json_object(audit.path)
        report = _read_json_object(report_artifact.path)
        if audit_payload.get("status") != "pass":
            return False
        if (
            report.get("run_id") != run_id
            or report.get("output_sha256") != workbook.sha256
            or report.get("machine_audit_status") != "pass"
            or report.get("human_review_status") != "quality_pass"
            or report.get("quality_review_status") != "quality_pass"
            or report.get("release_decision") != "release_pass"
            or report.get("status") != "release_pass"
            or report.get("exit_code") != 0
        ):
            return False
        if not _same_run_file(run_dir, report.get("output_path"), workbook.path):
            return False
        if not _same_run_file(run_dir, report.get("audit_path"), audit.path):
            return False

        human_stage = (manifest.get("stages") or {}).get("human_review") or {}
        publish_stage = (manifest.get("stages") or {}).get("publish_feedback") or {}
        if human_stage.get("status") != "succeeded" or publish_stage.get("status") != "succeeded":
            return False
        review_path = _resolve_run_file(
            run_dir,
            report.get("quality_review_report_path")
            or report.get("human_review_report_path"),
        )
        if review_path is None:
            return False
        review = _read_json_object(review_path)
        reviewer_kind = report.get("reviewer_kind")
        if (
            review.get("status") != "complete"
            or review.get("quality_review_status") != "quality_pass"
            or review.get("human_review_status") not in {"quality_pass", "not_performed"}
            or review.get("stage_status") != "succeeded"
            or review.get("reviewer_kind") != reviewer_kind
            or review.get("machine_audit_sha256") != audit.sha256
            or not _same_run_file(run_dir, review.get("machine_audit_path"), audit.path)
        ):
            return False
        if reviewer_kind == "human":
            review_workbook = _resolve_run_file(run_dir, review.get("review_workbook_path"))
            if review_workbook is None or not _valid_xlsx(review_workbook):
                return False
            if review.get("review_workbook_sha256") != sha256_file(review_workbook):
                return False
        elif reviewer_kind == "independent_ai":
            attestation_path = _resolve_run_file(run_dir, review.get("review_attestation_path"))
            if attestation_path is None or review.get("review_attestation_sha256") != sha256_file(attestation_path):
                return False
            attestation = _read_json_object(attestation_path)
            if (
                attestation.get("schema_version") != "mplus-quality-review-attestation/v1"
                or attestation.get("reviewer_kind") != "independent_ai"
                or attestation.get("run_id") != run_id
                or attestation.get("verdict") != "quality_pass"
                or attestation.get("source_workbook_sha256") != workbook.sha256
                or attestation.get("machine_audit_sha256") != audit.sha256
            ):
                return False
        else:
            return False

        publish_refs = list(publish_stage.get("output_refs") or [])
        feedback_path = _resolve_run_file(run_dir, publish_refs[0] if publish_refs else "")
        if feedback_path is None:
            return False
        feedback = _read_json_object(feedback_path)
        release_refs = feedback.get("release_artifact_refs") or {}
        if (
            feedback.get("schema_version") != "mplus-publish-feedback/v1"
            or feedback.get("quality_review_status") != "quality_pass"
            or feedback.get("reviewer_kind") != reviewer_kind
            or not _same_run_file(run_dir, release_refs.get("workbook"), workbook.path)
            or not _same_run_file(run_dir, release_refs.get("audit"), audit.path)
            or not _same_run_file(run_dir, feedback.get("quality_review_ref"), review_path)
        ):
            return False
        return all(_valid_pdf(path) for path in _pdf_candidates(output_root, run_dir))
    except Exception:
        return False


def _valid_xlsx(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0 or not is_zipfile(path):
        return False
    with ZipFile(path) as archive:
        if archive.testzip() is not None:
            return False
    workbook = load_workbook(path, read_only=True, data_only=False)
    try:
        return bool(workbook.sheetnames)
    finally:
        workbook.close()


def _valid_pdf(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    reader = PdfReader(str(path), strict=True)
    return reader is not None


def _pdf_candidates(*roots: Path) -> list[Path]:
    candidates = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink() and path.suffix.casefold() == ".pdf":
                candidates[str(path.resolve())] = path
    return list(candidates.values())


def _read_json_object(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root is not an object")
    return payload


def _resolve_run_file(run_dir: Path, recorded_path: str | Path | None) -> Path | None:
    if not str(recorded_path or "").strip():
        return None
    path = resolve_run_local_path(run_dir, recorded_path)
    try:
        path.relative_to(run_dir)
    except ValueError:
        return None
    return path if path.is_file() and not path.is_symlink() else None


def _same_run_file(run_dir: Path, recorded_path: str | Path | None, expected: Path) -> bool:
    resolved = _resolve_run_file(run_dir, recorded_path)
    return resolved is not None and resolved == expected.resolve()
