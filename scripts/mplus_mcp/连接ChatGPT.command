#!/bin/zsh
set -eu

MPLUS_REPO_DIR="${0:A:h:h:h}"
MPLUS_CACHE_DIR="$HOME/.cache/mplus-mcp-prototype"
MPLUS_PYTHON="${MPLUS_MCP_PYTHON:-$MPLUS_CACHE_DIR/venv/bin/python}"
MPLUS_PROFILE_DIR="${MPLUS_TUNNEL_PROFILE_DIR:-$MPLUS_CACHE_DIR/tunnel-profiles}"
MPLUS_LAUNCHER="$MPLUS_REPO_DIR/scripts/mplus_mcp/tunnel.py"
MPLUS_CLIENT_PATH="${MPLUS_TUNNEL_CLIENT:-$MPLUS_CACHE_DIR/tunnel-client-v0.0.14/tunnel-client}"
MPLUS_SNAPSHOT_PATH="${MPLUS_MCP_SNAPSHOT:-$MPLUS_CACHE_DIR/engineering-20260917-v3.sqlite3}"

trap 'print "连接未完成。请保留上方错误；本入口不会公开 Loomtake 或覆盖已有 profile。"' ZERR

if [[ ! -x "$MPLUS_PYTHON" || ! -x "$MPLUS_CLIENT_PATH" ]]; then
  print "未找到本次安装的 Python 或官方 tunnel-client，请查看同目录 CHATGPT.md。"
  exit 2
fi

"$MPLUS_PYTHON" "$MPLUS_LAUNCHER" check --snapshot "$MPLUS_SNAPSHOT_PATH"
print "本地检查通过。接下来连接你已有的 OpenAI 私有隧道。"
print "隧道设置：https://platform.openai.com/settings/organization/tunnels"

MPLUS_SELECTED_TUNNEL="${MPLUS_TUNNEL_ID:-}"
if [[ -z "$MPLUS_SELECTED_TUNNEL" ]]; then
  read -r 'MPLUS_SELECTED_TUNNEL?隧道 ID（tunnel_ 开头）：'
fi

MPLUS_KEY_PATH="${MPLUS_TUNNEL_KEY_FILE:-}"
if [[ -z "${CONTROL_PLANE_API_KEY:-}" && -z "$MPLUS_KEY_PATH" ]]; then
  print "请提供已保存的 runtime key 文件路径，文件权限须为 600。不要在此输入密钥正文。"
  read -r 'MPLUS_KEY_PATH?密钥文件完整路径：'
  if [[ -z "$MPLUS_KEY_PATH" ]]; then
    print "未提供运行凭据，连接未启动。"
    exit 2
  fi
fi

MPLUS_ARGS=(--snapshot "$MPLUS_SNAPSHOT_PATH" --tunnel-client "$MPLUS_CLIENT_PATH" --tunnel-id "$MPLUS_SELECTED_TUNNEL" --profile-dir "$MPLUS_PROFILE_DIR")
if [[ -n "$MPLUS_KEY_PATH" ]]; then
  MPLUS_ARGS+=(--key-file "$MPLUS_KEY_PATH")
fi

if [[ ! -e "$MPLUS_PROFILE_DIR/mplus-engineering.yaml" && ! -e "$MPLUS_PROFILE_DIR/mplus-engineering.yml" ]]; then
  "$MPLUS_PYTHON" "$MPLUS_LAUNCHER" prepare "${MPLUS_ARGS[@]}"
fi
"$MPLUS_PYTHON" "$MPLUS_LAUNCHER" doctor "${MPLUS_ARGS[@]}"
"$MPLUS_PYTHON" "$MPLUS_LAUNCHER" connect "${MPLUS_ARGS[@]}"
"$MPLUS_PYTHON" "$MPLUS_LAUNCHER" status --tunnel-client "$MPLUS_CLIENT_PATH"
print "请以以上 ready 状态为准；启动成功不等于网页接入已验收。"
print "ChatGPT 网页：https://chatgpt.com/plugins"
print "创建开发者模式应用，Connection 选择 Tunnel，再选择刚才的隧道。"
