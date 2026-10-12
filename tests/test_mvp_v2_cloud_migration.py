import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook, load_workbook
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from mvp_v2.artifact_service import ArtifactResolutionError, RunArtifactResolver
from mvp_v2.brief_artifact_store import BriefArtifactStore
from mvp_v2.cloud_migration import (
    CloudMigrationError,
    create_cloud_snapshot,
    restore_cloud_snapshot,
    validate_data_root,
    verify_cloud_snapshot,
)
from mvp_v2.identity_service import LocalIdentityAdapter
from mvp_v2.pipeline_runtime import resolve_pipeline_run
from mvp_v2.project_service import ProjectService
from mvp_v2.task_service import TaskService
from mvp_v2.workspace_store import WorkspaceStore


SOURCE_COMMIT = "a" * 40


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class CloudMigrationTests(unittest.TestCase):
    def _data_fixture(self, root: Path) -> dict:
        data_root = root / "old-machine" / "data"
        source_root = data_root / "sources"
        source_root.mkdir(parents=True)
        (source_root / "匿名资料.txt").write_text("匿名迁移资料\n", encoding="utf-8")
        package = data_root / "packages" / "launch" / "asset_package.json"
        package.parent.mkdir(parents=True)
        package.write_text('{"schema_version":"anonymous-fixture/v1"}\n', encoding="utf-8")
        (data_root / "secrets").mkdir()
        (data_root / "secrets" / "agent-confirmation.key").write_bytes(b"k" * 32)
        os.chmod(data_root / "secrets" / "agent-confirmation.key", 0o600)
        (data_root / "secrets.env").write_text("DEEPSEEK_API_KEY=must-not-export\n", encoding="utf-8")
        (data_root / ".env").write_text("DEEPSEEK_API_KEY=bare-env\n", encoding="utf-8")
        (data_root / ".env.local").write_text(
            "DEEPSEEK_API_KEY=local-env\n", encoding="utf-8"
        )

        store = WorkspaceStore(data_root / "workspace.sqlite3")
        store.initialize()
        identity = LocalIdentityAdapter(store)
        bootstrap = identity.bootstrap_owner(
            workspace_name="匿名工作区",
            display_name="匿名管理员",
            confirmed=True,
            token="portable-owner-token",
        )
        projects = ProjectService(store)
        project = projects.create_project(
            bootstrap["auth_token"],
            workspace_id=bootstrap["workspace_id"],
            name="匿名云迁移项目",
            category_id="launch",
            expected_workspace_version=0,
        )

        run_id = "run_portable_001"
        run_dir = data_root / "runs" / project["project_id"] / "runs" / run_id
        run_dir.mkdir(parents=True)
        plan = run_dir / "script_plan.json"
        plan.write_text(
            json.dumps({"schema_version": "mplus-script-plan/v2", "script_rows": []}),
            encoding="utf-8",
        )
        report = run_dir / "run_report.json"
        report.write_text('{"status":"waiting_human_review"}\n', encoding="utf-8")
        workbook = run_dir / "delivery.xlsx"
        book = Workbook()
        book.active.title = "脚本"
        book.active["A1"] = "匿名云迁移交付"
        book.save(workbook)
        pdf = run_dir / "delivery.pdf"
        document = canvas.Canvas(str(pdf))
        document.drawString(72, 720, "Anonymous cloud migration delivery")
        document.save()
        manifest = run_dir / "run_manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": "stage1-pipeline-runtime/v3",
                    "source_commit": SOURCE_COMMIT,
                    "run_id": run_id,
                    "project_name": project["name"],
                    "status": "waiting_human_review",
                    "stages": {
                        "script_plan": {
                            "stage_id": "script_plan",
                            "status": "succeeded",
                            "output_refs": [str(plan.resolve())],
                            "output_sha256": _sha(plan),
                        }
                    },
                    "artifacts": {
                        "run_report": {
                            "path": str(report.resolve()),
                            "sha256": _sha(report),
                            "size_bytes": report.stat().st_size,
                        },
                        "delivery_workbook": {
                            "path": str(workbook.resolve()),
                            "sha256": _sha(workbook),
                            "size_bytes": workbook.stat().st_size,
                        },
                        "delivery_pdf": {
                            "path": str(pdf.resolve()),
                            "sha256": _sha(pdf),
                            "size_bytes": pdf.stat().st_size,
                        }
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        bound = projects.bind_run(
            bootstrap["auth_token"],
            project_id=project["project_id"],
            run_id=run_id,
            source_kind="native",
            manifest_ref=f"artifact://run/{run_id}/manifest",
            manifest_sha256=_sha(manifest),
            expected_project_version=project["project_version"],
        )
        version = projects.create_project_version(
            bootstrap["auth_token"],
            project_id=project["project_id"],
            run_id=run_id,
            content_sha256=_sha(workbook),
            artifact_ref=f"artifact://run/{run_id}/delivery_workbook",
            expected_project_version=bound["project_version"],
        )
        brief_artifact = BriefArtifactStore(data_root).freeze({
            "项目名称": "匿名云迁移项目",
            "视频时长": 60,
        })
        agent_session = TaskService(store).create_agent_session(
            bootstrap["auth_token"], project_id=project["project_id"]
        )
        task = TaskService(store).create_task(
            bootstrap["auth_token"],
            agent_session_id=agent_session["agent_session_id"],
            task_type="cloud_restore_probe",
            payload_sha256=hashlib.sha256(b"anonymous-task").hexdigest(),
            expected_project_version=version["project_version"],
        )
        executing = TaskService(store).transition_task(
            bootstrap["auth_token"],
            task_id=task["task_id"],
            expected_task_version=task["task_version"],
            status="executing",
        )
        completed = TaskService(store).transition_task(
            bootstrap["auth_token"],
            task_id=task["task_id"],
            expected_task_version=executing["task_version"],
            status="completed",
            result_ref="artifact://task/cloud-restore/result",
        )
        return {
            "data_root": data_root,
            "source_root": source_root,
            "package": package,
            "token": bootstrap["auth_token"],
            "project": project,
            "version": version,
            "brief_artifact": brief_artifact,
            "task": completed,
            "run_id": run_id,
            "manifest_sha256": _sha(manifest),
        }

    def test_snapshot_restore_restarts_workspace_and_relocates_run_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._data_fixture(root)
            archive = root / "transfer" / "mplus-data.zip"
            exported = create_cloud_snapshot(
                fixture["data_root"],
                archive,
                source_root=fixture["source_root"],
                asset_packages=(fixture["package"],),
                source_commit=SOURCE_COMMIT,
            )
            self.assertEqual(exported["status"], "pass")
            self.assertEqual(exported["source_commit"], SOURCE_COMMIT)
            self.assertEqual(verify_cloud_snapshot(archive)["status"], "pass")
            with ZipFile(archive) as bundle:
                migration_manifest = json.loads(
                    bundle.read("migration-manifest.json").decode("utf-8")
                )
            self.assertNotIn("source_data_root", migration_manifest)

            restored_root = root / "cloud-server" / "data"
            with self.assertRaisesRegex(CloudMigrationError, "源码 SHA"):
                restore_cloud_snapshot(
                    archive,
                    restored_root,
                    source_commit="b" * 40,
                )
            self.assertFalse(restored_root.exists())
            restored = restore_cloud_snapshot(
                archive,
                restored_root,
                source_commit=SOURCE_COMMIT,
            )
            self.assertEqual(restored["status"], "restored")
            self.assertGreater(restored["bound_runs"]["artifacts_verified"], 0)
            restored_root = restored_root.resolve()
            self.assertEqual(restored["source_root"], str(restored_root / "sources"))
            self.assertEqual(
                restored["asset_packages"],
                [str(restored_root / "packages" / "launch" / "asset_package.json")],
            )
            self.assertFalse((restored_root / "secrets.env").exists())
            self.assertFalse((restored_root / ".env").exists())
            self.assertFalse((restored_root / ".env.local").exists())
            self.assertEqual(
                (restored_root / "secrets" / "agent-confirmation.key").read_bytes(),
                b"k" * 32,
            )
            self.assertEqual(
                (restored_root / "secrets" / "agent-confirmation.key").stat().st_mode & 0o777,
                0o600,
            )

            restarted_store = WorkspaceStore(restored_root / "workspace.sqlite3")
            projects = ProjectService(restarted_store)
            tasks = TaskService(restarted_store)
            self.assertEqual(
                projects.get_project(fixture["token"], fixture["project"]["project_id"])["name"],
                "匿名云迁移项目",
            )
            self.assertEqual(
                tasks.get_task(fixture["token"], fixture["task"]["task_id"])["status"],
                "completed",
            )
            self.assertEqual(
                projects.get_project(fixture["token"], fixture["project"]["project_id"])["current_version_id"],
                fixture["version"]["version_id"],
            )
            self.assertEqual(
                BriefArtifactStore(restored_root).read(
                    fixture["brief_artifact"]["brief_ref"],
                    fixture["brief_artifact"]["brief_sha256"],
                )["项目名称"],
                "匿名云迁移项目",
            )
            resolver = RunArtifactResolver(restored_root / "runs")
            self.assertEqual(
                resolver.read_manifest(
                    fixture["run_id"],
                    expected_sha256=fixture["manifest_sha256"],
                )["source_commit"],
                SOURCE_COMMIT,
            )
            self.assertEqual(
                resolver.resolve_artifact(fixture["run_id"], "run_report").path,
                (
                    restored_root
                    / "runs"
                    / fixture["project"]["project_id"]
                    / "runs"
                    / fixture["run_id"]
                    / "run_report.json"
                ).resolve(),
            )
            self.assertEqual(
                resolver.resolve_stage_artifact(fixture["run_id"], "script_plan").path.name,
                "script_plan.json",
            )
            restored_workbook = resolver.resolve_artifact(
                fixture["run_id"], "delivery_workbook"
            ).path
            restored_pdf = resolver.resolve_artifact(fixture["run_id"], "delivery_pdf").path
            self.assertEqual(
                load_workbook(restored_workbook, data_only=False)["脚本"]["A1"].value,
                "匿名云迁移交付",
            )
            self.assertEqual(len(PdfReader(str(restored_pdf)).pages), 1)
            resolved_release = resolve_pipeline_run(
                restored_root / "runs" / fixture["project"]["project_id"],
                fixture["run_id"],
                allowed_statuses={"waiting_human_review"},
                artifact_names={"run_report", "delivery_workbook", "delivery_pdf"},
            )
            self.assertEqual(resolved_release["delivery_workbook"], restored_workbook)
            self.assertEqual(validate_data_root(restored_root)["status"], "pass")

            with self.assertRaisesRegex(CloudMigrationError, "已存在"):
                restore_cloud_snapshot(
                    archive,
                    restored_root,
                    source_commit=SOURCE_COMMIT,
                )
            with self.assertRaisesRegex(CloudMigrationError, "已存在"):
                create_cloud_snapshot(
                    fixture["data_root"],
                    archive,
                    source_root=fixture["source_root"],
                    source_commit=SOURCE_COMMIT,
                )

    def test_export_rejects_external_dependencies_symlinks_and_missing_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._data_fixture(root)
            archive = root / "snapshot.zip"
            with self.assertRaisesRegex(CloudMigrationError, "data_root 外"):
                create_cloud_snapshot(
                    fixture["data_root"],
                    archive,
                    source_root=root / "external-sources",
                    source_commit=SOURCE_COMMIT,
                )
            link = fixture["data_root"] / "linked-data"
            link.symlink_to(fixture["source_root"], target_is_directory=True)
            with self.assertRaisesRegex(CloudMigrationError, "符号链接"):
                create_cloud_snapshot(
                    fixture["data_root"],
                    archive,
                    source_root=fixture["source_root"],
                    source_commit=SOURCE_COMMIT,
                )
            link.unlink()
            with self.assertRaisesRegex(CloudMigrationError, "40 位"):
                create_cloud_snapshot(
                    fixture["data_root"],
                    archive,
                    source_root=fixture["source_root"],
                    source_commit="unavailable",
                )

    def test_verify_rejects_tampered_file_and_unsafe_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture = self._data_fixture(root)
            archive = root / "snapshot.zip"
            create_cloud_snapshot(
                fixture["data_root"],
                archive,
                source_root=fixture["source_root"],
                source_commit=SOURCE_COMMIT,
            )
            tampered = root / "tampered.zip"
            with ZipFile(archive) as source, ZipFile(tampered, "w", ZIP_DEFLATED) as target:
                for info in source.infolist():
                    raw = source.read(info.filename)
                    if info.filename.endswith("匿名资料.txt"):
                        raw = b"tampered"
                    target.writestr(info, raw)
            with self.assertRaisesRegex(CloudMigrationError, "完整性"):
                verify_cloud_snapshot(tampered)

            unsafe = root / "unsafe.zip"
            with ZipFile(archive) as source, ZipFile(unsafe, "w", ZIP_DEFLATED) as target:
                for info in source.infolist():
                    target.writestr(info, source.read(info.filename))
                target.writestr("../escape", b"unsafe")
            with self.assertRaisesRegex(CloudMigrationError, "不安全路径"):
                verify_cloud_snapshot(unsafe)

    def test_relocated_absolute_path_does_not_accept_other_run_or_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_id = "run_portable_002"
            run_dir = root / "new" / "runs" / "project" / "runs" / run_id
            run_dir.mkdir(parents=True)
            report = run_dir / "run_report.json"
            report.write_text("{}", encoding="utf-8")
            manifest = run_dir / "run_manifest.json"
            manifest.write_text(
                json.dumps({
                    "run_id": run_id,
                    "artifacts": {
                        "run_report": {
                            "path": f"/old/data/runs/project/runs/other-run/{report.name}",
                            "sha256": _sha(report),
                            "size_bytes": report.stat().st_size,
                        }
                    },
                }),
                encoding="utf-8",
            )
            resolver = RunArtifactResolver(root / "new" / "runs")
            with self.assertRaisesRegex(ArtifactResolutionError, "ARTIFACT_SCOPE_VIOLATION"):
                resolver.resolve_artifact(run_id, "run_report")

            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["artifacts"]["run_report"]["path"] = (
                f"/old/data/runs/project/runs/{run_id}/{report.name}"
            )
            payload["artifacts"]["run_report"]["sha256"] = "f" * 64
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ArtifactResolutionError, "ARTIFACT_HASH_MISMATCH"):
                resolver.resolve_artifact(run_id, "run_report")


if __name__ == "__main__":
    unittest.main()
