from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import secrets
import threading
import uuid

from .package_registry import (
    DEFAULT_ALIAS,
    PackageRegistryError,
    package_record_sha256,
    parse_package_ref,
)
from .runtime_config import configured_source_commit
from .run_contract import sha256_json
from .input_limits import CLOUD_INPUT_FILE_BYTES, CLOUD_RUN_INPUT_BYTES, cloud_run_input_limits


CLOUD_RUN_SCHEMA_VERSION = "mplus-cloud-run/v2"
POSTGRES_SCHEMA_VERSION = 4
MAX_INPUT_FILE_BYTES = CLOUD_INPUT_FILE_BYTES
MAX_RUN_INPUT_BYTES = CLOUD_RUN_INPUT_BYTES
MAX_CONCURRENT_RUNS = 2
ARTIFACT_AVAILABILITIES = {
    "metadata_only",
    "delivery_available",
    "review_available",
}
TERMINAL_STATUSES = {
    "succeeded",
    "waiting_human_review",
    "blocked",
    "failed",
    "cancelled",
}
RUN_STATUSES = {"queued", "executing", *TERMINAL_STATUSES}


class CloudRuntimeError(RuntimeError):
    pass


POSTGRES_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS mplus_schema_migrations(
        version INTEGER PRIMARY KEY CHECK(version > 0),
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mplus_package_versions(
        package_id TEXT NOT NULL,
        package_version INTEGER NOT NULL CHECK(package_version > 0),
        category_id TEXT NOT NULL,
        package_schema_version TEXT NOT NULL,
        package_sha256 TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        storage_prefix TEXT NOT NULL,
        file_records_json JSONB NOT NULL,
        size_bytes BIGINT NOT NULL CHECK(size_bytes >= 0),
        registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(package_id, package_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mplus_package_aliases(
        category_id TEXT NOT NULL,
        alias TEXT NOT NULL CHECK(alias = 'formal'),
        package_id TEXT NOT NULL,
        package_version INTEGER NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(category_id, alias),
        FOREIGN KEY(package_id, package_version)
            REFERENCES mplus_package_versions(package_id, package_version)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mplus_package_alias_history(
        history_id BIGSERIAL PRIMARY KEY,
        category_id TEXT NOT NULL,
        alias TEXT NOT NULL CHECK(alias = 'formal'),
        previous_package_id TEXT,
        previous_package_version INTEGER,
        package_id TEXT NOT NULL,
        package_version INTEGER NOT NULL,
        changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY(package_id, package_version)
            REFERENCES mplus_package_versions(package_id, package_version)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mplus_cloud_users(
        github_id BIGINT PRIMARY KEY,
        github_login TEXT NOT NULL,
        organizations_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        last_login_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mplus_web_sessions(
        session_sha256 TEXT PRIMARY KEY,
        github_id BIGINT NOT NULL REFERENCES mplus_cloud_users(github_id) ON DELETE CASCADE,
        expires_at TIMESTAMPTZ NOT NULL,
        revoked_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mplus_cloud_runs(
        run_id TEXT PRIMARY KEY,
        parent_run_id TEXT REFERENCES mplus_cloud_runs(run_id) ON DELETE RESTRICT,
        source_run_id TEXT,
        workspace_id TEXT NOT NULL,
        project_id TEXT NOT NULL,
        category_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN (
            'queued','executing','succeeded','waiting_human_review',
            'blocked','failed','cancelled'
        )),
        package_id TEXT NOT NULL,
        package_version INTEGER NOT NULL,
        package_sha256 TEXT NOT NULL,
        package_schema_version TEXT NOT NULL,
        package_storage_prefix TEXT NOT NULL,
        package_files_json JSONB NOT NULL,
        runner_version TEXT NOT NULL,
        source_commit TEXT NOT NULL,
        run_schema_version TEXT NOT NULL,
        input_ref TEXT NOT NULL,
        input_sha256 TEXT NOT NULL,
        input_filename TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        contract_json JSONB NOT NULL,
        request_json JSONB NOT NULL,
        result_ref TEXT,
        result_sha256 TEXT,
        artifact_availability TEXT NOT NULL DEFAULT 'metadata_only'
            CHECK(artifact_availability IN ('metadata_only','delivery_available','review_available')),
        exit_code INTEGER,
        error_code TEXT,
        attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
        lease_owner TEXT,
        lease_token TEXT,
        lease_expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        FOREIGN KEY(package_id, package_version)
            REFERENCES mplus_package_versions(package_id, package_version)
            ON DELETE RESTRICT
    )
    """,
    """
    ALTER TABLE mplus_cloud_runs
    ADD COLUMN IF NOT EXISTS contract_json JSONB
    """,
    """
    UPDATE mplus_cloud_runs SET contract_json='{}'::jsonb WHERE contract_json IS NULL
    """,
    """
    ALTER TABLE mplus_cloud_runs ALTER COLUMN contract_json SET NOT NULL
    """,
    """
    ALTER TABLE mplus_cloud_runs ADD COLUMN IF NOT EXISTS idempotency_key TEXT
    """,
    """
    UPDATE mplus_cloud_runs SET idempotency_key=run_id WHERE idempotency_key IS NULL
    """,
    """
    ALTER TABLE mplus_cloud_runs ALTER COLUMN idempotency_key SET NOT NULL
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS mplus_cloud_runs_idempotency_idx
    ON mplus_cloud_runs(workspace_id,idempotency_key)
    """,
    """
    CREATE INDEX IF NOT EXISTS mplus_cloud_runs_claim_idx
    ON mplus_cloud_runs(status, lease_expires_at, created_at)
    """,
    """
    ALTER TABLE mplus_cloud_runs
    ADD COLUMN IF NOT EXISTS source_run_id TEXT
    """,
    """
    CREATE TABLE IF NOT EXISTS mplus_cloud_run_events(
        event_id BIGSERIAL PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES mplus_cloud_runs(run_id) ON DELETE RESTRICT,
        status TEXT NOT NULL,
        details_json JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS mplus_legacy_runs(
        run_id TEXT PRIMARY KEY,
        parent_run_id TEXT,
        project_name TEXT NOT NULL,
        category_id TEXT,
        status TEXT NOT NULL,
        artifact_availability TEXT NOT NULL
            CHECK(artifact_availability IN ('metadata_only','delivery_available','review_available')),
        source_manifest_sha256 TEXT NOT NULL,
        catalog_ref TEXT NOT NULL,
        catalog_sha256 TEXT NOT NULL,
        delivery_ref TEXT,
        delivery_sha256 TEXT,
        delivery_filename TEXT,
        delivery_files_json JSONB NOT NULL DEFAULT '[]'::jsonb,
        started_at TEXT,
        finished_at TEXT,
        imported_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    ALTER TABLE mplus_legacy_runs
    ADD COLUMN IF NOT EXISTS delivery_files_json JSONB NOT NULL DEFAULT '[]'::jsonb
    """,
)


class PostgresCloudRunRepository:
    def __init__(self, dsn: str, *, connect=None) -> None:
        self.dsn = str(dsn or "").strip()
        if not self.dsn:
            raise CloudRuntimeError("MPLUS_POSTGRES_DSN 未配置，拒绝回退到 SQLite")
        if connect is None:
            try:
                import psycopg
                from psycopg.rows import dict_row
            except ImportError as exc:
                raise CloudRuntimeError("未安装 psycopg[binary]") from exc

            def connect(value):
                return psycopg.connect(value, row_factory=dict_row)
        self._connect = connect

    @classmethod
    def from_environment(cls, environ: dict | None = None, *, connect=None):
        values = os.environ if environ is None else environ
        return cls(values.get("MPLUS_POSTGRES_DSN") or "", connect=connect)

    def initialize(self) -> None:
        """Backward-compatible explicit migration entrypoint."""
        self.migrate(apply=True)

    def migrate(self, *, apply: bool = False) -> dict:
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                if apply:
                    for statement in POSTGRES_SCHEMA_STATEMENTS:
                        cursor.execute(statement)
                    cursor.execute(
                        "INSERT INTO mplus_schema_migrations(version) VALUES (%s) ON CONFLICT DO NOTHING",
                        (POSTGRES_SCHEMA_VERSION,),
                    )
                else:
                    try:
                        cursor.execute(
                            "SELECT COALESCE(MAX(version),0) AS version FROM mplus_schema_migrations"
                        )
                    except Exception as exc:
                        raise CloudRuntimeError("云端 PostgreSQL Schema 未迁移") from exc
                cursor.execute(
                    "SELECT COALESCE(MAX(version),0) AS version FROM mplus_schema_migrations"
                )
                row = _row(cursor.fetchone())
        actual = int((row or {}).get("version") or 0)
        result = {
            "expected_version": POSTGRES_SCHEMA_VERSION,
            "actual_version": actual,
            "status": "current" if actual == POSTGRES_SCHEMA_VERSION else "outdated",
        }
        if actual != POSTGRES_SCHEMA_VERSION:
            raise CloudRuntimeError(
                f"云端 PostgreSQL Schema 版本不匹配: {actual} != {POSTGRES_SCHEMA_VERSION}"
            )
        return result

    def check_schema(self) -> dict:
        return self.migrate(apply=False)

    @contextmanager
    def package_sync_lock(self):
        connection = self._connect(self.dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_lock(hashtext(%s))",
                    ("mplus:package-sync",),
                )
            yield
        finally:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_advisory_unlock(hashtext(%s))",
                        ("mplus:package-sync",),
                    )
            finally:
                connection.close()

    def preflight_packages(self, records: list[dict]) -> None:
        for record in records:
            _validate_package_record(record, require_storage=False)
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                for record in records:
                    cursor.execute(
                        "SELECT package_sha256 FROM mplus_package_versions "
                        "WHERE package_id=%s AND package_version=%s",
                        (record["package_id"], record["package_version"]),
                    )
                    stored = _row(cursor.fetchone())
                    if stored and stored.get("package_sha256") != record["package_sha256"]:
                        raise CloudRuntimeError(
                            f"不可变资产包版本冲突: "
                            f"{record['package_id']}@{record['package_version']}"
                        )

    def register_package(self, record: dict) -> dict:
        return self.register_packages([record])[0]

    def register_packages(self, records: list[dict]) -> list[dict]:
        if not records:
            return []
        for record in records:
            _validate_package_record(record, require_storage=True)
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                stored_rows = []
                for record in records:
                    cursor.execute(
                        """
                        INSERT INTO mplus_package_versions(
                            package_id, package_version, category_id,
                            package_schema_version, package_sha256, manifest_sha256,
                            storage_prefix, file_records_json, size_bytes
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                        ON CONFLICT(package_id, package_version) DO NOTHING
                        """,
                        _package_values(record),
                    )
                    cursor.execute(
                        "SELECT * FROM mplus_package_versions "
                        "WHERE package_id=%s AND package_version=%s",
                        (record["package_id"], record["package_version"]),
                    )
                    stored = _row(cursor.fetchone())
                    if not stored or stored.get("package_sha256") != record["package_sha256"]:
                        raise CloudRuntimeError("不可变资产包版本冲突")
                    stored_rows.append(_normalize_package_row(stored))
        return stored_rows

    def promote_alias(
        self,
        *,
        category_id: str,
        package_id: str,
        package_version: int,
        alias: str = DEFAULT_ALIAS,
    ) -> dict:
        return self.promote_aliases([{
            "category_id": category_id,
            "package_id": package_id,
            "package_version": package_version,
            "alias": alias,
        }])[0]

    def promote_aliases(self, aliases: list[dict]) -> list[dict]:
        if not aliases:
            return []
        categories: set[str] = set()
        for item in aliases:
            normalized_alias = str(item.get("alias") or DEFAULT_ALIAS).casefold()
            if normalized_alias != DEFAULT_ALIAS:
                raise CloudRuntimeError("只允许 formal 作为可移动别名")
            category_id = _required(item.get("category_id"), "category_id")
            if category_id in categories:
                raise CloudRuntimeError(f"批次中 Formal 类别重复: {category_id}")
            categories.add(category_id)
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                prepared = []
                for item in aliases:
                    cursor.execute(
                        "SELECT category_id FROM mplus_package_versions "
                        "WHERE package_id=%s AND package_version=%s",
                        (item["package_id"], item["package_version"]),
                    )
                    found = _row(cursor.fetchone())
                    if not found:
                        raise CloudRuntimeError("待提升的资产包版本未注册")
                    if found["category_id"] != item["category_id"]:
                        raise CloudRuntimeError("别名类别与资产包类别不一致")
                    cursor.execute(
                        "SELECT package_id,package_version FROM mplus_package_aliases "
                        "WHERE category_id=%s AND alias=%s",
                        (item["category_id"], DEFAULT_ALIAS),
                    )
                    prepared.append((item, _row(cursor.fetchone())))
                rows = []
                for item, previous in prepared:
                    cursor.execute(
                        """
                        INSERT INTO mplus_package_aliases(
                            category_id, alias, package_id, package_version, updated_at
                        ) VALUES (%s,%s,%s,%s,now())
                        ON CONFLICT(category_id, alias) DO UPDATE SET
                            package_id=EXCLUDED.package_id,
                            package_version=EXCLUDED.package_version,
                            updated_at=now()
                        RETURNING *
                        """,
                        (item["category_id"], DEFAULT_ALIAS, item["package_id"], item["package_version"]),
                    )
                    row = _row(cursor.fetchone())
                    if not previous or (
                        previous.get("package_id"), previous.get("package_version")
                    ) != (item["package_id"], item["package_version"]):
                        cursor.execute(
                            """
                            INSERT INTO mplus_package_alias_history(
                                category_id,alias,previous_package_id,previous_package_version,
                                package_id,package_version
                            ) VALUES (%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                item["category_id"], DEFAULT_ALIAS,
                                (previous or {}).get("package_id"),
                                (previous or {}).get("package_version"),
                                item["package_id"], item["package_version"],
                            ),
                        )
                    rows.append(row)
                return rows

    def rollback_aliases(self, category_ids: list[str] | None = None) -> list[dict]:
        requested = [_required(item, "category_id") for item in (category_ids or [])]
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                if requested:
                    cursor.execute(
                        "SELECT * FROM mplus_package_aliases "
                        "WHERE alias=%s AND category_id=ANY(%s) ORDER BY category_id FOR UPDATE",
                        (DEFAULT_ALIAS, requested),
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM mplus_package_aliases "
                        "WHERE alias=%s ORDER BY category_id FOR UPDATE",
                        (DEFAULT_ALIAS,),
                    )
                current_rows = [_row(row) for row in cursor.fetchall()]
                if requested and {row["category_id"] for row in current_rows} != set(requested):
                    raise CloudRuntimeError("部分 Formal 类别不存在，拒绝部分回滚")
                prepared = []
                for current in current_rows:
                    cursor.execute(
                        """
                        SELECT previous_package_id,previous_package_version
                        FROM mplus_package_alias_history
                        WHERE category_id=%s AND alias=%s
                          AND package_id=%s AND package_version=%s
                          AND previous_package_id IS NOT NULL
                          AND previous_package_version IS NOT NULL
                        ORDER BY history_id DESC LIMIT 1
                        """,
                        (
                            current["category_id"], DEFAULT_ALIAS,
                            current["package_id"], current["package_version"],
                        ),
                    )
                    previous = _row(cursor.fetchone())
                    if not previous:
                        raise CloudRuntimeError(
                            f"类别 {current['category_id']} 没有可回滚的 Formal 前一版"
                        )
                    prepared.append((current, previous))
                result = []
                for current, previous in prepared:
                    cursor.execute(
                        """
                        UPDATE mplus_package_aliases SET
                            package_id=%s,package_version=%s,updated_at=now()
                        WHERE category_id=%s AND alias=%s RETURNING *
                        """,
                        (
                            previous["previous_package_id"],
                            previous["previous_package_version"],
                            current["category_id"], DEFAULT_ALIAS,
                        ),
                    )
                    row = _row(cursor.fetchone())
                    cursor.execute(
                        """
                        INSERT INTO mplus_package_alias_history(
                            category_id,alias,previous_package_id,previous_package_version,
                            package_id,package_version
                        ) VALUES (%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            current["category_id"], DEFAULT_ALIAS,
                            current["package_id"], current["package_version"],
                            previous["previous_package_id"],
                            previous["previous_package_version"],
                        ),
                    )
                    result.append(row)
        return result

    def create_run(
        self,
        *,
        workspace_id: str,
        project_id: str,
        category_id: str,
        package_ref: str,
        input_ref: str,
        input_sha256: str,
        input_filename: str,
        runner_version: str,
        input_size_bytes: int = 0,
        input_files: list[dict] | None = None,
        container_image_digest: str | None = None,
        invocation_source: str = "api",
        cli_version: str | None = None,
        idempotency_key: str | None = None,
        source_commit: str | None = None,
        parent_run_id: str | None = None,
        source_run_id: str | None = None,
        request: dict | None = None,
    ) -> dict:
        source_sha = _source_commit(source_commit or configured_source_commit())
        input_digest = _sha256(input_sha256, "input_sha256")
        image_digest = _container_digest(
            container_image_digest or os.environ.get("MPLUS_CONTAINER_IMAGE_DIGEST")
        )
        input_bundle = _input_bundle(
            input_ref=input_ref,
            input_sha256=input_digest,
            input_filename=input_filename,
            input_size_bytes=input_size_bytes,
            input_files=input_files,
        )
        request_body = dict(request or {})
        run_id = f"run_{uuid.uuid4().hex}"
        idempotency = _required(idempotency_key or run_id, "idempotency_key")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM mplus_cloud_runs WHERE workspace_id=%s AND idempotency_key=%s",
                    (_required(workspace_id, "workspace_id"), idempotency),
                )
                existing_run = _row(cursor.fetchone())
                if existing_run:
                    return _normalize_run(existing_run)
                package = self._resolve_package(cursor, category_id, package_ref)
                self._validate_run_references(
                    cursor,
                    workspace_id=workspace_id,
                    project_id=project_id,
                    parent_run_id=parent_run_id,
                    source_run_id=source_run_id,
                )
                contract = _run_contract(
                    run_id=run_id,
                    runner_version=runner_version,
                    cli_version=cli_version,
                    source_commit=source_sha,
                    container_image_digest=image_digest,
                    package_ref=package_ref,
                    package=package,
                    input_bundle=input_bundle,
                    invocation_source=invocation_source,
                    parent_run_id=parent_run_id,
                    source_run_id=source_run_id,
                    idempotency_key=idempotency,
                    request=request_body,
                )
                cursor.execute(
                    """
                    INSERT INTO mplus_cloud_runs(
                        run_id, parent_run_id, source_run_id, workspace_id, project_id, category_id,
                        status, package_id, package_version, package_sha256,
                        package_schema_version, package_storage_prefix, package_files_json,
                        runner_version, source_commit, run_schema_version,
                        input_ref, input_sha256, input_filename, idempotency_key,
                        contract_json, request_json
                    ) VALUES (
                        %s,%s,%s,%s,%s,%s,'queued',%s,%s,%s,%s,%s,%s::jsonb,
                        %s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb
                    ) RETURNING *
                    """,
                    (
                        run_id,
                        parent_run_id or None,
                        source_run_id or None,
                        _required(workspace_id, "workspace_id"),
                        _required(project_id, "project_id"),
                        _required(category_id, "category_id"),
                        package["package_id"],
                        package["package_version"],
                        package["package_sha256"],
                        package["package_schema_version"],
                        package["storage_prefix"],
                        json.dumps(package["file_records"], ensure_ascii=False, sort_keys=True),
                        _required(runner_version, "runner_version"),
                        source_sha,
                        CLOUD_RUN_SCHEMA_VERSION,
                        _required(input_ref, "input_ref"),
                        input_digest,
                        _filename(input_filename),
                        idempotency,
                        json.dumps(contract, ensure_ascii=False, sort_keys=True),
                        json.dumps(request_body, ensure_ascii=False, sort_keys=True),
                    ),
                )
                row = _row(cursor.fetchone())
                self._event(cursor, run_id, "queued", {"package_ref": package_ref})
        return _normalize_run(row)

    def get_run(self, run_id: str) -> dict:
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM mplus_cloud_runs WHERE run_id=%s", (run_id,))
                row = _row(cursor.fetchone())
        if not row:
            raise CloudRuntimeError("云端运行不存在")
        return _normalize_run(row)

    def cancel(self, run_id: str) -> dict:
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE mplus_cloud_runs SET
                        status='cancelled', lease_owner=NULL, lease_token=NULL,
                        lease_expires_at=NULL, finished_at=now(), updated_at=now(),
                        exit_code=130, error_code='CANCELLED_BY_USER'
                    WHERE run_id=%s AND status IN ('queued','executing')
                    RETURNING *
                    """,
                    (run_id,),
                )
                row = _row(cursor.fetchone())
                if row:
                    self._event(cursor, run_id, "cancelled", {"reason": "user_request"})
                else:
                    cursor.execute("SELECT * FROM mplus_cloud_runs WHERE run_id=%s", (run_id,))
                    row = _row(cursor.fetchone())
        if not row:
            raise CloudRuntimeError("云端运行不存在")
        return _normalize_run(row)

    def claim(self, worker_id: str, *, lease_seconds: int = 120) -> dict | None:
        if type(lease_seconds) is not int or lease_seconds < 30:
            raise CloudRuntimeError("lease_seconds 不得小于 30")
        token = secrets.token_urlsafe(32)
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    WITH candidate AS (
                        SELECT run_id FROM mplus_cloud_runs
                        WHERE (
                            status='queued'
                            OR (status='executing' AND lease_expires_at < now())
                          ) AND (
                            SELECT COUNT(*) FROM mplus_cloud_runs AS active
                            WHERE active.status='executing'
                              AND active.lease_expires_at >= now()
                          ) < %s
                        ORDER BY created_at, run_id
                        FOR UPDATE SKIP LOCKED
                        LIMIT 1
                    )
                    UPDATE mplus_cloud_runs AS runs SET
                        status='executing',
                        lease_owner=%s,
                        lease_token=%s,
                        lease_expires_at=now() + (%s * interval '1 second'),
                        attempts=runs.attempts + 1,
                        started_at=COALESCE(runs.started_at, now()),
                        updated_at=now()
                    FROM candidate
                    WHERE runs.run_id=candidate.run_id
                    RETURNING runs.*
                    """,
                    (
                        MAX_CONCURRENT_RUNS,
                        _required(worker_id, "worker_id"),
                        token,
                        lease_seconds,
                    ),
                )
                row = _row(cursor.fetchone())
                if row:
                    self._event(cursor, row["run_id"], "executing", {"worker_id": worker_id})
        return _normalize_run(row) if row else None

    def heartbeat(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_token: str,
        lease_seconds: int = 120,
    ) -> dict:
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE mplus_cloud_runs SET
                        lease_expires_at=now() + (%s * interval '1 second'),
                        updated_at=now()
                    WHERE run_id=%s AND status='executing'
                      AND lease_owner=%s AND lease_token=%s
                      AND lease_expires_at >= now()
                    RETURNING *
                    """,
                    (lease_seconds, run_id, worker_id, lease_token),
                )
                row = _row(cursor.fetchone())
        if not row:
            raise CloudRuntimeError("TASK_LEASE_LOST")
        return _normalize_run(row)

    def finish(
        self,
        run_id: str,
        *,
        worker_id: str,
        lease_token: str,
        status: str,
        exit_code: int,
        artifact_availability: str,
        result_ref: str | None = None,
        result_sha256: str | None = None,
        error_code: str | None = None,
    ) -> dict:
        if status not in TERMINAL_STATUSES:
            raise CloudRuntimeError("只能完成为终态")
        availability = _availability(artifact_availability)
        digest = _sha256(result_sha256, "result_sha256") if result_sha256 else None
        if bool(result_ref) != bool(digest):
            raise CloudRuntimeError("result_ref 与 result_sha256 必须同时存在或同时缺省")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE mplus_cloud_runs SET
                        status=%s, exit_code=%s,
                        artifact_availability=%s,
                        result_ref=%s, result_sha256=%s, error_code=%s,
                        lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL,
                        finished_at=now(), updated_at=now()
                    WHERE run_id=%s AND status='executing'
                      AND lease_owner=%s AND lease_token=%s
                      AND lease_expires_at >= now()
                    RETURNING *
                    """,
                    (
                        status,
                        int(exit_code),
                        availability,
                        result_ref,
                        digest,
                        error_code,
                        run_id,
                        worker_id,
                        lease_token,
                    ),
                )
                row = _row(cursor.fetchone())
                if row:
                    self._event(cursor, run_id, status, {"exit_code": int(exit_code)})
        if not row:
            raise CloudRuntimeError("TASK_LEASE_LOST")
        return _normalize_run(row)

    def register_legacy_run(self, record: dict) -> dict:
        availability = _availability(record.get("artifact_availability"))
        manifest_sha = _sha256(record.get("source_manifest_sha256"), "source_manifest_sha256")
        catalog_sha = _sha256(record.get("catalog_sha256"), "catalog_sha256")
        delivery_sha = record.get("delivery_sha256")
        if delivery_sha:
            delivery_sha = _sha256(delivery_sha, "delivery_sha256")
        delivery_files = list(record.get("delivery_files") or [])
        for item in delivery_files:
            _required(item.get("storage_ref"), "delivery storage_ref")
            _sha256(item.get("sha256"), "delivery sha256")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                values = (
                    _required(record.get("run_id"), "run_id"),
                    record.get("parent_run_id") or None,
                    str(record.get("project_name") or "未命名历史项目"),
                    record.get("category_id") or None,
                    _required(record.get("status"), "status"),
                    availability,
                    manifest_sha,
                    _required(record.get("catalog_ref"), "catalog_ref"),
                    catalog_sha,
                    record.get("delivery_ref") or None,
                    delivery_sha,
                    record.get("delivery_filename") or None,
                    json.dumps(delivery_files, ensure_ascii=False, sort_keys=True),
                    record.get("started_at") or None,
                    record.get("finished_at") or None,
                )
                cursor.execute(
                    """
                    INSERT INTO mplus_legacy_runs(
                        run_id,parent_run_id,project_name,category_id,status,
                        artifact_availability,source_manifest_sha256,
                        catalog_ref,catalog_sha256,delivery_ref,delivery_sha256,
                        delivery_filename,delivery_files_json,started_at,finished_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                    ON CONFLICT(run_id) DO NOTHING
                    """,
                    values,
                )
                cursor.execute("SELECT * FROM mplus_legacy_runs WHERE run_id=%s", (values[0],))
                stored = _row(cursor.fetchone())
        normalized = _normalize_legacy_row(stored)
        if (
            not normalized
            or normalized.get("source_manifest_sha256") != manifest_sha
            or normalized.get("catalog_sha256") != catalog_sha
            or normalized.get("artifact_availability") != availability
            or normalized.get("delivery_files") != delivery_files
        ):
            raise CloudRuntimeError("历史 run 不可变记录冲突")
        return normalized

    def list_legacy_runs(self, *, limit: int | None = 300, run_id: str | None = None) -> list[dict]:
        # None is used internally when merging local and archived sources before
        # project authorization and pagination. Public page sizes remain bounded.
        query = "SELECT * FROM mplus_legacy_runs"
        params = []
        if run_id is not None:
            query += " WHERE run_id=%s"
            params.append(run_id)
        query += " ORDER BY COALESCE(finished_at,started_at,'') DESC, run_id DESC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(max(1, min(int(limit), 1000)))
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query, tuple(params))
                return [_normalize_legacy_row(_row(row)) for row in cursor.fetchall()]

    def package_usage(self) -> list[dict]:
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COALESCE(contract_json->>'requested_package_ref','unknown') AS requested_ref,
                        package_id, package_version, package_sha256,
                        COALESCE(contract_json->>'invocation_source','unknown') AS invocation_source,
                        'cloud' AS execution_backend, status, COUNT(*) AS call_count
                    FROM mplus_cloud_runs
                    GROUP BY requested_ref,package_id,package_version,package_sha256,
                             invocation_source,status
                    ORDER BY package_id,package_version,status
                    """
                )
                rows = [_row(row) for row in cursor.fetchall()]
                cursor.execute(
                    """
                    SELECT 'unknown' AS requested_ref, 'unknown' AS package_id,
                           NULL AS package_version, 'unknown' AS package_sha256,
                           'legacy_import' AS invocation_source,
                           'legacy' AS execution_backend, status, COUNT(*) AS call_count
                    FROM mplus_legacy_runs GROUP BY status ORDER BY status
                    """
                )
                rows.extend(_row(row) for row in cursor.fetchall())
        return rows

    def create_web_session(
        self,
        *,
        github_id: int,
        github_login: str,
        organizations: list[str],
        session_sha256: str,
        expires_at: datetime,
    ) -> dict:
        digest = _sha256(session_sha256, "session_sha256")
        if type(github_id) is not int or github_id < 1:
            raise CloudRuntimeError("github_id 无效")
        login = _required(github_login, "github_login")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO mplus_cloud_users(
                        github_id,github_login,organizations_json,last_login_at
                    ) VALUES (%s,%s,%s::jsonb,now())
                    ON CONFLICT(github_id) DO UPDATE SET
                        github_login=EXCLUDED.github_login,
                        organizations_json=EXCLUDED.organizations_json,
                        last_login_at=now()
                    """,
                    (github_id, login, json.dumps(sorted(set(organizations)))),
                )
                cursor.execute(
                    """
                    INSERT INTO mplus_web_sessions(session_sha256,github_id,expires_at)
                    VALUES (%s,%s,%s)
                    """,
                    (digest, github_id, expires_at),
                )
        return {"github_id": github_id, "github_login": login, "expires_at": expires_at}

    def validate_web_session(self, session_sha256: str) -> dict | None:
        digest = _sha256(session_sha256, "session_sha256")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT users.github_id,users.github_login,users.organizations_json,
                           sessions.expires_at
                    FROM mplus_web_sessions AS sessions
                    JOIN mplus_cloud_users AS users ON users.github_id=sessions.github_id
                    WHERE sessions.session_sha256=%s
                      AND sessions.revoked_at IS NULL AND sessions.expires_at > now()
                    """,
                    (digest,),
                )
                row = _row(cursor.fetchone())
        return row or None

    def revoke_web_session(self, session_sha256: str) -> None:
        digest = _sha256(session_sha256, "session_sha256")
        with self._connect(self.dsn) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE mplus_web_sessions SET revoked_at=now() "
                    "WHERE session_sha256=%s AND revoked_at IS NULL",
                    (digest,),
                )

    def _resolve_package(self, cursor, category_id: str, package_ref: str) -> dict:
        try:
            package_id, selector = parse_package_ref(package_ref)
        except PackageRegistryError as exc:
            raise CloudRuntimeError(str(exc)) from exc
        if selector == DEFAULT_ALIAS:
            cursor.execute(
                """
                SELECT versions.* FROM mplus_package_aliases AS aliases
                JOIN mplus_package_versions AS versions
                  ON versions.package_id=aliases.package_id
                 AND versions.package_version=aliases.package_version
                WHERE aliases.category_id=%s AND aliases.alias=%s
                """,
                (category_id, DEFAULT_ALIAS),
            )
            row = _row(cursor.fetchone())
            if row and row.get("package_id") != package_id:
                raise CloudRuntimeError("指定 package_id 与 Formal 别名指向不一致")
        else:
            cursor.execute(
                """
                SELECT * FROM mplus_package_versions
                WHERE package_id=%s AND package_version=%s AND category_id=%s
                """,
                (package_id, int(selector), category_id),
            )
            row = _row(cursor.fetchone())
        if not row:
            raise CloudRuntimeError("云端资产包版本不存在")
        package = _normalize_package_row(row)
        _validate_package_record(package, require_storage=True)
        return package

    @staticmethod
    def _validate_run_references(
        cursor,
        *,
        workspace_id: str,
        project_id: str,
        parent_run_id: str | None,
        source_run_id: str | None,
    ) -> None:
        if parent_run_id:
            cursor.execute(
                "SELECT workspace_id,project_id FROM mplus_cloud_runs WHERE run_id=%s",
                (parent_run_id,),
            )
            parent = _row(cursor.fetchone())
            if not parent:
                raise CloudRuntimeError("parent_run_id 不存在")
            if parent.get("workspace_id") != workspace_id or parent.get("project_id") != project_id:
                raise CloudRuntimeError("parent_run_id 不属于当前项目")
        if not source_run_id:
            return
        cursor.execute(
            "SELECT workspace_id,project_id FROM mplus_cloud_runs WHERE run_id=%s",
            (source_run_id,),
        )
        source = _row(cursor.fetchone())
        if source:
            if source.get("workspace_id") != workspace_id or source.get("project_id") != project_id:
                raise CloudRuntimeError("source_run_id 不属于当前项目")
            return
        cursor.execute("SELECT run_id FROM mplus_legacy_runs WHERE run_id=%s", (source_run_id,))
        if not _row(cursor.fetchone()):
            raise CloudRuntimeError("source_run_id 不存在")

    @staticmethod
    def _event(cursor, run_id: str, status: str, details: dict) -> None:
        cursor.execute(
            "INSERT INTO mplus_cloud_run_events(run_id,status,details_json) VALUES (%s,%s,%s::jsonb)",
            (run_id, status, json.dumps(details, ensure_ascii=False, sort_keys=True)),
        )


class MemoryCloudRunRepository:
    """Deterministic contract implementation used by tests and local acceptance only."""

    def __init__(self, *, now=None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._packages: dict[tuple[str, int], dict] = {}
        self._aliases: dict[tuple[str, str], tuple[str, int]] = {}
        self._alias_history: list[dict] = []
        self._runs: dict[str, dict] = {}
        self._legacy_runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def initialize(self) -> None:
        return None

    def migrate(self, *, apply: bool = False) -> dict:
        return {
            "expected_version": POSTGRES_SCHEMA_VERSION,
            "actual_version": POSTGRES_SCHEMA_VERSION,
            "status": "current",
        }

    def check_schema(self) -> dict:
        return self.migrate()

    @contextmanager
    def package_sync_lock(self):
        yield

    def preflight_packages(self, records: list[dict]) -> None:
        for record in records:
            _validate_package_record(record, require_storage=False)
            existing = self._packages.get((record["package_id"], record["package_version"]))
            if existing and existing["package_sha256"] != record["package_sha256"]:
                raise CloudRuntimeError("不可变资产包版本冲突")

    def register_package(self, record: dict) -> dict:
        return self.register_packages([record])[0]

    def register_packages(self, records: list[dict]) -> list[dict]:
        for record in records:
            _validate_package_record(record, require_storage=True)
        with self._lock:
            for record in records:
                key = (record["package_id"], record["package_version"])
                existing = self._packages.get(key)
                if existing and existing["package_sha256"] != record["package_sha256"]:
                    raise CloudRuntimeError("不可变资产包版本冲突")
            for record in records:
                key = (record["package_id"], record["package_version"])
                self._packages.setdefault(key, deepcopy(record))
            return [
                deepcopy(self._packages[(item["package_id"], item["package_version"])])
                for item in records
            ]

    def promote_alias(self, *, category_id, package_id, package_version, alias=DEFAULT_ALIAS):
        return self.promote_aliases([{
            "category_id": category_id,
            "package_id": package_id,
            "package_version": package_version,
            "alias": alias,
        }])[0]

    def promote_aliases(self, aliases: list[dict]) -> list[dict]:
        with self._lock:
            categories: set[str] = set()
            prepared = []
            for item in aliases:
                alias = str(item.get("alias") or DEFAULT_ALIAS).casefold()
                category_id = item["category_id"]
                if alias != DEFAULT_ALIAS:
                    raise CloudRuntimeError("只允许 formal 作为可移动别名")
                if category_id in categories:
                    raise CloudRuntimeError(f"批次中 Formal 类别重复: {category_id}")
                key = (item["package_id"], item["package_version"])
                package = self._packages.get(key)
                if not package or package["category_id"] != category_id:
                    raise CloudRuntimeError("待提升的资产包版本未注册")
                categories.add(category_id)
                prepared.append((category_id, alias, key))
            for category_id, alias, key in prepared:
                previous = self._aliases.get((category_id, alias))
                self._aliases[(category_id, alias)] = key
                if previous != key:
                    self._alias_history.append({
                        "category_id": category_id,
                        "alias": alias,
                        "previous": previous,
                        "current": key,
                    })
        return [
            {
                "category_id": category_id,
                "alias": alias,
                "package_id": key[0],
                "package_version": key[1],
            }
            for category_id, alias, key in prepared
        ]

    def rollback_aliases(self, category_ids: list[str] | None = None) -> list[dict]:
        with self._lock:
            categories = sorted(
                set(category_ids or [category for category, alias in self._aliases if alias == DEFAULT_ALIAS])
            )
            prepared = []
            for category_id in categories:
                current = self._aliases.get((category_id, DEFAULT_ALIAS))
                previous = None
                for event in reversed(self._alias_history):
                    if (
                        event["category_id"] == category_id
                        and event["alias"] == DEFAULT_ALIAS
                        and event["current"] == current
                        and event["previous"] is not None
                    ):
                        previous = event["previous"]
                        break
                if current is None or previous not in self._packages:
                    raise CloudRuntimeError(
                        f"类别 {category_id} 没有可回滚的 Formal 前一版"
                    )
                prepared.append((category_id, current, previous))
            for category_id, current, previous in prepared:
                self._aliases[(category_id, DEFAULT_ALIAS)] = previous
                self._alias_history.append({
                    "category_id": category_id,
                    "alias": DEFAULT_ALIAS,
                    "previous": current,
                    "current": previous,
                })
        return [
            {
                "category_id": category_id,
                "alias": DEFAULT_ALIAS,
                "package_id": previous[0],
                "package_version": previous[1],
            }
            for category_id, _current, previous in prepared
        ]

    def create_run(self, **values) -> dict:
        package_id, selector = parse_package_ref(values["package_ref"])
        category_id = values["category_id"]
        input_bundle = _input_bundle(
            input_ref=values["input_ref"],
            input_sha256=values["input_sha256"],
            input_filename=values["input_filename"],
            input_size_bytes=values.get("input_size_bytes", 0),
            input_files=values.get("input_files"),
        )
        image_digest = _container_digest(
            values.get("container_image_digest")
            or os.environ.get("MPLUS_CONTAINER_IMAGE_DIGEST")
        )
        with self._lock:
            key = self._aliases.get((category_id, DEFAULT_ALIAS)) if selector == DEFAULT_ALIAS else (package_id, int(selector))
            package = self._packages.get(key) if key else None
            if not package or package["category_id"] != category_id or package["package_id"] != package_id:
                raise CloudRuntimeError("云端资产包版本不存在")
            now = self._now()
            idempotency = _required(
                values.get("idempotency_key") or f"run_{uuid.uuid4().hex}",
                "idempotency_key",
            )
            for existing in self._runs.values():
                if (
                    existing["workspace_id"] == values["workspace_id"]
                    and existing.get("idempotency_key") == idempotency
                ):
                    return _normalize_run(deepcopy(existing))
            run_id = f"run_{uuid.uuid4().hex}"
            source_commit = _source_commit(
                values.get("source_commit") or configured_source_commit()
            )
            request_body = deepcopy(values.get("request") or {})
            contract = _run_contract(
                run_id=run_id,
                runner_version=values["runner_version"],
                cli_version=values.get("cli_version"),
                source_commit=source_commit,
                container_image_digest=image_digest,
                package_ref=values["package_ref"],
                package=package,
                input_bundle=input_bundle,
                invocation_source=values.get("invocation_source") or "api",
                parent_run_id=values.get("parent_run_id"),
                source_run_id=values.get("source_run_id"),
                idempotency_key=idempotency,
                request=request_body,
            )
            row = {
                "run_id": run_id,
                "parent_run_id": values.get("parent_run_id") or None,
                "source_run_id": values.get("source_run_id") or None,
                "workspace_id": values["workspace_id"],
                "project_id": values["project_id"],
                "category_id": category_id,
                "status": "queued",
                "package_id": package["package_id"],
                "package_version": package["package_version"],
                "package_sha256": package["package_sha256"],
                "package_schema_version": package["package_schema_version"],
                "package_storage_prefix": package["storage_prefix"],
                "package_files": deepcopy(package["file_records"]),
                "runner_version": values["runner_version"],
                "source_commit": source_commit,
                "run_schema_version": CLOUD_RUN_SCHEMA_VERSION,
                "input_ref": values["input_ref"],
                "input_sha256": _sha256(values["input_sha256"], "input_sha256"),
                "input_filename": _filename(values["input_filename"]),
                "idempotency_key": idempotency,
                "contract": contract,
                "request": request_body,
                "artifact_availability": "metadata_only",
                "attempts": 0,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "created_at": now,
                "updated_at": now,
            }
            parent_id = row["parent_run_id"]
            if parent_id:
                parent = self._runs.get(parent_id)
                if not parent:
                    raise CloudRuntimeError("parent_run_id 不存在")
                if parent["workspace_id"] != row["workspace_id"] or parent["project_id"] != row["project_id"]:
                    raise CloudRuntimeError("parent_run_id 不属于当前项目")
            source_id = row["source_run_id"]
            if source_id:
                source = self._runs.get(source_id)
                if source:
                    if source["workspace_id"] != row["workspace_id"] or source["project_id"] != row["project_id"]:
                        raise CloudRuntimeError("source_run_id 不属于当前项目")
                elif source_id not in self._legacy_runs:
                    raise CloudRuntimeError("source_run_id 不存在")
            self._runs[run_id] = row
            return _normalize_run(deepcopy(row))

    def get_run(self, run_id: str) -> dict:
        with self._lock:
            row = self._runs.get(run_id)
            if not row:
                raise CloudRuntimeError("云端运行不存在")
            return _normalize_run(deepcopy(row))

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            row = self._runs.get(run_id)
            if not row:
                raise CloudRuntimeError("云端运行不存在")
            if row["status"] in {"queued", "executing"}:
                now = self._now()
                row.update({
                    "status": "cancelled",
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "finished_at": now,
                    "updated_at": now,
                    "exit_code": 130,
                    "error_code": "CANCELLED_BY_USER",
                })
            return _normalize_run(deepcopy(row))

    def claim(self, worker_id: str, *, lease_seconds: int = 120) -> dict | None:
        with self._lock:
            now = self._now()
            active_count = sum(
                1 for row in self._runs.values()
                if row["status"] == "executing" and row["lease_expires_at"] >= now
            )
            eligible = [
                row for row in self._runs.values()
                if active_count < MAX_CONCURRENT_RUNS and (
                    row["status"] == "queued"
                    or (row["status"] == "executing" and row["lease_expires_at"] < now)
                )
            ]
            if not eligible:
                return None
            row = sorted(eligible, key=lambda item: (item["created_at"], item["run_id"]))[0]
            row.update({
                "status": "executing",
                "lease_owner": worker_id,
                "lease_token": secrets.token_urlsafe(32),
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "attempts": row["attempts"] + 1,
                "started_at": row.get("started_at") or now,
                "updated_at": now,
            })
            return _normalize_run(deepcopy(row))

    def heartbeat(self, run_id: str, *, worker_id: str, lease_token: str, lease_seconds: int = 120) -> dict:
        with self._lock:
            row = self._runs.get(run_id)
            now = self._now()
            if not _owns_live_lease(row, worker_id, lease_token, now):
                raise CloudRuntimeError("TASK_LEASE_LOST")
            row["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
            row["updated_at"] = now
            return _normalize_run(deepcopy(row))

    def finish(self, run_id: str, *, worker_id: str, lease_token: str, status: str, exit_code: int, artifact_availability: str, result_ref=None, result_sha256=None, error_code=None) -> dict:
        if status not in TERMINAL_STATUSES:
            raise CloudRuntimeError("只能完成为终态")
        if bool(result_ref) != bool(result_sha256):
            raise CloudRuntimeError("result_ref 与 result_sha256 必须同时存在或同时缺失")
        with self._lock:
            row = self._runs.get(run_id)
            now = self._now()
            if not _owns_live_lease(row, worker_id, lease_token, now):
                raise CloudRuntimeError("TASK_LEASE_LOST")
            row.update({
                "status": status,
                "exit_code": int(exit_code),
                "artifact_availability": _availability(artifact_availability),
                "result_ref": result_ref,
                "result_sha256": _sha256(result_sha256, "result_sha256") if result_sha256 else None,
                "error_code": error_code,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "finished_at": now,
                "updated_at": now,
            })
            return _normalize_run(deepcopy(row))

    def register_legacy_run(self, record: dict) -> dict:
        normalized = {
            **deepcopy(record),
            "artifact_availability": _availability(record.get("artifact_availability")),
            "source_manifest_sha256": _sha256(
                record.get("source_manifest_sha256"), "source_manifest_sha256"
            ),
            "catalog_sha256": _sha256(record.get("catalog_sha256"), "catalog_sha256"),
            "delivery_files": list(record.get("delivery_files") or []),
        }
        run_id = _required(normalized.get("run_id"), "run_id")
        with self._lock:
            existing = self._legacy_runs.get(run_id)
            if existing and existing != normalized:
                raise CloudRuntimeError("历史 run 不可变记录冲突")
            self._legacy_runs.setdefault(run_id, normalized)
            return deepcopy(self._legacy_runs[run_id])

    def list_legacy_runs(self, *, limit: int | None = 300, run_id: str | None = None) -> list[dict]:
        with self._lock:
            rows = sorted(
                (item for item in self._legacy_runs.values() if run_id is None or item['run_id'] == run_id),
                key=lambda item: (str(item.get("finished_at") or item.get("started_at") or ""),
                                  str(item.get("run_id") or "")), reverse=True,
            )
            return deepcopy(rows if limit is None else rows[:max(1, min(int(limit), 1000))])

    def package_usage(self) -> list[dict]:
        counts: dict[tuple, int] = {}
        with self._lock:
            for run in self._runs.values():
                contract = dict(run.get("contract") or {})
                key = (
                    contract.get("requested_package_ref") or "unknown",
                    run.get("package_id") or "unknown",
                    run.get("package_version"),
                    run.get("package_sha256") or "unknown",
                    contract.get("invocation_source") or "unknown",
                    "cloud",
                    run.get("status") or "unknown",
                )
                counts[key] = counts.get(key, 0) + 1
            for run in self._legacy_runs.values():
                key = (
                    "unknown", "unknown", None, "unknown",
                    "legacy_import", "legacy", run.get("status") or "unknown",
                )
                counts[key] = counts.get(key, 0) + 1
        fields = (
            "requested_ref", "package_id", "package_version", "package_sha256",
            "invocation_source", "execution_backend", "status",
        )
        return [
            {**dict(zip(fields, key)), "call_count": count}
            for key, count in sorted(counts.items(), key=lambda item: str(item[0]))
        ]


def _owns_live_lease(row: dict | None, worker_id: str, lease_token: str, now: datetime) -> bool:
    return bool(
        row
        and row.get("status") == "executing"
        and row.get("lease_owner") == worker_id
        and row.get("lease_token") == lease_token
        and row.get("lease_expires_at") >= now
    )


def _package_values(record: dict) -> tuple:
    return (
        record["package_id"],
        record["package_version"],
        record["category_id"],
        record["package_schema_version"],
        record["package_sha256"],
        record["manifest_sha256"],
        record["storage_prefix"],
        json.dumps(record["file_records"], ensure_ascii=False, sort_keys=True),
        record["size_bytes"],
    )


def _validate_package_record(record: dict, *, require_storage: bool) -> None:
    for field in ("package_id", "category_id", "package_schema_version"):
        _required(record.get(field), field)
    if type(record.get("package_version")) is not int or record["package_version"] < 1:
        raise CloudRuntimeError("package_version 必须是正整数")
    _sha256(record.get("package_sha256"), "package_sha256")
    _sha256(record.get("manifest_sha256"), "manifest_sha256")
    if require_storage:
        _required(record.get("storage_prefix"), "storage_prefix")
        records = record.get("file_records")
        if not isinstance(records, list) or not records:
            raise CloudRuntimeError("file_records 不得为空")
        total = 0
        for item in records:
            _required(item.get("path"), "package file path")
            _sha256(item.get("sha256"), "package file sha256")
            _required(item.get("storage_ref"), "package file storage_ref")
            size = item.get("size_bytes")
            if type(size) is not int or size < 0:
                raise CloudRuntimeError("package file size_bytes 无效")
            total += size
        if total != record.get("size_bytes"):
            raise CloudRuntimeError("资产包文件大小汇总不一致")
        calculated = package_record_sha256({**record, "files": records})
        if calculated != record.get("package_sha256"):
            raise CloudRuntimeError("资产包文件清单与 package_sha256 不一致")


def _normalize_package_row(row: dict) -> dict:
    result = dict(row)
    records = result.pop("file_records_json", result.get("file_records", []))
    if isinstance(records, str):
        records = json.loads(records)
    result["file_records"] = records
    return result


def _normalize_run(row: dict | None) -> dict:
    if not row:
        return {}
    result = dict(row)
    for source, target in (
        ("package_files_json", "package_files"),
        ("contract_json", "contract"),
        ("request_json", "request"),
    ):
        value = result.pop(source, result.get(target, [] if target == "package_files" else {}))
        if isinstance(value, str):
            value = json.loads(value)
        result[target] = value
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return result


def _input_bundle(
    *,
    input_ref: str,
    input_sha256: str,
    input_filename: str,
    input_size_bytes: int,
    input_files: list[dict] | None,
) -> dict:
    primary = {
        "filename": _filename(input_filename),
        "storage_ref": _required(input_ref, "input_ref"),
        "sha256": _sha256(input_sha256, "input_sha256"),
        "size_bytes": input_size_bytes,
        "role": "brief",
    }
    source = input_files if input_files is not None else [primary]
    if not isinstance(source, list) or not source:
        raise CloudRuntimeError("input_files 不得为空")
    normalized = []
    total = 0
    limits = cloud_run_input_limits()
    for item in source:
        size = item.get("size_bytes")
        if type(size) is not int or size < 0:
            raise CloudRuntimeError("输入文件 size_bytes 无效")
        if size > limits["max_file_bytes"]:
            raise CloudRuntimeError(f"单个输入文件不得超过 {limits['max_file_bytes'] / (1024 * 1024):g}MiB（{limits['max_file_bytes']} bytes）")
        total += size
        normalized.append({
            "filename": _filename(item.get("filename")),
            "storage_ref": _required(item.get("storage_ref"), "input storage_ref"),
            "sha256": _sha256(item.get("sha256"), "input file sha256"),
            "size_bytes": size,
            "role": _required(item.get("role") or "attachment", "input role"),
        })
    if total > limits["max_total_bytes"]:
        raise CloudRuntimeError(f"单次 run 输入总量不得超过 {limits['max_total_bytes'] / (1024 * 1024 * 1024):g}GiB（{limits['max_total_bytes']} bytes）")
    if not any(
        item["storage_ref"] == primary["storage_ref"]
        and item["sha256"] == primary["sha256"]
        and item["filename"] == primary["filename"]
        for item in normalized
    ):
        raise CloudRuntimeError("主 Brief 未包含在完整输入清单中")
    payload = {
        "schema_version": "mplus-run-input-bundle/v1",
        "files": normalized,
        "size_bytes": total,
    }
    payload["sha256"] = sha256_json(payload)
    return payload


def _run_contract(
    *,
    run_id: str,
    runner_version: str,
    cli_version: str | None,
    source_commit: str,
    container_image_digest: str,
    package_ref: str,
    package: dict,
    input_bundle: dict,
    invocation_source: str,
    parent_run_id: str | None,
    source_run_id: str | None,
    idempotency_key: str,
    request: dict,
) -> dict:
    return {
        "schema_version": CLOUD_RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "runner_version": _required(runner_version, "runner_version"),
        "cli_version": _required(cli_version or runner_version, "cli_version"),
        "source_commit": source_commit,
        "container_image_digest": container_image_digest,
        "requested_package_ref": _required(package_ref, "package_ref"),
        "resolved_package": {
            "package_id": package["package_id"],
            "package_version": package["package_version"],
            "package_sha256": package["package_sha256"],
            "package_schema_version": package["package_schema_version"],
        },
        "input_bundle": input_bundle,
        "ai": {
            "provider": _required(request.get("ai_provider"), "ai_provider"),
            "model": _required(request.get("ai_model"), "ai_model"),
            "prompt_version": _required(request.get("prompt_version"), "prompt_version"),
            "schema_version": _required(
                request.get("prompt_schema_version"), "prompt_schema_version"
            ),
        },
        "invocation_source": _required(invocation_source, "invocation_source"),
        "execution_backend": "cloud",
        "parent_run_id": parent_run_id or None,
        "source_run_id": source_run_id or None,
        "idempotency_key": _required(idempotency_key, "idempotency_key"),
    }


def _container_digest(value: object) -> str:
    normalized = str(value or "").strip().casefold()
    if not normalized.startswith("sha256:") or len(normalized) != 71:
        raise CloudRuntimeError("container_image_digest 必须是 sha256:<64位十六进制>")
    _sha256(normalized[7:], "container_image_digest")
    return normalized


def _normalize_legacy_row(row: dict | None) -> dict:
    if not row:
        return {}
    result = dict(row)
    files = result.pop("delivery_files_json", result.get("delivery_files", []))
    if isinstance(files, str):
        files = json.loads(files)
    result["delivery_files"] = list(files or [])
    for key, value in list(result.items()):
        if isinstance(value, datetime):
            result[key] = value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return result


def _row(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        return dict(value)
    except (TypeError, ValueError) as exc:
        raise CloudRuntimeError("PostgreSQL 未返回字典行") from exc


def _source_commit(value: str) -> str:
    commit = str(value or "").strip().casefold()
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise CloudRuntimeError("云端运行必须绑定完整 40 位 source commit")
    return commit


def _sha256(value: object, field: str) -> str:
    digest = str(value or "").strip().casefold()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise CloudRuntimeError(f"{field} 必须是 SHA-256")
    return digest


def _required(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise CloudRuntimeError(f"{field} 不得为空")
    return normalized


def _filename(value: object) -> str:
    name = str(value or "").strip()
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise CloudRuntimeError("input_filename 必须是单个文件名")
    return name


def _availability(value: object) -> str:
    normalized = str(value or "")
    if normalized not in ARTIFACT_AVAILABILITIES:
        raise CloudRuntimeError("artifact_availability 无效")
    return normalized
