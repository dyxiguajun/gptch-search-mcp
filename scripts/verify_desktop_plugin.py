"""Verify the installed plugin's launch wiring, never label this as a Chat UI test."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def payload(response):
    if response.isError:
        raise RuntimeError("MCP 工具返回协议错误")
    return json.loads(response.content[0].text)


def resolve_command(command: str, plugin: Path):
    """Enforce Agent Plugins command semantics before direct SDK execution."""
    if "/" not in command and "\\" not in command and ":" not in command:
        return command
    if command.startswith("./"):
        resolved = (plugin / command).resolve()
        if resolved.is_relative_to(plugin.resolve()) and resolved.is_file():
            return str(resolved)
    raise ValueError(
        "Agent Plugins stdio command must be a bare executable name or a contained ./ path"
    )


async def verify(project: Path, *, quick=False):
    install = json.loads((project / "docs/desktop-integration/install.json").read_text())
    plugin = Path(install["installation"]["installedPath"])
    wiring = json.loads((plugin / "mcp.json").read_text())["mcpServers"]["gptch-search"]
    parameters = StdioServerParameters(
        command=resolve_command(wiring["command"], plugin),
        args=[arg.replace("${PLUGIN_ROOT}", str(plugin)) for arg in wiring["args"]],
        env={**os.environ, **wiring["env"]},
        cwd=wiring.get("cwd", str(plugin)).replace("${PLUGIN_ROOT}", str(plugin)),
    )
    report = {
        "test_surface": "installed_plugin_stdio_client",
        "installed_plugin_path": str(plugin),
        "ordinary_chat_verified": False,
        "work_ui_verified": False,
        "codex_product_tool_call_verified": False,
        "fresh_stdio_sessions": [],
    }
    for number in range(2):
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            initialized = await client.initialize()
            names = [t.name for t in (await client.list_tools()).tools]
            ping = payload(await client.call_tool("ping_local_server", {}))
            assert ping["status"] == "ok" and ping["execution"] == "local"
            report["fresh_stdio_sessions"].append(
                {
                    "session": number + 1,
                    "protocol": initialized.protocolVersion,
                    "tools": names,
                    "ping": ping,
                }
            )
            if quick and number == 0:
                submitted = payload(
                    await client.call_tool(
                        "deep_search",
                        {
                            "request": {
                                "query": "FL Studio 20 Rawstyle Screech",
                                "depth": "quick",
                                "pages_per_keyword": 1,
                                "timeout_seconds": 45,
                                "max_requests": 15,
                            }
                        },
                    )
                )
                task_id = submitted["task_id"]
                deadline = time.monotonic() + 55
                while True:
                    result = payload(
                        await client.call_tool(
                            "get_task", {"task_id": task_id, "view": "candidates", "limit": 20}
                        )
                    )
                    if result["status"] in {
                        "completed",
                        "partial",
                        "failed",
                        "cancelled",
                        "interrupted",
                    }:
                        break
                    if time.monotonic() >= deadline:
                        await client.call_tool(
                            "manage_task", {"task_id": task_id, "action": "cancel"}
                        )
                        raise RuntimeError("验证任务未按时完成，已取消")
                    await asyncio.sleep(2)
                report["quick_search"] = result
    destination = project / "docs/desktop-integration/plugin-protocol-test.json"
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "fresh_stdio_sessions": len(report["fresh_stdio_sessions"]),
        "ping": "ok",
        "ordinary_chat_verified": False,
        "quick_status": report.get("quick_search", {}).get("status"),
        "candidate_count": report.get("quick_search", {}).get("candidate_count"),
        "error": report.get("quick_search", {}).get("error"),
        "evidence": str(destination),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            asyncio.run(verify(Path(__file__).resolve().parent.parent, quick=args.quick)),
            ensure_ascii=False,
            indent=2,
        )
    )
