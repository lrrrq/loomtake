# 第三方声明

原项目未授予一般使用许可，不改变第三方组件及素材自身的许可或既有权利。

## 图片 fixture

`tests/fixtures/v10_image_delivery/cc0-participant-group.jpg`：作者 LikamboisaacK；Wikimedia Commons [原始文件页面](https://commons.wikimedia.org/wiki/File:Participant_Group_picture.jpg) 标注 CC0 1.0。原有来源、SHA 和许可声明保留在同目录 JSON 中。该图片不是客户资料，CC0 权利独立适用。

其余三张图片为已有匿名测试 fixture。现有 fixture 来源及权利声明随文件保留；不是产品演示或客户验收证据。

## Python 依赖

本仓库只引用锁定依赖，不包含安装包、虚拟环境、依赖源代码、浏览器二进制或第三方字体。下表根据源锁文件和对应本机已安装包的许可元数据核对；安装时应保留依赖原有许可证、版权和通知。

| 包 | 锁定版本 | 许可元数据 |
|---|---|---|
| annotated-doc | 0.0.5 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.14.2 | MIT |
| attrs | 26.1.0 | MIT |
| certifi | 2026.7.22 | MPL-2.0 |
| charset-normalizer | 3.5.1 | MIT |
| click | 8.4.2 | BSD-3-Clause |
| colorama | 0.4.6 | 本机未安装，元数据尚未核对 |
| cos-python-sdk-v5 | 1.9.44 | MIT |
| crcmod | 1.7 | MIT |
| et-xmlfile | 2.0.0 | MIT |
| exceptiongroup | 1.3.1 | License :: OSI Approved :: MIT License |
| fastapi | 0.128.8 | MIT |
| greenlet | 3.5.5 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.19 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| jsonschema | 4.26.0 | MIT |
| jsonschema-specifications | 2025.9.1 | MIT |
| openpyxl | 3.1.5 | MIT |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | MIT-CMU |
| playwright | 1.62.0 | Apache-2.0 |
| pluggy | 1.6.0 | MIT |
| psycopg | 3.3.4 | LGPL-3.0-only |
| psycopg-binary | 3.3.4 | LGPL-3.0-only |
| pycryptodome | 3.23.0 | BSD, Public Domain |
| pydantic | 2.13.4 | MIT |
| pydantic-core | 2.46.4 | MIT |
| pyee | 13.0.1 | MIT |
| pygments | 2.21.0 | BSD-2-Clause |
| pypdf | 6.16.1 | BSD-3-Clause |
| pytest | 9.1.1 | MIT |
| python-multipart | 0.0.32 | Apache-2.0 |
| referencing | 0.37.0 | MIT |
| reportlab | 4.5.1 | BSD license (see license.txt for details), Copyright (c) 2000-2025, ReportLab Inc. |
| requests | 2.34.2 | Apache-2.0 |
| rpds-py | 0.30.0 | MIT |
| six | 1.17.0 | MIT |
| starlette | 0.52.1 | BSD-3-Clause |
| tomli | 2.4.1 | MIT |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.4 | MIT |
| tzdata | 2026.3 | 本机未安装，元数据尚未核对 |
| urllib3 | 2.7.0 | MIT |
| uvicorn | 0.39.0 | BSD-3-Clause |
| xmltodict | 1.0.4 | MIT |

Psycopg/psycopg-binary 为 LGPL，certifi 为 MPL；本仓库没有复制或修改这些库，其原有授权继续独立适用。若后续分发安装包、镜像、浏览器、字体或修改后的第三方代码，必须另行检查该分发形式的义务。本次不构成完整的法律兼容性认证。

Windows/时区条件依赖 colorama、tzdata 未在当前 macOS 环境安装，仅保留锁定引用，没有打包它们的内容。Dockerfile 引用的系统工具及镜像亦未被本次复制或分发；本次未构建镜像。

## 可选工程读取 MCP

独立工具通过 `scripts/mplus_mcp/requirements.in` 与哈希锁定文件引用 `mcp==1.30.0`，不捆绑 SDK 或依赖源码。该 SDK 使用 MIT 许可，版权声明为 Copyright (c) 2024 Anthropic, PBC，见 [SDK 原许可证](https://github.com/modelcontextprotocol/python-sdk/blob/v1.x/LICENSE)。此第三方授权不构成对 Loomtake 原项目的一般使用许可。官方 tunnel-client 需由维护者另行获取并遵守其许可，本仓库不分发其二进制；本次没有建立隧道、连接账号或调用模型。
