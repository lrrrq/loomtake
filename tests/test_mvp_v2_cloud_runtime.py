from datetime import datetime, timedelta, timezone
import unittest

from mvp_v2.cloud_runtime import (
    CLOUD_RUN_SCHEMA_VERSION,
    CloudRuntimeError,
    MemoryCloudRunRepository,
    POSTGRES_SCHEMA_VERSION,
    POSTGRES_SCHEMA_STATEMENTS,
)
from mvp_v2.package_registry import package_record_sha256


def _package(version: int, digest: str) -> dict:
    record = {
        "package_id": "launch-core",
        "package_version": version,
        "category_id": "launch",
        "package_schema_version": "mplus-asset-package/v3",
        "manifest_sha256": digest * 64,
        "storage_prefix": f"mplus/packages/launch-core/{version}",
        "file_records": [{
            "path": "asset_package.json",
            "sha256": digest * 64,
            "size_bytes": 10,
            "storage_ref": f"cos://private-123/mplus/{version}?sha256={digest * 64}",
        }],
        "size_bytes": 10,
    }
    record["package_sha256"] = package_record_sha256({
        **record,
        "files": record["file_records"],
    })
    return record


class CloudRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.clock = [datetime(2026, 8, 24, tzinfo=timezone.utc)]
        self.repository = MemoryCloudRunRepository(now=lambda: self.clock[0])
        self.repository.register_package(_package(4, "a"))
        self.repository.promote_alias(
            category_id="launch",
            package_id="launch-core",
            package_version=4,
        )

    def _create(self, package_ref="launch-core", **overrides):
        values = {
            "workspace_id": "workspace_12345678",
            "project_id": "project_12345678",
            "category_id": "launch",
            "package_ref": package_ref,
            "input_ref": f"cos://private-123/input?sha256={'c' * 64}",
            "input_sha256": "c" * 64,
            "input_filename": "brief.xlsx",
            "runner_version": "1.0.0",
            "source_commit": "d" * 40,
            "container_image_digest": f"sha256:{'e' * 64}",
            "request": {
                "ai_provider": "deepseek-http",
                "ai_model": "deepseek-chat",
                "prompt_version": "prompt-v1",
                "prompt_schema_version": "schema-v1",
            },
        }
        values.update(overrides)
        return self.repository.create_run(**values)

    def test_alias_is_resolved_once_and_old_run_keeps_locked_version(self):
        old = self._create()
        self.repository.register_package(_package(5, "b"))
        self.repository.promote_alias(
            category_id="launch",
            package_id="launch-core",
            package_version=5,
        )
        new = self._create()

        self.assertEqual(old["package_version"], 4)
        self.assertEqual(old["package_sha256"], _package(4, "a")["package_sha256"])
        self.assertEqual(new["package_version"], 5)
        self.assertEqual(new["run_schema_version"], CLOUD_RUN_SCHEMA_VERSION)
        self.assertEqual(new["contract"]["requested_package_ref"], "launch-core")
        self.assertEqual(
            new["contract"]["resolved_package"]["package_sha256"],
            new["package_sha256"],
        )

        rollback = self.repository.rollback_aliases(["launch"])
        self.assertEqual(rollback[0]["package_version"], 4)
        after_rollback = self._create()
        self.assertEqual(after_rollback["package_version"], 4)

    def test_expired_lease_is_reclaimed_and_old_worker_cannot_finish(self):
        queued = self._create()
        first = self.repository.claim("worker-a", lease_seconds=30)
        self.assertEqual(first["run_id"], queued["run_id"])
        self.assertIsNone(self.repository.claim("worker-b", lease_seconds=30))

        self.clock[0] += timedelta(seconds=31)
        second = self.repository.claim("worker-b", lease_seconds=30)
        self.assertEqual(second["attempts"], 2)
        with self.assertRaisesRegex(CloudRuntimeError, "TASK_LEASE_LOST"):
            self.repository.finish(
                queued["run_id"],
                worker_id="worker-a",
                lease_token=first["lease_token"],
                status="succeeded",
                exit_code=0,
                artifact_availability="delivery_available",
            )
        completed = self.repository.finish(
            queued["run_id"],
            worker_id="worker-b",
            lease_token=second["lease_token"],
            status="succeeded",
            exit_code=0,
            artifact_availability="delivery_available",
            result_ref=f"cos://private-123/result?sha256={'e' * 64}",
            result_sha256="e" * 64,
        )
        self.assertEqual(completed["status"], "succeeded")
        self.assertIsNone(completed["lease_token"])

    def test_cancel_invalidates_active_worker_token(self):
        queued = self._create()
        claimed = self.repository.claim("worker-a", lease_seconds=30)

        cancelled = self.repository.cancel(queued["run_id"])

        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(cancelled["exit_code"], 130)
        with self.assertRaisesRegex(CloudRuntimeError, "TASK_LEASE_LOST"):
            self.repository.heartbeat(
                queued["run_id"],
                worker_id="worker-a",
                lease_token=claimed["lease_token"],
                lease_seconds=30,
            )

    def test_only_two_runs_execute_and_the_third_remains_queued(self):
        first = self._create()
        second = self._create()
        third = self._create()

        claimed_first = self.repository.claim("worker-a", lease_seconds=30)
        claimed_second = self.repository.claim("worker-b", lease_seconds=30)

        self.assertEqual(len({claimed_first["run_id"], claimed_second["run_id"]}), 2)
        self.assertIsNone(self.repository.claim("worker-c", lease_seconds=30))
        statuses = [
            self.repository.get_run(run["run_id"])["status"]
            for run in (first, second, third)
        ]
        self.assertEqual(statuses.count("executing"), 2)
        self.assertEqual(statuses.count("queued"), 1)

    def test_cloud_mode_requires_full_source_commit(self):
        with self.assertRaisesRegex(CloudRuntimeError, "40"):
            self.repository.create_run(
                workspace_id="workspace_12345678",
                project_id="project_12345678",
                category_id="launch",
                package_ref="launch-core",
                input_ref="cos://private/input",
                input_sha256="c" * 64,
                input_filename="brief.xlsx",
                runner_version="1.0.0",
                source_commit="unavailable",
                container_image_digest=f"sha256:{'e' * 64}",
                request={
                    "ai_provider": "deepseek-http",
                    "ai_model": "deepseek-chat",
                    "prompt_version": "prompt-v1",
                    "prompt_schema_version": "schema-v1",
                },
            )

    def test_postgres_contract_contains_skip_locked_and_no_sqlite_fallback(self):
        ddl = "\n".join(POSTGRES_SCHEMA_STATEMENTS)
        self.assertIn("mplus_cloud_runs", ddl)
        self.assertIn("mplus_schema_migrations", ddl)
        self.assertIn("mplus_package_alias_history", ddl)
        self.assertEqual(POSTGRES_SCHEMA_VERSION, 4)
        # Claim SQL lives in the repository method; source assertion protects the lease primitive.
        import inspect
        from mvp_v2.cloud_runtime import PostgresCloudRunRepository

        source = inspect.getsource(PostgresCloudRunRepository.claim)
        self.assertIn("FOR UPDATE SKIP LOCKED", source)
        finish_source = inspect.getsource(PostgresCloudRunRepository.finish)
        self.assertIn("lease_expires_at >= now()", finish_source)

    def test_package_file_list_must_match_package_sha(self):
        package = _package(6, "f")
        package["file_records"][0]["sha256"] = "e" * 64
        with self.assertRaisesRegex(CloudRuntimeError, "package_sha256"):
            self.repository.register_package(package)

    def test_rerun_can_bind_an_immutable_legacy_source_run(self):
        self.repository.register_legacy_run({
            "run_id": "legacy-1",
            "project_name": "匿名项目",
            "status": "succeeded",
            "artifact_availability": "delivery_available",
            "source_manifest_sha256": "1" * 64,
            "catalog_ref": f"cos://private-123/catalog?sha256={'2' * 64}",
            "catalog_sha256": "2" * 64,
            "delivery_files": [],
        })
        rerun = self._create(source_run_id="legacy-1")
        self.assertEqual(rerun["source_run_id"], "legacy-1")
        with self.assertRaisesRegex(CloudRuntimeError, "source_run_id"):
            self._create(source_run_id="missing")

    def test_alias_batch_is_all_or_nothing(self):
        second = _package(4, "b")
        second.update({"package_id": "sports-core", "category_id": "sports"})
        second["package_sha256"] = package_record_sha256({
            **second,
            "files": second["file_records"],
        })
        self.repository.register_package(second)

        with self.assertRaisesRegex(CloudRuntimeError, "未注册"):
            self.repository.promote_aliases([
                {
                    "category_id": "launch",
                    "package_id": "launch-core",
                    "package_version": 4,
                },
                {
                    "category_id": "sports",
                    "package_id": "missing-core",
                    "package_version": 4,
                },
            ])

        self.assertNotIn(("sports", "formal"), self.repository._aliases)

    def test_run_input_capacity_and_bundle_sha_are_locked(self):
        run = self._create(input_size_bytes=123)
        self.assertEqual(run["contract"]["input_bundle"]["size_bytes"], 123)
        self.assertEqual(len(run["contract"]["input_bundle"]["sha256"]), 64)

        with self.assertRaisesRegex(CloudRuntimeError, "500MiB"):
            self._create(input_size_bytes=500 * 1024 * 1024 + 1)

    def test_idempotency_key_returns_the_original_read_only_run(self):
        first = self._create(idempotency_key="client-retry-key-0001")
        second = self._create(idempotency_key="client-retry-key-0001")

        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(first["contract"]["idempotency_key"], "client-retry-key-0001")


if __name__ == "__main__":
    unittest.main()
