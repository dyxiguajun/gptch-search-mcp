# GPTCH Search MCP

GPTCH Search 是面向中文社区研究的本机搜索网关。MCP 客户端可以按平台搜索、筛选候选，并按需读取正文、字幕或讨论。搜索任务支持请求预算，结果可保存到本机 SQLite，供后续检索和分页读取。

## 平台能力

| 平台 | 0.5.0 实现与验证范围 |
| --- | --- |
| 哔哩哔哩 | 原生搜索、视频详情、经归属检查的 CC 字幕、评论和弹幕。符合条件的视频可选取帧。 |
| 掘金、SegmentFault 思否 | 原生文章搜索、正文与代码块读取。 |
| 小红书 | 登录后原生搜索和笔记说明文字读取已实测。 |
| 百度贴吧 | 登录后原生搜索和主帖第一楼读取已实测。 |
| 知乎 | 实验适配器。登录已核验，网关搜索实测遇到 HTTP 403。 |
| 微信公众号 | 搜狗微信索引可发现候选。原文跳转实测遇到验证码，客户端搜一搜待接入。 |

实际可用范围可通过 MCP 的 `get_capabilities` 查询。历史实测与客户端范围见 [验证记录](docs/validation.md)。

## 快速开始

安装 [uv](https://docs.astral.sh/uv/)。工程使用 Python 3.12，安装脚本会准备环境并生成本机 MCP 配置。

```bash
bash scripts/setup.sh
uv run gptch-search --data-dir ./storage search 'Python asyncio' --platform juejin --depth quick
```

Windows 可运行 `安装 Windows.bat`，也可在项目目录运行以下命令。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1
```

安装会生成 `config/mcp.local.json` 和 `config/mcp.local.toml`。将对应配置加入支持本机 stdio 的客户端。移动工程后可运行 `uv run gptch-search --data-dir ./storage config` 重新生成。Codex 本机插件另见 [安装说明](plugins/gptch-search/README.md)。

需要平台登录时，调用 `open_login_panel`，或运行以下命令。

```bash
uv run gptch-search --data-dir ./storage auth panel
```

浏览器平台需要本机 Chrome。视频视觉取证需要 FFmpeg。账号会话、搜索归档和缓存存放于本机数据目录，源码仓库只包含可复用的工程文件。

## MCP 调用

| 工具 | 用途 |
| --- | --- |
| `get_capabilities`、`ping_local_server` | 查询平台能力与本机服务状态 |
| `deep_search`、`get_task`、`manage_task` | 提交、查看、取消或恢复有预算的采集任务 |
| `fetch_content`、`search_archive` | 分页读取已归档内容与全文检索 |
| `inspect_video`、`read_video_evidence` | 提取或读取有限视频帧证据 |
| `auth_status`、`open_login_panel` | 查看会话状态与打开本机登录入口 |

每个搜索任务指定一个平台。可先设 `max_items=0` 获取候选，筛选后用 `content_ids` 定向采集。阅读 `get_task` 返回的状态、覆盖范围和错误，遇到 `partial` 时仍可通过 `fetch_content` 使用已经归档的内容。

## 项目结构与开发

- `src/personal_search/` 包含 MCP 服务、任务队列、归档和平台适配器。
- `plugins/gptch-search/` 包含本机插件、启动入口和研究 Skill。
- `scripts/` 提供安装、启动、排错和验证入口。
- `tests/` 包含离线契约、解析、归档和 stdio 测试。
- `references/manifest.json` 记录上游参考版本，复用来源见 [第三方许可证](LICENSES.md)。

```bash
uv run --frozen ruff check src tests scripts
uv run --frozen pytest -q
uv run --frozen python scripts/smoke_mcp.py
```

更多说明见 [架构](ARCHITECTURE.md) 和 [维护指南](MAINTENANCE.md)。
