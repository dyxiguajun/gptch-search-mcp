"""Opt-in, bounded live CC checks; never modifies the production task queue.

Credentials are read from the existing data directory. The validation archive is
separate and can be opened by the unchanged MCP server for local readback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from personal_search.bilibili import BilibiliAdapter, parse_bvid
from personal_search.cc_subtitles import explicit_part
from personal_search.errors import safe_error
from personal_search.models import SearchRequest
from personal_search.results import fetch_saved
from personal_search.settings import Settings
from personal_search.storage import Store, atomic_json


async def validate(args):
    request = SearchRequest(
        query="CC integration live validation",
        content_ids=args.content,
        max_requests=args.max_requests,
        subtitle_tracks=2,
        refresh=True,
    )
    settings = Settings(Path(args.auth_dir).resolve(), request_interval=5)
    archive = Path(args.archive_dir).resolve()
    archive.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = Store(archive)
    task = store.create_task(request.model_dump())
    store.update(task["id"], status="running")
    adapter = BilibiliAdapter(settings, store, task["id"], request)
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "task_id": task["id"],
        "archive_dir": str(archive),
        "max_requests": args.max_requests,
        "interval_seconds": 5,
        "requests": [],
        "samples": [],
        "production_queue_modified": False,
    }

    async def response_hook(response):
        query = response.request.url.params
        report["requests"].append(
            {
                "at_utc": datetime.now(timezone.utc).isoformat(),
                "host": response.request.url.host,
                "path": response.request.url.path,
                "http_status": response.status_code,
                "content_type": response.headers.get("content-type"),
                "query_keys": sorted(set(query.keys())),
                "identity_params": {k: query[k] for k in ("bvid", "aid", "cid") if k in query},
                "session_cookie_sent": "SESSDATA=" in response.request.headers.get("cookie", ""),
            }
        )

    adapter.http.client.event_hooks["response"].append(response_hook)
    try:
        await adapter.preflight()
        for value in request.content_ids:
            bvid = parse_bvid(value)
            sample = {"bvid": bvid}
            try:
                data, provenance = await adapter.video_details(bvid)
                part = explicit_part(value) or 1
                page = next((p for p in data["pages"] if p.get("page", 1) == part), None)
                if page is None:
                    from personal_search.errors import GatewayError

                    raise GatewayError("INVALID_VIDEO_PAGE", "请求的分P不存在")
                result = await adapter.subtitles(bvid, data["aid"], page)
                sample.update(
                    {
                        "title": data.get("title"),
                        "aid": data["aid"],
                        "cid": page["cid"],
                        "page": page.get("page", 1),
                        "duration": page.get("duration"),
                        "subtitles": result,
                    }
                )
                sample["accepted"] = bool(result.get("tracks")) and not result.get("error")
                item = {
                    "id": bvid,
                    "platform": "bilibili",
                    "url": f"https://www.bilibili.com/video/{bvid}",
                    "title": data.get("title", ""),
                    "description": data.get("desc", ""),
                    "author": data.get("owner", {}).get("name", ""),
                    "published_at": data.get("pubdate", 0),
                    "metadata": provenance,
                    "status": "complete"
                    if result["status"] in ("complete", "unavailable")
                    and result.get("code") != "AUTH_REQUIRED"
                    else "partial",
                    "pages": [
                        {
                            "cid": page["cid"],
                            "page": page.get("page", 1),
                            "title": page.get("part", ""),
                            "subtitles": result,
                            "danmaku": {"status": "not_requested"},
                        }
                    ],
                    "comments_hot": {"status": "not_requested"},
                    "comments_latest": {"status": "not_requested"},
                    "coverage": {
                        "all_platform_content": False,
                        "scope": "cc_integration_validation_only",
                    },
                    "extracted_at": time.time(),
                }
                store.save_item(task["id"], item)
                saved = fetch_saved(Store(archive), task["id"], bvid, "subtitles", limit=1)
                sample["sqlite_readback"] = {
                    "total_saved": saved["total_saved"],
                    "records": saved["records"],
                    "has_more": saved["has_more"],
                }
            except Exception as exc:
                sample["accepted"] = False
                sample["error"] = safe_error(exc)
            report["samples"].append(sample)
            atomic_json(Path(args.output), report)
            print(
                json.dumps(
                    {k: sample.get(k) for k in ("bvid", "title", "accepted", "error")},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if adapter.http.blocked:
                break
    except Exception as exc:
        report["error"] = safe_error(exc)
    finally:
        report["actual_requests"] = adapter.http.requests
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["authenticated_session_present"] = adapter.authenticated
        status = (
            "completed"
            if report["samples"]
            and not report.get("error")
            and all(
                s.get("subtitles", {}).get("status") in ("complete", "unavailable")
                and s.get("subtitles", {}).get("code") != "AUTH_REQUIRED"
                for s in report["samples"]
            )
            else "partial"
        )
        store.update(
            task["id"],
            status=status,
            progress={"phase": "cc_validation", "requests": adapter.http.requests},
        )
        await adapter.close()
        atomic_json(Path(args.output), report)
        print(
            json.dumps(
                {
                    "output": args.output,
                    "actual_requests": report["actual_requests"],
                    "task_id": task["id"],
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("content", nargs="+")
    parser.add_argument("--auth-dir", required=True)
    parser.add_argument("--archive-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-requests", type=int, default=24)
    asyncio.run(validate(parser.parse_args()))
