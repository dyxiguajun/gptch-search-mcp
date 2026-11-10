# GPTCH Search 本机插件

插件为 Codex 提供本机 stdio 搜索工具和 `private-search-research` Skill。Skill 根据研究目标和 `get_capabilities` 报告选择平台、预算与取证深度。

先完成[项目安装](../../README.md#快速开始)，再从项目根目录运行以下命令。

```bash
uv run --frozen python scripts/install_desktop_plugin.py
```

安装器通过 Codex CLI 注册个人 Marketplace，复制插件与 Skill，并生成当前机器使用的 `mcp.json`。重新连接客户端后，可先调用 `ping_local_server` 检查工具，再通过 `@GPTCH Search` 提出研究问题。平台需要登录时，可以调用 `open_login_panel` 打开本机入口。

安装器依赖项目的 `.venv` 与 `storage/archive.db`。移动工程或换机器后，重新运行安装脚本并更新插件配置。一个数据目录同一时间由一个网关进程使用。

标准 MCP、Codex、ChatGPT Work 和普通 Chat 的实测范围分别记录在[验证记录](../../docs/validation.md)。
