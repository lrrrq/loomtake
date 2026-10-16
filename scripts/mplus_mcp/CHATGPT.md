# Loomtake ChatGPT 网页接入 v1

使用 OpenAI Secure MCP Tunnel 将现有工程读取 MCP 接入个人 ChatGPT。Loomtake 保持本地运行；该入口读取已选工程快照，提供系统地图、能力追踪、搜索和原文片段，不操作业务项目。

## 本机准备

从 [OpenAI 官方最新 release](https://github.com/openai/tunnel-client/releases/latest) 下载适合本机的 `tunnel-client`，对照同版 `SHA256SUMS.txt` 校验。运行 `tunnel-client help quickstart` 确认命令。启动器不自动下载、升级或替换二进制。

以下命令从仓库根目录运行。先设置非敏感路径：

```bash
export MPLUS_TUNNEL_CLIENT="$HOME/.cache/mplus-mcp-prototype/tunnel-client-v0.0.14/tunnel-client"
export MPLUS_MCP_SNAPSHOT="$HOME/.cache/mplus-mcp-prototype/engineering-20260917-v3.sqlite3"
```

这里的版本与快照名是本次已安装值。更换时指向已校验的实际文件；启动器不扫描或猜测最新快照。使用独立环境完成本地 MCP 初始化和工具发现：

```bash
~/.cache/mplus-mcp-prototype/venv/bin/python scripts/mplus_mcp/tunnel.py check
```

## 连接账号

在 [Platform 隧道设置](https://platform.openai.com/settings/organization/tunnels) 获取实际 `tunnel_id`，将隧道关联到要使用的 ChatGPT 工作区。运行账号需要 Tunnels Read + Use；创建隧道还需要 Manage。ChatGPT 开发者模式权限单独管理。[官方接入说明](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

设置 `MPLUS_TUNNEL_ID` 为该实际 ID。runtime API key 通过 `CONTROL_PLANE_API_KEY` 环境变量或显式 `--key-file /绝对路径/runtime.key` 提供。不要在聊天、源码或命令参数中粘贴密钥正文；私有文件需由当前用户拥有且权限为 `600`。启动器只检查指定文件元数据，内容由官方客户端读取。不得用 admin key 替代 runtime key。

先创建本地 profile；同名文件存在时保留原文件：

双击启动器会复用已有 `mplus-engineering.yaml` 或 `.yml`，继续执行 `doctor`、`connect` 和 `status`。需要自定义安装路径时，可设置 `MPLUS_MCP_PYTHON` 与 `MPLUS_TUNNEL_PROFILE_DIR`；复用不会覆盖 profile，也不会跳过检查或更改已有 alias 的隧道归属。

```bash
~/.cache/mplus-mcp-prototype/venv/bin/python scripts/mplus_mcp/tunnel.py prepare
~/.cache/mplus-mcp-prototype/venv/bin/python scripts/mplus_mcp/tunnel.py doctor
```

使用文件密钥时，上述命令与后续启动命令均附加相同 `--key-file`。profile 存在仓库外的 `~/.cache/mplus-mcp-prototype/tunnel-profiles`，只存密钥引用。隧道子进程启动工程读取服务时会移除 runtime key、其他 API key 和非必需环境变量。

长期运行使用官方管理进程，并核对状态：

```bash
~/.cache/mplus-mcp-prototype/venv/bin/python scripts/mplus_mcp/tunnel.py connect
~/.cache/mplus-mcp-prototype/venv/bin/python scripts/mplus_mcp/tunnel.py status
```

`connect` 总是提供实际 `--tunnel-id`，只接入已有隧道；同名 alias 已绑定其他隧道时保留原连接，使用新的 `--alias`。成功启动不代表就绪；以 `status` 中的 `process_running`、`healthy`、`ready` 和网页实测判断。需要仅在当前终端运行时改用 `run`，按 Ctrl+C 停止。管理进程用 `stop` 停止。电脑关机、休眠或断网时，ChatGPT 无法读取该本地服务。

## 网页使用与验收

保持隧道运行，在 [ChatGPT 插件](https://chatgpt.com/plugins) 创建开发者模式应用，Connection 选择 Tunnel 并选中该 ID。连接失败时先查看 `status`，再运行 `doctor`。找不到隧道时核对工作区关联和 Tunnels Read + Use。[官方网页连接步骤](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)

连接后在对话选择 Loomtake 应用，先要求：“调用 mplus_get_system_map，列出能力 ID，说明这份快照的版本与读取范围。”再要求搜索具体函数并用 `fetch` 读回原文，核对片段行号与本地审计。完成网页实际调用后才可标记接入通过。

`search`/`fetch` 当前是自定义 MCP 工具，不宣称已通过 Company Knowledge 专用协议验收；`mplus-source://` 是来源标识，不能当网页链接打开。调用结果为固定快照，不代表当前运行状态，也不会包含尚未构建进快照的新改动。真实业务数据、多人权限和生产安全验收仍在本封装之外。
