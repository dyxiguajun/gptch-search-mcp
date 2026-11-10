from __future__ import annotations

import argparse
import asyncio
import re
import time

import psutil

from .errors import GatewayError, safe_error
from .models import SearchRequest
from .platforms import create_adapter, direct_candidate
from .settings import Settings
from .storage import Store


def relevance(item, keywords):
    title = item["title"].casefold()
    description = item.get("description", "").casefold()
    tokens = set(re.findall(r"\w+", " ".join(keywords).casefold()))
    return sum(3 * (token in title) + (token in description) for token in tokens)


async def execute(settings, store, task_id, *, adapter_factory=None):
    task = store.task(task_id)
    request = SearchRequest.model_validate(task["request"])
    try:
        adapter = (adapter_factory or create_adapter)(settings, store, task_id, request)
    except Exception as exc:
        store.update(task_id, status="failed", error=safe_error(exc))
        return
    progress = {
        "stage": "search",
        "requests": 0,
        "cache_hits": 0,
        "search_pages": 0,
        "candidate_scan_complete": False,
    }
    store.update(task_id, status="running", progress=progress, clear_error=True)
    failures = []
    search_truncated = False

    def checkpoint():
        progress.update(
            requests=adapter.http.requests,
            cache_hits=adapter.cache_hits,
            concurrency=adapter.http.gate.limit,
        )
        if hasattr(adapter.http, "static_cache_hits"):
            progress["browser_static_cache_hits"] = adapter.http.static_cache_hits
        store.update(task_id, progress=progress)

    try:
        async with asyncio.timeout(request.timeout_seconds):
            await adapter.preflight()
            # Each result is committed immediately; caches make rerunning an interrupted scan cheap.
            candidates = {r["id"]: r for r in store.candidates(task_id, limit=300)}
            if request.content_ids:
                for value in request.content_ids:
                    candidate = direct_candidate(request.platforms[0], value)
                    candidates[candidate["id"]] = candidate
                    store.save_candidate(task_id, candidate)
            for keyword in [] if request.content_ids else request.keywords:
                for page in range(1, request.pages_per_keyword + 1):
                    results, total_pages = await adapter.search_page(keyword, page)
                    progress["search_pages"] += 1
                    for item in results:
                        previous = candidates.get(item["id"])
                        if previous:
                            previous["matched_keywords"] = list(
                                dict.fromkeys([*previous["matched_keywords"], keyword])
                            )
                            store.save_candidate(task_id, previous)
                        elif len(candidates) < request.max_candidates:
                            candidates[item["id"]] = item
                            store.save_candidate(task_id, item)
                        else:
                            search_truncated = True
                    checkpoint()
                    if (
                        not results and not getattr(adapter, "last_page_empty_is_filtered", False)
                    ) or (total_pages and page >= total_pages):
                        break
                    if page == request.pages_per_keyword:
                        search_truncated = True
                    if len(candidates) >= request.max_candidates:
                        search_truncated = True
                        break
                if len(candidates) >= request.max_candidates:
                    break
            progress["candidate_scan_complete"] = True
            progress["search_truncated"] = search_truncated
            progress["stage"] = "extract"
            ranked = sorted(
                candidates.values(), key=lambda r: relevance(r, request.keywords), reverse=True
            )
            semaphore = asyncio.Semaphore(request.concurrency)

            async def one(candidate):
                async with semaphore:
                    if adapter.http.blocked:
                        return
                    try:
                        item = await adapter.extract(candidate["id"])
                        item["relevance_score"] = relevance(candidate, request.keywords)
                        store.save_item(task_id, item)
                    except Exception as exc:
                        failures.append({"content_id": candidate["id"], **safe_error(exc)})
                    checkpoint()

            await asyncio.gather(*(one(candidate) for candidate in ranked[: request.max_items]))
            items = store.items(task_id, limit=20)
            if adapter.http.blocked:
                raise adapter.http.blocked
            partial = bool(failures) or any(i.get("status") == "partial" for i in items)
            progress.update(
                stage="done",
                extraction_failures=failures,
                selected_items=min(len(ranked), request.max_items),
                finished_at=time.time(),
            )
            checkpoint()
            store.update(task_id, status="partial" if partial else "completed")
    except TimeoutError:
        error = GatewayError("TIME_BUDGET_EXCEEDED", "已达到任务时间预算；已采集结果已保存")
        checkpoint()
        current = store.task(task_id)
        store.update(
            task_id,
            status="partial" if current["candidate_count"] or current["item_count"] else "failed",
            error=error.as_dict(),
        )
    except Exception as exc:
        checkpoint()
        current = store.task(task_id)
        store.update(
            task_id,
            status="partial" if current["candidate_count"] or current["item_count"] else "failed",
            error=safe_error(exc),
        )
    finally:
        await adapter.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task_id")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    settings = Settings.load(args.data_dir)

    async def supervised():
        store = Store(settings.data_dir)
        job = asyncio.create_task(execute(settings, store, args.task_id))
        try:
            parent = psutil.Process(args.parent_pid) if args.parent_pid else None
            while not job.done():
                if parent and (not parent.is_running() or parent.status() == psutil.STATUS_ZOMBIE):
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
                    store.update(
                        args.task_id,
                        status="interrupted",
                        error={
                            "code": "PARENT_EXITED",
                            "message": "网关已退出；工作进程已释放，已采集资料保留。",
                        },
                    )
                    return
                await asyncio.wait({job}, timeout=1)
            await job
        except psutil.NoSuchProcess:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            store.update(
                args.task_id,
                status="interrupted",
                error={"code": "PARENT_EXITED", "message": "网关已退出；已保存资料保留。"},
            )

    asyncio.run(supervised())


if __name__ == "__main__":
    main()
