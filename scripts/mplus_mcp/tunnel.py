"""Launch the existing stdio server through the official OpenAI tunnel client."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys

REPO = Path(__file__).resolve().parents[2]
CACHE = Path.home() / ".cache/mplus-mcp-prototype"
SAFE_ENV = {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}


def local_environment() -> dict[str, str]:
    """The engineering reader does not need the tunnel's API key or app secrets."""
    return {key: value for key, value in os.environ.items() if key in SAFE_ENV}


def private_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("目录必须由当前用户拥有、非符号链接且权限为 700")
    return path


def secret_reference(key_file: Path | None) -> str:
    if key_file is None:
        return "env:CONTROL_PLANE_API_KEY"
    path = key_file.expanduser().absolute()
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("密钥文件必须由当前用户拥有、非符号链接且仅当前用户可读")
    # Only the official client opens the explicitly supplied secret file.
    return "file:" + str(path)


def mcp_command(snapshot: Path, audit_dir: Path) -> str:
    return shlex.join([sys.executable, str(Path(__file__).resolve()), "stdio",
                       "--snapshot", str(snapshot.expanduser().absolute()),
                       "--audit-dir", str(audit_dir.expanduser().absolute())])


def native_command(args: argparse.Namespace, client: str, key_ref: str) -> list[str]:
    if args.action in {"status", "stop"}:
        return [client, "runtimes", args.action, args.alias, "--json"]
    if args.action == "prepare":
        return [client, "init", "--sample", "sample_mcp_stdio_local",
                "--profile", args.alias, "--profile-dir", str(args.profile_dir),
                "--tunnel-id", args.tunnel_id, "--mcp-command", mcp_command(args.snapshot, args.audit_dir),
                "--control-plane-api-key-ref", key_ref, "--health-listen-addr", "127.0.0.1:0"]
    if args.action == "connect":
        # Explicit tunnel ID prevents remote tunnel creation/name lookup.
        return [client, "runtimes", "connect", "--alias", args.alias,
                "--profile", args.alias, "--profile-dir", str(args.profile_dir),
                "--tunnel-id", args.tunnel_id, "--mcp-command", mcp_command(args.snapshot, args.audit_dir),
                "--runtime-api-key", key_ref, "--json"]
    result = [client, args.action, "--profile", args.alias, "--profile-dir", str(args.profile_dir),
              "--control-plane.api-key", key_ref, "--health.listen-addr", "127.0.0.1:0",
              "--mcp.stdio-send-initialized-notification"]
    if args.action == "doctor":
        result.append("--explain")
    return result


def check_alias(client: str, alias: str, tunnel_id: str, environment: dict[str, str]) -> None:
    result = subprocess.run([client, "runtimes", "list", "--json"], capture_output=True,
                            text=True, env=environment, timeout=10)
    if result.returncode:
        raise ValueError("无法读取现有本地隧道 alias；请先检查 tunnel-client runtimes list")
    try:
        aliases = json.loads(result.stdout)["aliases"]
        for record in aliases:
            if record.get("alias") == alias and record.get("tunnel_id") != tunnel_id:
                raise ValueError("此 alias 已绑定其他隧道；请使用新的 --alias，原连接保持不变")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("官方客户端返回了无法识别的本地 alias 清单") from exc


async def local_check(snapshot: Path, audit_dir: Path) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = shlex.split(mcp_command(snapshot, audit_dir))
    params = StdioServerParameters(command=command[0], args=command[1:], env=local_environment())
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            result = await session.initialize()
            tools = await session.list_tools()
            names = sorted(item.name for item in tools.tools)
            expected = ["fetch", "mplus_get_system_map", "mplus_trace_feature", "search"]
            if names != expected:
                raise ValueError("实际 MCP 工具清单与工程读取原型不一致")
            return {"status": "local_stdio_passed", "server": result.serverInfo.name,
                    "tools": names, "chatgpt_connected": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Loomtake ChatGPT 私有隧道启动；不创建远端隧道")
    parser.add_argument("action", choices=["check", "stdio", "prepare", "doctor", "run", "connect", "status", "stop"])
    parser.add_argument("--snapshot", type=Path, default=os.environ.get("MPLUS_MCP_SNAPSHOT"))
    parser.add_argument("--audit-dir", type=Path, default=CACHE / "audit")
    parser.add_argument("--tunnel-client", default=os.environ.get("MPLUS_TUNNEL_CLIENT", "tunnel-client"))
    parser.add_argument("--profile-dir", type=Path, default=CACHE / "tunnel-profiles")
    parser.add_argument("--alias", default="mplus-engineering")
    parser.add_argument("--tunnel-id", default=os.environ.get("MPLUS_TUNNEL_ID"))
    parser.add_argument("--key-file", type=Path, help="显式指定的私有 runtime key 文件；不打印内容")
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.alias):
            raise ValueError("alias 只接受 1 至 64 位字母、数字、下划线和连字符")
        if args.action in {"check", "stdio", "prepare", "connect"}:
            if args.snapshot is None:
                raise ValueError("请用 --snapshot 或 MPLUS_MCP_SNAPSHOT 指定已核对的工程快照")
            args.snapshot = Path(args.snapshot).expanduser().absolute()
            if not args.snapshot.is_file():
                raise ValueError("工程快照不存在")
            args.audit_dir = private_directory(args.audit_dir)
        if args.action == "stdio":
            os.chdir(REPO)
            os.execve(sys.executable, [sys.executable, "-m", "scripts.mplus_mcp.server", "serve",
                                      "--snapshot", str(args.snapshot), "--audit-dir", str(args.audit_dir)],
                      local_environment())
        if args.action == "check":
            result = asyncio.run(asyncio.wait_for(local_check(args.snapshot, args.audit_dir), timeout=20))
            print(json.dumps(result, ensure_ascii=False))
            return 0
        client = shutil.which(args.tunnel_client)
        if client is None:
            raise ValueError("找不到官方 tunnel-client；通过 --tunnel-client 指定已校验的二进制文件")
        if args.action in {"prepare", "connect"}:
            if not args.tunnel_id or not re.fullmatch(r"tunnel_[A-Za-z0-9_-]+", args.tunnel_id):
                raise ValueError("请用 --tunnel-id 或 MPLUS_TUNNEL_ID 提供 Platform 返回的实际 tunnel_id")
        args.profile_dir = private_directory(args.profile_dir)
        if args.action == "prepare" and any((args.profile_dir / (args.alias + suffix)).exists() for suffix in (".yaml", ".yml")):
            raise ValueError("同名 profile 已存在；请复用 doctor/run 或选择新的 --alias")
        key_ref = secret_reference(args.key_file)
        environment = local_environment()
        # Do not inherit ambient OpenAI API/admin keys or tunnel overrides.
        if args.key_file is None and os.environ.get("CONTROL_PLANE_API_KEY"):
            environment["CONTROL_PLANE_API_KEY"] = os.environ["CONTROL_PLANE_API_KEY"]
        if args.action in {"doctor", "run", "connect"} and args.key_file is None and not environment.get("CONTROL_PLANE_API_KEY"):
            raise ValueError("缺少 runtime key；请设置 CONTROL_PLANE_API_KEY 或提供 --key-file")
        if args.action == "connect":
            check_alias(client, args.alias, args.tunnel_id, environment)
            environment["MCP_STDIO_SEND_INITIALIZED_NOTIFICATION"] = "true"
        return subprocess.run(native_command(args, client, key_ref), env=environment, cwd=REPO).returncode
    except (ValueError, OSError, asyncio.TimeoutError, subprocess.TimeoutExpired) as exc:
        # Do not include library errors that could contain credentials or documents.
        message = str(exc) if type(exc) is ValueError else "本地文件、进程或 MCP 检查失败，请检查指定路径与运行环境"
        print(message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
