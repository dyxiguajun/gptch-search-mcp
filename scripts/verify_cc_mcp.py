"""Read live-validated CC archives through the real stdio MCP protocol."""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from personal_search.storage import atomic_json


async def verify(args):
    reports = [json.loads(Path(path).read_text()) for path in args.reports]
    archive = str(Path(args.archive_dir).resolve())
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "personal_search.server"],
        env={**os.environ, "GPTCH_DATA_DIR": archive},
    )
    evidence = {
        "archive_dir": archive,
        "product_ui_tested": False,
        "transport": "stdio",
        "platform_requests": 0,
        "samples": [],
    }
    async with (
        stdio_client(params) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        initialized = await session.initialize()
        evidence["protocol"] = initialized.protocolVersion
        evidence["tools"] = [t.name for t in (await session.list_tools()).tools]
        for report in reports:
            task_id = report["task_id"]
            for sample in report["samples"]:
                bvid = sample["bvid"]
                records, offset, pages = [], 0, 0
                while True:
                    response = await session.call_tool(
                        "fetch_content",
                        {
                            "task_id": task_id,
                            "content_id": bvid,
                            "section": "subtitles",
                            "offset": offset,
                            "limit": 50,
                        },
                    )
                    assert not response.isError
                    data = json.loads(response.content[0].text)
                    assert "error" not in data, data
                    records.extend(data["records"])
                    pages += 1
                    if not data["has_more"]:
                        break
                    assert data["next_offset"] > offset
                    offset = data["next_offset"]
                expected = [
                    {
                        "page": sample["page"],
                        "cid": sample["cid"],
                        "bvid": bvid,
                        "track_id": t["id"],
                        "language": t["language"],
                        **segment,
                    }
                    for t in sample["subtitles"].get("tracks", [])
                    for segment in t["segments"]
                ]
                assert records == expected
                evidence["samples"].append(
                    {
                        "task_id": task_id,
                        "bvid": bvid,
                        "cid": sample["cid"],
                        "record_count": len(records),
                        "pagination_calls": pages,
                        "exact_archive_match": True,
                        "coverage": data["coverage"],
                        "first_record": records[:1],
                    }
                )
    atomic_json(Path(args.output), evidence)
    print(
        json.dumps(
            {
                "protocol": evidence["protocol"],
                "samples": [
                    {
                        k: s[k]
                        for k in ("bvid", "record_count", "pagination_calls", "exact_archive_match")
                    }
                    for s in evidence["samples"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+")
    parser.add_argument("--archive-dir", required=True)
    parser.add_argument("--output", required=True)
    asyncio.run(verify(parser.parse_args()))
