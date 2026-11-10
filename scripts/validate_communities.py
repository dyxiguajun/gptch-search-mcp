"""Real native search -> worker -> SQLite -> fetch_content, isolated from production.

Two platforms, one search page and two selected articles each. No login, no web fallback.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from personal_search.models import SearchRequest
from personal_search.results import fetch_saved
from personal_search.settings import Settings
from personal_search.storage import Store, atomic_json
from personal_search.worker import execute


async def main():
    root = Path(__file__).resolve().parents[1]
    report = {"test_surface": "live_native_platform_worker_sqlite_readback_not_Work_UI", "runs": []}
    for platform in ("juejin", "segmentfault"):
        settings = Settings(root / "storage/platform-expansion-live" / platform, request_interval=2)
        settings.prepare()
        store = Store(settings.data_dir)
        for case in ("initial", "cached-repeat"):
            request = SearchRequest(
                query="asyncio",
                platforms=[platform],
                depth="quick",
                pages_per_keyword=1,
                max_items=2,
                max_requests=12,
                timeout_seconds=120,
                concurrency=1,
            )
            task = store.create_task(request.model_dump())
            started = time.perf_counter()
            await execute(settings, store, task["id"])
            state = store.task(task["id"])
            row = {
                "platform": platform,
                "case": case,
                "task_id": task["id"],
                "task_status": state["status"],
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "progress": state["progress"],
                "candidate_count": state["candidate_count"],
                "items": [],
                "error": state["error"],
            }
            for item in store.items(task["id"], limit=2):
                read = fetch_saved(store, task["id"], item["id"], "body", limit=200)
                row["items"].append(
                    {
                        "id": item["id"],
                        "platform": item["platform"],
                        "url": item["url"],
                        "title": item["title"],
                        "published_at": item.get("published_at"),
                        "status": item["status"],
                        "body_records": read["total_saved"],
                        "code_blocks": sum(r["type"] == "code" for r in item["body"]),
                        "body_chars": item["coverage"]["body"]["chars_saved"],
                        "cached": item["cache"]["cached"],
                        "source_relationship": item.get("source_relationship"),
                    }
                )
            report["runs"].append(row)
            atomic_json(root / "docs/platform-expansion/live-worker-results.json", report)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if state["status"] in ("failed", "partial") and state["error"]:
                return
            if case == "cached-repeat":
                assert row["progress"]["requests"] == 0
                assert len(row["items"]) == 2
    assert all(len(r["items"]) == 2 for r in report["runs"])


if __name__ == "__main__":
    asyncio.run(main())
