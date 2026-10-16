# Loomtake 工程读取 MCP 原型 v1

这是独立的本地 stdio MCP，用于读取固定快照中的工程文档、代码、Schema、能力注册表及静态 API 关系。它不导入 Loomtake 业务模块，不连接业务数据库，不提供生成、保存、审核或发布操作。

ChatGPT 网页接入使用官方 Secure MCP Tunnel，见 [网页接入说明](CHATGPT.md)。本机可双击 [连接 ChatGPT](连接ChatGPT.command) 完成本地检查并连接已有隧道；需要目标账号实际的 tunnel ID 和 runtime key 私有文件。启动封装已经提供，账号连接与网页实际查询尚未验收。

## 安装与使用

在项目根目录运行。使用独立 Python 3.10 环境，保持应用自己的 `.venv`、`pyproject.toml` 和 `uv.lock` 不变：

```bash
uv venv --python 3.10 ~/.cache/mplus-mcp-prototype/venv
uv pip sync --python ~/.cache/mplus-mcp-prototype/venv/bin/python \
  --only-binary :all: --require-hashes scripts/mplus_mcp/requirements.txt
```

创建新快照，默认只读取固定 HEAD 的已提交工程文件。目标文件必须不存在，避免覆盖已有快照：

```bash
~/.cache/mplus-mcp-prototype/venv/bin/python -m scripts.mplus_mcp.server build \
  --repo . --output ~/.cache/mplus-mcp-prototype/engineering.sqlite3
```

需要查询本机未提交的工程修改时，显式增加 `--working-tree`，并换一个输出文件名。构建结束会复核文件哈希，发生并发编辑则拒绝发布该快照。未跟踪文件只包括代码中明确列出的需求/导航文件，不扫描所有未跟踪资料。

供本地 MCP 客户端启动的命令：

```bash
~/.cache/mplus-mcp-prototype/venv/bin/python -m scripts.mplus_mcp.server serve \
  --snapshot ~/.cache/mplus-mcp-prototype/engineering.sqlite3 \
  --audit-dir ~/.cache/mplus-mcp-prototype/audit
```

客户端工作目录应为仓库根目录；使用绝对 Python 和快照路径。该命令通过 stdin/stdout 通信，不开放 HTTP 端口。不将它直接配置成远程或多人共享服务。

## 已提供的工具

| 工具 | 用法 |
|---|---|
| `mplus_get_system_map` | 返回能力 ID、页面名称、静态 API handler 与调用名；接口清单可以分页 |
| `mplus_trace_feature` | 按能力 ID 查权威入口、不变量与源码位置；返回的测试名只是参考，不是已通过结论 |
| `search` | 用需求关键词、函数名、模块名检索；多个词按 AND 匹配，支持 limit/offset |
| `fetch` | 用搜索/追踪返回的 chunk ID 读取片段、行号和内容哈希，不接受路径或 URL |

推荐顺序：系统地图 → 能力追踪 → 搜索相关前端/API/需求 → 读取原文片段。无命中时缩短关键词或使用能力 ID，不推断不存在该功能。

`mplus-source://` 是本地来源标识，不能在 ChatGPT 网页中直接打开；远程可引用链接需在后续接入阶段提供。当前 `search`/`fetch` 为本地扩展协议形状，不宣称已兼容 company knowledge 的完整返回契约。

## 来源、资源与安全边界

- 源码范围由 `index.py` 的路径白名单决定：当前应用代码、Schema 与明确列出的工程文档。业务数据库、客户资产、运行输出、日志、测试正文、历史资料及未列出的文档不进入索引。
- 每个返回带快照、源提交、工作区模式、生成时间及策略版本；片段带行号与脱敏状态。原始文件清单哈希保存在本地快照内。它不是实时运行状态，未提交快照也不能只凭 HEAD 复现。
- 构建前检查 Git blob 大小；单文件 2 MiB、总输入 64 MiB，片段最多 60 行/12,000 字符。查询读取预建 SQLite，只读连接、1 秒查询执行预算、最多 20 条/页、64,000 字节工具结果、并发上限 2。
- 处理已知凭据格式、敏感键、多行 Python 赋值、带凭据 URL、邮箱和本机用户路径。脱敏是保守的工程防护，不能识别全部商业秘密、编码后的凭据或个人信息；未完成内容分类和出站审查前，不连接远程模型或真实客户数据。
- 每个进入工具分发的调用记录独立本地审计；参数和响应只存临时密钥 HMAC。日志带链式哈希，失败时不返回查询正文。每进程审计上限 10 MiB；有效性错误及未知工具也经过安全错误转换与审计。
- 审计日志只提供本地调用追踪和相邻事件完整性检查；临时 HMAC 密钥不持久保存，不提供跨进程重算、独立签名或生产级防篡改保证。日志轮转、保留策略、OAuth/项目权限与多用户隔离属于后续阶段。
- 本地运行用户可访问快照即拥有该工程资料读取权。操作系统权限不是远程身份系统。不能将工具标记为只读就认为内容已经安全对外。

## 检查

索引测试不需要安装 MCP SDK；协议边界测试在独立环境运行：

```bash
~/.cache/mplus-mcp-prototype/venv/bin/python -m unittest \
  tests.test_mplus_mcp_index tests.test_mplus_mcp_protocol -v
```

已执行本地真实 stdio 初始化、工具发现、九项能力查询、搜索与读取、安全拒绝及审计校验。当前原型没有接入 ChatGPT，没有查询真实业务数据，没有进行正常业务负载下的性能验收。

SDK 使用仍维护的 `mcp==1.30.0`，完整依赖及哈希固定在本目录，使用可安装的二进制依赖避免修改本机编译环境。[官方 v1 SDK 说明](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)

后续阶段与授权范围见 [可行性评估](../../docs/plans/Loomtake读取型MCP可行性评估-v1.md)。
