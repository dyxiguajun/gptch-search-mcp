"""Actual stdio MCP handshake; this does not test ordinary ChatGPT chat integration."""

import asyncio
import json
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    with tempfile.TemporaryDirectory(prefix="gptch-mcp-smoke-") as folder:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "personal_search.server"],
            env={**os.environ, "GPTCH_DATA_DIR": folder},
        )
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            initialized = await session.initialize()
            tools = (await session.list_tools()).tools
            result = await session.call_tool("ping_local_server", {})
            print(
                json.dumps(
                    {
                        "protocol": initialized.protocolVersion,
                        "tools": [tool.name for tool in tools],
                        "ping": json.loads(result.content[0].text),
                        "ordinary_chatgpt_chat_verified": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )


if __name__ == "__main__":
    asyncio.run(main())
