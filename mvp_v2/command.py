from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from urllib.parse import quote

import httpx

from .readonly_agent import ReadonlyAgentService
from .runtime_config import default_profile_port, load_local_secrets, load_runtime_config
from .run_contract import sha256_file
from .script_type_adapter import category_id_for_script_type
from .workspace_store import WorkspaceServiceError
from .input_limits import cloud_run_input_limits


def _url() -> str:
    return str(
        os.environ.get("MPLUS_PUBLIC_URL")
        or f"http://127.0.0.1:{default_profile_port()}"
    ).rstrip("/")


def _session() -> tuple[str, str]:
    remote_token = str(os.environ.get("MPLUS_AGENT_AUTH_TOKEN") or "").strip()
    remote_workspace = str(os.environ.get("MPLUS_WORKSPACE_ID") or "").strip()
    if remote_token or remote_workspace:
        if not remote_token or not remote_workspace:
            raise RuntimeError(
                "MPLUS_AGENT_AUTH_TOKEN 与 MPLUS_WORKSPACE_ID 必须同时配置"
            )
        return remote_token, remote_workspace
    result = ReadonlyAgentService.from_runtime_config().reissue_local_owner_session(
        confirmed=True
    )
    return str(result["auth_token"]), str(result["workspace_id"])


def _request(
    method: str,
    path: str,
    *,
    token: str,
    json_body: dict | None = None,
    files: dict | None = None,
    data: dict | None = None,
) -> dict:
    try:
        response = httpx.request(
            method,
            f"{_url()}{path}",
            headers={"X-MPlus-Agent-Auth": token},
            json=json_body,
            files=files,
            data=data,
            timeout=120,
        )
    except httpx.HTTPError as exc:
        raise RuntimeError("Loomtake 工作台服务不可用；请先运行 mplus ui") from exc
    payload = response.json() if "application/json" in response.headers.get("content-type", "") else {}
    if response.is_error:
        detail = payload.get("detail") if isinstance(payload, dict) else None
        message = detail.get("message") if isinstance(detail, dict) else detail
        raise RuntimeError(str(message or f"Loomtake 请求失败：HTTP {response.status_code}"))
    return dict(payload)


def _project(token: str, project_id: str) -> dict:
    return _request("GET", f"/api/formal/projects/{project_id}", token=token)


def _print(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _package_summary(record: dict) -> dict:
    return {
        key: record.get(key)
        for key in (
            "package_id", "package_version", "category_id", "package_schema_version",
            "package_sha256", "manifest_sha256", "file_count", "size_bytes",
            "local_path", "storage_prefix", "alias",
        )
        if record.get(key) is not None
    }


def _project_import(args: argparse.Namespace) -> int:
    token, workspace_id = _session()
    project = _project(token, args.project_id)
    path = Path(args.file).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("导入文件不存在")
    with path.open("rb") as stream:
        result = _request(
            "POST",
            f"/api/formal/projects/{args.project_id}/brief/files"
            f"?workspace_id={workspace_id}&expected_project_version={project['project_version']}",
            token=token,
            files={"files": (path.name, stream, "application/octet-stream")},
            data={"critical": "true"},
        )
    _print(result)
    return 0


def _run_plan(args: argparse.Namespace) -> int:
    token, workspace_id = _session()
    project = _project(token, args.project_id)
    result = _request(
        "POST",
        f"/api/formal/projects/{args.project_id}/plans?workspace_id={workspace_id}",
        token=token,
        json_body={"expected_project_version": project["project_version"]},
    )
    _print(result)
    return 0


def _run_generate(args: argparse.Namespace) -> int:
    if args.confirm is not True:
        raise RuntimeError("生成脚本必须显式提供 --confirm")
    token, workspace_id = _session()
    project = _project(token, args.project_id)
    preview = _request(
        "GET",
        f"/api/formal/projects/{args.project_id}/plans/{args.plan_id}"
        f"?workspace_id={workspace_id}&expected_project_version={project['project_version']}",
        token=token,
    )
    if str(preview.get("plan_sha256") or "") != args.plan_sha:
        raise RuntimeError("PLAN_SHA_DRIFT：指定 Plan SHA 与当前不可变规划不一致")
    result = _request(
        "POST",
        f"/api/formal/projects/{args.project_id}/plans/{args.plan_id}/confirm"
        f"?workspace_id={workspace_id}",
        token=token,
        json_body={"expected_project_version": project["project_version"]},
    )
    _print(result)
    return 0


def _run_cloud(args: argparse.Namespace) -> int:
    from . import __version__
    from .cloud_storage import CosObjectStorage
    from .script_draft import PROMPT_VERSION, SCHEMA_VERSION

    token, workspace_id = _session()
    project = _project(token, args.project_id)
    category_id = str(args.category_id or project.get("category_id") or "")
    source = Path(args.brief).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError("云端运行 Brief 不存在")
    source_size = source.stat().st_size
    limits = cloud_run_input_limits()
    if source_size > limits["max_file_bytes"]:
        raise RuntimeError(f"云端单文件超过上限 {limits['max_file_bytes']} bytes")
    if source_size > limits["max_total_bytes"]:
        raise RuntimeError(f"云端输入清单超过总量上限 {limits['max_total_bytes']} bytes")
    digest = sha256_file(source)
    if source.stat().st_size != source_size:
        raise RuntimeError("云端输入文件在预检后发生变化，请重试")
    storage = CosObjectStorage.from_environment()
    uploaded = storage.put_file(
        source,
        key=storage.content_key(digest, namespace="inputs"),
        expected_sha256=digest,
    )
    result = _request(
        "POST",
        f"/api/formal/workspaces/{workspace_id}/cloud-runs",
        token=token,
        json_body={
            "project_id": args.project_id,
            "category_id": category_id,
            "package_ref": args.package,
            "input_ref": uploaded.storage_ref,
            "input_sha256": digest,
            "input_filename": source.name,
            "input_size_bytes": source_size,
            "input_files": [{
                "filename": source.name,
                "storage_ref": uploaded.storage_ref,
                "sha256": digest,
                "size_bytes": source_size,
                "role": "brief",
            }],
            "runner_version": __version__,
            "idempotency_key": f"cli_{uuid.uuid4().hex}",
            "invocation_source": "cli",
            "parent_run_id": args.parent_run_id or None,
            "source_run_id": args.source_run_id or None,
            "request": {
                "max_loops": args.max_loops,
                "quality_profile": args.quality_profile,
                "ai_provider": args.ai_provider,
                "ai_model": args.ai_model,
                "prompt_version": PROMPT_VERSION,
                "prompt_schema_version": SCHEMA_VERSION,
            },
        },
    )
    if args.async_mode:
        _print(result)
        return 0
    while result.get("status") not in {
        "succeeded", "waiting_human_review", "blocked", "failed", "cancelled"
    }:
        time.sleep(max(0.25, args.poll_seconds))
        result = _request(
            "GET",
            f"/api/formal/workspaces/{workspace_id}/cloud-runs/{result['run_id']}",
            token=token,
        )
    _print(result)
    return {
        "succeeded": 0,
        "waiting_human_review": 3,
        "blocked": 2,
        "failed": 1,
        "cancelled": 130,
    }[result["status"]]


def _run_execute(args: argparse.Namespace) -> int:
    runtime = load_runtime_config()
    if args.local:
        backend = "local"
    elif args.cloud:
        backend = "cloud"
    else:
        backend = runtime["execution_backend"]
    if backend == "cloud":
        if not args.ai_provider:
            args.ai_provider = "minimax-http"
        return _run_cloud(args)
    if not args.ai_provider:
        args.ai_provider = "minimax-cli"
    if args.async_mode:
        raise RuntimeError("本地运行不支持 --async；该参数仅用于云端任务")
    return _run_local_execute(args, runtime)


def _run_status(args: argparse.Namespace) -> int:
    token, workspace_id = _session()
    result = _request(
        "GET",
        f"/api/formal/workspaces/{workspace_id}/cloud-runs/{args.run_id}",
        token=token,
    )
    _print(result)
    return 0


def _run_download(args: argparse.Namespace) -> int:
    token, workspace_id = _session()
    metadata = _request(
        "GET",
        f"/api/formal/workspaces/{workspace_id}/cloud-runs/{args.run_id}/artifacts"
        f"?artifact_path={quote(args.artifact_path, safe='')}",
        token=token,
    )
    target = Path(args.output).expanduser().resolve()
    if target.exists():
        raise RuntimeError("下载目标已存在，拒绝覆盖")
    response = httpx.get(str(metadata["url"]), timeout=120, follow_redirects=True)
    response.raise_for_status()
    payload = response.content
    digest = hashlib.sha256(payload).hexdigest()
    if digest != metadata.get("sha256") or len(payload) != metadata.get("size_bytes"):
        raise RuntimeError("云端交付物下载后 SHA-256 或大小不一致")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    _print({
        "status": "downloaded",
        "run_id": args.run_id,
        "artifact_path": args.artifact_path,
        "output": str(target),
        "sha256": digest,
        "size_bytes": len(payload),
    })
    return 0


def _run_local_execute(args: argparse.Namespace, runtime: dict) -> int:
    from . import __version__
    from .excel_io import read_briefs
    from .package_registry import LocalPackageRegistry
    from .release_status import current_source_commit
    from .script_draft import PROMPT_VERSION, SCHEMA_VERSION

    source = Path(args.brief).expanduser().resolve()
    if not source.is_file():
        raise RuntimeError("本地运行 Brief 不存在")
    briefs = read_briefs(source)
    if len(briefs) != 1:
        raise RuntimeError("run execute 要求 Brief 仅包含一条项目记录")
    inferred_category = category_id_for_script_type(briefs[0]["脚本类型"])
    category_id = str(args.category_id or inferred_category)
    if category_id != inferred_category:
        raise RuntimeError("指定 category_id 与 Brief 脚本类型不一致")
    package = LocalPackageRegistry(runtime["package_registry_path"]).resolve(
        category_id,
        args.package,
    )
    run_id = f"local_{uuid.uuid4().hex}"
    output_root = Path(runtime["exports_root"]) / "cli-runs" / run_id
    output_root.mkdir(parents=True, exist_ok=False)
    digest = sha256_file(source)
    provenance = {
        "schema_version": "mplus-cloud-run/v2",
        "local_run_id": run_id,
        "runner_version": __version__,
        "cli_version": __version__,
        "source_commit": current_source_commit(),
        "requested_package_ref": args.package,
        "package_id": package["package_id"],
        "package_version": package["package_version"],
        "package_sha256": package["package_sha256"],
        "package_schema_version": package["package_schema_version"],
        "input_sha256": digest,
        "ai_provider": args.ai_provider,
        "ai_model": args.ai_model,
        "prompt_version": PROMPT_VERSION,
        "prompt_schema_version": SCHEMA_VERSION,
        "execution_backend": "local",
        "parent_run_id": args.parent_run_id or None,
        "source_run_id": args.source_run_id or None,
    }
    command = [
        sys.executable,
        "-m",
        "mvp_v2.launch_entry",
        "--brief",
        str(source),
        "--asset-package",
        package["local_path"],
        "--source-root",
        str(Path(package["local_path"]) / "sources"),
        "--output-dir",
        str(output_root),
        "--max-loops",
        str(args.max_loops),
        "--quality-profile",
        args.quality_profile,
        "--text-model",
        args.ai_model,
        "--writer-provider",
        args.ai_provider,
        "--editor-provider",
        args.ai_provider,
    ]
    if args.parent_run_id:
        command.extend(["--parent-run-id", args.parent_run_id])
    environment = dict(os.environ)
    environment["MPLUS_LOCKED_RUN_PROVENANCE"] = json.dumps(
        provenance, ensure_ascii=False, sort_keys=True
    )
    completed = subprocess.run(command, env=environment, cwd=Path(__file__).resolve().parents[1])
    from .package_usage import append_local_usage

    usage_status = {
        0: "succeeded",
        2: "blocked",
        3: "waiting_human_review",
    }.get(completed.returncode, "failed")
    append_local_usage(runtime["logs_root"], {
        "run_id": run_id,
        "requested_ref": args.package,
        "package_id": package["package_id"],
        "package_version": package["package_version"],
        "package_sha256": package["package_sha256"],
        "invocation_source": "local_cli",
        "status": usage_status,
    })
    _print({
        "run_id": run_id,
        "status": "finished",
        "execution_backend": "local",
        "exit_code": completed.returncode,
        "output_dir": str(output_root),
        "locked_versions": provenance,
    })
    return int(completed.returncode)


def _admin_status(_args: argparse.Namespace) -> int:
    token, workspace_id = _session()
    _print(_request("GET", f"/api/formal/workspaces/{workspace_id}/system", token=token))
    return 0


def _version(_args: argparse.Namespace) -> int:
    from .release_status import version_report

    _print(version_report())
    return 0


def _admin_release_status(_args: argparse.Namespace) -> int:
    from .release_status import version_report

    report = version_report(include_remote=True)
    _print(report)
    return 0 if report["publish"]["verdict"] == "PASS" else 1


def _admin_cloud_migrate(args: argparse.Namespace) -> int:
    from .cloud_runtime import PostgresCloudRunRepository

    repository = PostgresCloudRunRepository.from_environment()
    _print(repository.migrate(apply=args.apply))
    return 0


def _admin_package_sync(args: argparse.Namespace) -> int:
    from .cloud_packages import sync_packages
    from .cloud_runtime import PostgresCloudRunRepository
    from .cloud_storage import CosObjectStorage
    from .package_registry import LocalPackageRegistry, build_package_record

    runtime = load_runtime_config()
    registry = LocalPackageRegistry(runtime["package_registry_path"])
    package_paths = list(args.package or [])
    if not package_paths and not args.configured:
        raise RuntimeError("必须提供 --configured 或至少一个 --package")
    expected_by_category = {}
    if args.configured:
        from .script_type_adapter import SCRIPT_TYPE_METADATA

        configured = dict(runtime.get("asset_packages") or {})
        if runtime.get("asset_package") and "发布会" not in configured:
            configured["发布会"] = runtime["asset_package"]
        category_packages = {
            category_id_for_script_type(script_type): path
            for script_type, path in configured.items()
        }
        expected_categories = {item.category_id for item in SCRIPT_TYPE_METADATA}
        if set(category_packages) != expected_categories:
            missing = "、".join(sorted(expected_categories - set(category_packages))) or "无"
            extra = "、".join(sorted(set(category_packages) - expected_categories)) or "无"
            raise RuntimeError(f"六类 Formal 资产包配置不完整；缺失: {missing}；多余: {extra}")
        expected_by_category = category_packages
        package_paths.extend(str(item) for item in category_packages.values())
    unique_paths = []
    seen_paths = set()
    for path in package_paths:
        resolved = str(Path(path).expanduser().resolve())
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            unique_paths.append(resolved)
    records = [build_package_record(path) for path in unique_paths]
    if expected_by_category:
        records_by_path = {record["local_path"]: record for record in records}
        for category_id, package_path in expected_by_category.items():
            record = records_by_path[str(Path(package_path).expanduser().resolve())]
            if record["category_id"] != category_id:
                raise RuntimeError(
                    f"配置类别 {category_id} 与资产包 {record['category_id']} 不一致"
                )
    rows = registry.register_records(
        records,
        promote_formal=args.formal and not args.cloud,
    )
    result = {"registry_path": str(registry.path), "packages": rows, "count": len(rows)}
    if args.cloud:
        repository = PostgresCloudRunRepository.from_environment()
        repository.check_schema()
        storage = CosObjectStorage.from_environment()
        cloud_rows = sync_packages(
            records,
            storage,
            repository,
            promote_formal=args.formal,
        )
        if args.formal:
            registry.register_records(records, promote_formal=True)
        result["cloud"] = cloud_rows
    output = {
        "registry_path": result["registry_path"],
        "count": result["count"],
        "packages": [_package_summary(item) for item in result["packages"]],
    }
    if "cloud" in result:
        output["cloud"] = [
            {
                "status": item["status"],
                "package": _package_summary(item["package"]),
                "alias": item.get("alias"),
                "uploaded_file_count": item["uploaded_file_count"],
                "uploaded_size_bytes": item["uploaded_size_bytes"],
            }
            for item in result["cloud"]
        ]
    _print(output)
    return 0


def _admin_package_list(args: argparse.Namespace) -> int:
    from .package_registry import LocalPackageRegistry

    runtime = load_runtime_config()
    result = LocalPackageRegistry(runtime["package_registry_path"]).list()
    if args.full:
        _print(result)
    else:
        _print({
            "schema_version": result["schema_version"],
            "updated_at": result["updated_at"],
            "aliases": result["aliases"],
            "packages": [
                _package_summary(record)
                for _key, record in sorted(result["packages"].items())
            ],
        })
    return 0


def _admin_package_usage(args: argparse.Namespace) -> int:
    from .package_usage import local_usage_summary

    runtime = load_runtime_config()
    rows = local_usage_summary(runtime["logs_root"])
    if args.cloud:
        from .cloud_runtime import PostgresCloudRunRepository

        repository = PostgresCloudRunRepository.from_environment()
        repository.check_schema()
        rows.extend(repository.package_usage())
    _print({
        "schema_version": "mplus-package-usage-summary/v1",
        "profile": runtime["profile"],
        "rows": rows,
        "call_count": sum(int(row.get("call_count") or 0) for row in rows),
        "unknown_version_count": sum(
            int(row.get("call_count") or 0)
            for row in rows
            if row.get("package_version") is None
        ),
    })
    return 0


def _admin_package_rollback(args: argparse.Namespace) -> int:
    runtime = load_runtime_config()
    if args.cloud:
        from .cloud_runtime import PostgresCloudRunRepository

        repository = PostgresCloudRunRepository.from_environment()
        repository.check_schema()
        with repository.package_sync_lock():
            rows = repository.rollback_aliases(args.category_id or None)
        scope = "cloud"
    else:
        from .package_registry import LocalPackageRegistry

        rows = LocalPackageRegistry(runtime["package_registry_path"]).rollback_formal(
            args.category_id or None
        )
        scope = "local"
    _print({"status": "rolled_back", "scope": scope, "aliases": rows})
    return 0


def _admin_legacy_plan(args: argparse.Namespace) -> int:
    from .legacy_archive import build_legacy_upload_plan, write_legacy_upload_plan

    plan = build_legacy_upload_plan(
        args.archive_root,
        review_run_ids=args.review_run_id,
    )
    path = write_legacy_upload_plan(args.output, plan)
    _print({"path": str(path), **plan["summary"], "plan_sha256": plan["plan_sha256"]})
    return 0


def _admin_legacy_verify(args: argparse.Namespace) -> int:
    from .cloud_storage import CosObjectStorage
    from .legacy_archive import (
        LEGACY_RECEIPT_SCHEMA_VERSION,
        read_legacy_catalog,
        verify_legacy_upload_receipt,
    )

    catalog = read_legacy_catalog(args.catalog, verify_files=not args.cloud)
    if args.cloud:
        if catalog.get("schema_version") != LEGACY_RECEIPT_SCHEMA_VERSION:
            raise RuntimeError("云端校验需要 upload receipt")
        result = verify_legacy_upload_receipt(catalog, CosObjectStorage.from_environment())
    else:
        result = {"status": "pass", **dict(catalog.get("summary") or {})}
    _print(result)
    return 0


def _admin_legacy_upload(args: argparse.Namespace) -> int:
    from .cloud_runtime import PostgresCloudRunRepository
    from .cloud_storage import CosObjectStorage
    from .legacy_archive import read_legacy_catalog, upload_legacy_plan

    plan = read_legacy_catalog(args.plan, verify_files=True)
    repository = PostgresCloudRunRepository.from_environment()
    repository.check_schema()
    receipt = upload_legacy_plan(
        plan,
        CosObjectStorage.from_environment(),
        repository=repository,
        receipt_path=args.receipt,
    )
    _print({"receipt": str(Path(args.receipt).resolve()), **receipt["summary"], "receipt_sha256": receipt["receipt_sha256"]})
    return 0


def _admin_legacy_inventory(args: argparse.Namespace) -> int:
    from .legacy_archive import build_cold_archive_inventory, write_cold_archive_inventory

    inventory = build_cold_archive_inventory(args.archive_root)
    path = write_cold_archive_inventory(args.output, inventory)
    _print({
        "path": str(path),
        **inventory["summary"],
        "inventory_sha256": inventory["inventory_sha256"],
    })
    return 0


def _admin_legacy_inventory_verify(args: argparse.Namespace) -> int:
    from .legacy_archive import read_cold_archive_inventory

    inventory = read_cold_archive_inventory(args.inventory, verify_files=True)
    _print({
        "status": "pass",
        **inventory["summary"],
        "inventory_sha256": inventory["inventory_sha256"],
    })
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="mplus", description="Loomtake 单一工作台命令入口")
    commands = root.add_subparsers(dest="command", required=True)
    ui = commands.add_parser("ui", help="启动统一图形工作台")
    ui.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    commands.add_parser("doctor", help="检查本机运行条件")
    version = commands.add_parser("version", help="显示当前代码、Profile、后端和 Formal 版本")
    version.set_defaults(handler=_version)

    project = commands.add_parser("project", help="正式项目操作")
    project_commands = project.add_subparsers(dest="project_command", required=True)
    project_import = project_commands.add_parser("import", help="把需求文件导入已有正式项目")
    project_import.add_argument("--project-id", required=True)
    project_import.add_argument("--file", required=True)
    project_import.set_defaults(handler=_project_import)

    run = commands.add_parser("run", help="对已有正式项目执行规划或生成")
    run_commands = run.add_subparsers(dest="run_command", required=True)
    run_plan = run_commands.add_parser("plan", help="准备确定性规划")
    run_plan.add_argument("--project-id", required=True)
    run_plan.set_defaults(handler=_run_plan)
    run_generate = run_commands.add_parser("generate", help="从已核对规划生成脚本")
    run_generate.add_argument("--project-id", required=True)
    run_generate.add_argument("--plan-id", required=True)
    run_generate.add_argument("--plan-sha", required=True)
    run_generate.add_argument("--confirm", action="store_true")
    run_generate.set_defaults(handler=_run_generate)
    run_cloud = run_commands.add_parser("cloud", help="使用锁定的 Formal 资产包提交云端运行")
    run_cloud.add_argument("--project-id", required=True)
    run_cloud.add_argument("--brief", required=True)
    run_cloud.add_argument("--package", required=True, help="package_id[@formal|版本]")
    run_cloud.add_argument("--category-id", default="")
    run_cloud.add_argument("--parent-run-id", default="")
    run_cloud.add_argument("--source-run-id", default="", help="记录本次重跑的云端或历史来源 run")
    run_cloud.add_argument("--max-loops", type=int, default=3)
    run_cloud.add_argument("--quality-profile", choices=("draft", "client-review"), default="draft")
    run_cloud.add_argument(
        "--ai-provider",
        choices=("minimax-http", "deepseek-http"),
        default="minimax-http",
    )
    run_cloud.add_argument("--ai-model", default="MiniMax-M2.7")
    run_cloud.add_argument("--async", dest="async_mode", action="store_true", help="提交后立即返回")
    run_cloud.add_argument("--poll-seconds", type=float, default=2.0)
    run_cloud.set_defaults(handler=_run_cloud)
    run_execute = run_commands.add_parser("execute", help="按 Profile 执行本地或云端统一管线")
    run_execute.add_argument("--project-id", required=True)
    run_execute.add_argument("--brief", required=True)
    run_execute.add_argument("--package", required=True, help="package_id[@formal|版本]")
    run_execute.add_argument("--category-id", default="")
    run_execute.add_argument("--parent-run-id", default="")
    run_execute.add_argument("--source-run-id", default="")
    run_execute.add_argument("--max-loops", type=int, default=3)
    run_execute.add_argument("--quality-profile", choices=("draft", "client-review"), default="draft")
    run_execute.add_argument("--ai-provider", choices=("minimax-cli", "minimax-http", "deepseek-http"), default="")
    run_execute.add_argument("--ai-model", default="MiniMax-M2.7")
    run_execute.add_argument("--async", dest="async_mode", action="store_true")
    run_execute.add_argument("--poll-seconds", type=float, default=2.0)
    backend = run_execute.add_mutually_exclusive_group()
    backend.add_argument("--local", action="store_true")
    backend.add_argument("--cloud", action="store_true")
    run_execute.set_defaults(handler=_run_execute)
    run_status = run_commands.add_parser("status", help="查询云端 run 状态与锁定版本")
    run_status.add_argument("--run-id", required=True)
    run_status.set_defaults(handler=_run_status)
    run_download = run_commands.add_parser("download", help="用 10 分钟私有签名下载并复验 SHA")
    run_download.add_argument("--run-id", required=True)
    run_download.add_argument("--artifact-path", required=True)
    run_download.add_argument("--output", required=True)
    run_download.set_defaults(handler=_run_download)

    admin = commands.add_parser("admin", help="工作区管理操作")
    admin_commands = admin.add_subparsers(dest="admin_command", required=True)
    status = admin_commands.add_parser("status", help="读取脱敏系统状态")
    status.set_defaults(handler=_admin_status)
    release = admin_commands.add_parser("release", help="源码发布与版本漂移")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    release_status = release_commands.add_parser("status", help="比较本地源提交与 source-only 远端")
    release_status.set_defaults(handler=_admin_release_status)
    cloud = admin_commands.add_parser("cloud", help="云端 PostgreSQL 管理")
    cloud_commands = cloud.add_subparsers(dest="cloud_command", required=True)
    cloud_migrate = cloud_commands.add_parser("migrate", help="检查或显式应用数据库迁移")
    cloud_migrate_mode = cloud_migrate.add_mutually_exclusive_group(required=True)
    cloud_migrate_mode.add_argument("--check", action="store_true")
    cloud_migrate_mode.add_argument("--apply", action="store_true")
    cloud_migrate.set_defaults(handler=_admin_cloud_migrate)
    package = admin_commands.add_parser("package", help="不可变资产包注册表")
    package_commands = package.add_subparsers(dest="package_command", required=True)
    package_sync = package_commands.add_parser("sync", help="注册本地资产包，可选同步私有 COS")
    package_sync.add_argument("--package", action="append", default=[])
    package_sync.add_argument("--configured", action="store_true", help="注册当前配置的六类 Formal 包")
    package_sync.add_argument("--formal", action="store_true", help="提升为 Formal 别名")
    package_sync.add_argument("--cloud", action="store_true", help="同步到 COS 和 PostgreSQL")
    package_sync.set_defaults(handler=_admin_package_sync)
    package_list = package_commands.add_parser("list", help="查看当前资产包和 Formal 别名")
    package_list.add_argument("--full", action="store_true", help="包含逐文件 SHA 清单")
    package_list.set_defaults(handler=_admin_package_list)
    package_usage = package_commands.add_parser("usage", help="按别名、锁定版本、入口、后端和状态统计调用")
    package_usage.add_argument("--cloud", action="store_true", help="同时查询 PostgreSQL 云端与历史索引")
    package_usage.set_defaults(handler=_admin_package_usage)
    package_rollback = package_commands.add_parser("rollback", help="原子回滚 Formal 到上一个已验证版本")
    package_rollback.add_argument("--category-id", action="append", default=[])
    package_rollback.add_argument("--cloud", action="store_true")
    package_rollback.set_defaults(handler=_admin_package_rollback)

    legacy = admin_commands.add_parser("legacy", help="精选历史上云")
    legacy_commands = legacy.add_subparsers(dest="legacy_command", required=True)
    legacy_plan = legacy_commands.add_parser("plan", help="生成只读选择清单")
    legacy_plan.add_argument("--archive-root", required=True)
    legacy_plan.add_argument("--output", required=True)
    legacy_plan.add_argument("--review-run-id", action="append", default=[])
    legacy_plan.set_defaults(handler=_admin_legacy_plan)
    legacy_verify = legacy_commands.add_parser("verify", help="校验本地计划或云端回执")
    legacy_verify.add_argument("--catalog", required=True)
    legacy_verify.add_argument("--cloud", action="store_true")
    legacy_verify.set_defaults(handler=_admin_legacy_verify)
    legacy_upload = legacy_commands.add_parser("upload", help="按计划上传，不扫描计划外文件")
    legacy_upload.add_argument("--plan", required=True)
    legacy_upload.add_argument("--receipt", required=True)
    legacy_upload.set_defaults(handler=_admin_legacy_upload)
    legacy_inventory = legacy_commands.add_parser("inventory", help="生成全量本地冷归档文件和 SHA 清单")
    legacy_inventory.add_argument("--archive-root", required=True)
    legacy_inventory.add_argument("--output", required=True)
    legacy_inventory.set_defaults(handler=_admin_legacy_inventory)
    legacy_inventory_verify = legacy_commands.add_parser("inventory-verify", help="逐文件复验冷归档清单")
    legacy_inventory_verify.add_argument("--inventory", required=True)
    legacy_inventory_verify.set_defaults(handler=_admin_legacy_inventory_verify)
    return root


def main(argv: list[str] | None = None) -> int:
    load_local_secrets()
    args = parser().parse_args(argv)
    if args.command in {"ui", "doctor"}:
        from .local_ui import main as ui_main

        if args.command == "doctor":
            return ui_main(["--diagnose"])
        return ui_main(["--no-open"] if args.no_open else [])
    try:
        return int(args.handler(args))
    except (RuntimeError, WorkspaceServiceError) as exc:
        print(f"Loomtake 未执行：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
