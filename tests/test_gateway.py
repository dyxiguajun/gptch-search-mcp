import asyncio
import json
import os
import sqlite3
import sys
import tomllib

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import ValidationError

from personal_search.auth import AuthStore
from personal_search.bilibili import parse_bvid
from personal_search.errors import GatewayError, safe_error
from personal_search.http import validate_url
from personal_search.manager import TaskManager
from personal_search.models import SearchRequest
from personal_search.results import fetch_saved
from personal_search.storage import Store, cache_key, scrub
from personal_search.worker import execute

BV = "BV1GJ411x7h7"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.bilibili.com/x",
        "https://127.0.0.1/x",
        "https://api.bilibili.com.evil.test/x",
        "https://evil.test/a.hdslb.com",
        "https://user:password@api.bilibili.com/x",
        "https://api.bilibili.com:123/x",
    ],
)
def test_rejects_arbitrary_network_targets(url):
    with pytest.raises(GatewayError):
        validate_url(url)


def test_content_id_scope():
    assert parse_bvid(BV) == BV
    assert parse_bvid(f"https://www.bilibili.com/video/{BV}?p=2") == BV
    with pytest.raises(GatewayError):
        parse_bvid(f"https://evil.test/{BV}")


def test_validates_budget_and_preserves_normalized_requests():
    req = SearchRequest(
        query=" first ", keywords=["two", "three", "four", "five", "six"], depth="deep"
    )
    assert SearchRequest.model_validate(req.model_dump()) == req
    assert req.pages_per_keyword == 5
    assert SearchRequest(query="extract", content_ids=[BV]).max_items == 1
    with pytest.raises(ValidationError):
        SearchRequest(query=" ")
    with pytest.raises(ValidationError):
        SearchRequest(query="test", platforms=["unimplemented-platform"])
    with pytest.raises(ValidationError):
        SearchRequest(query="test", concurrency=99)
    with pytest.raises(ValidationError):
        SearchRequest(query="test", keywords=[str(i) for i in range(6)])


def test_secrets_do_not_enter_archive(env):
    _, store, _, _ = env
    task = store.create_task(SearchRequest(query="test").model_dump())
    store.raw(
        task["id"],
        "player",
        {
            "subtitle_url": "secret",
            "subtitle_url_v2": "secret",
            "SESSDATA": "secret",
            "body": [{"Cookie": "secret", "text": "okay"}],
        },
    )
    payload = next((store.root / "raw" / task["id"]).glob("*.json")).read_text()
    assert "secret" not in payload
    assert "okay" in payload
    assert "secret" not in json.dumps(safe_error(RuntimeError("secret")))
    assert scrub({"Authorization": "secret"}) == {}


async def test_native_pagination_and_signed_requests(env):
    _, _, platform, factory = env
    adapter = factory()
    try:
        first, total = await adapter.search_page("FL Studio", 1)
        await adapter.search_page("FL Studio", 2)
        assert first[0]["title"] == "FL Studio 教程"
        assert total == 2
        search_calls = [r for r in platform.calls if r.url.path.endswith("search/type")]
        assert [r.url.params["page"] for r in search_calls] == ["1", "2"]
        assert all(len(r.url.params["w_rid"]) == 32 for r in search_calls)
        assert adapter.http.requests == 3  # 1 signing-key request and 2 searches
    finally:
        await adapter.close()


async def test_cache_survives_adapter_restart(env):
    _, _, platform, factory = env
    first = factory()
    await first.search_page("test", 1)
    await first.close()
    before = len(platform.calls)
    second = factory()
    rows, _ = await second.search_page("test", 1)
    assert rows[0]["cached"] is True
    assert len(platform.calls) == before
    assert second.http.requests == 0
    await second.close()


async def test_full_fixture_extract_compressed_danmaku_and_local_retrieval(env):
    _, store, _, factory = env
    adapter = factory()
    item = await adapter.extract(BV)
    store.save_item(adapter.task_id, item)
    await adapter.close()
    assert item["status"] == "complete"
    assert item["pages"][0]["danmaku"]["danmaku"][0]["playback_seconds"] == 1.5
    assert item["comments_hot"]["replies"][0]["root_id"] == "1"
    records = fetch_saved(store, adapter.task_id, BV, "subtitles", limit=1)
    assert records["records"][0]["text"] == "Fixture Rawstyle 教程"
    assert store.archive_search("Rawstyle")[0]["content_id"] == BV
    assert "example-secret" not in json.dumps(item)


async def test_video_details_use_wbi_endpoint_when_legacy_view_is_blocked(env):
    _, _, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(412, text="<html>risk control</html>")
        if r.url.path == "/x/web-interface/view"
        else None
    )
    adapter = factory()
    try:
        item = await adapter.extract(BV)
        detail_calls = [r for r in platform.calls if r.url.path.endswith("/view")]
        assert len(detail_calls) == 1
        request = detail_calls[0]
        assert request.url.path == "/x/web-interface/wbi/view"
        assert request.url.params["bvid"] == BV
        assert len(request.url.params["w_rid"]) == 32
        assert "wts" in request.url.params
        assert item["pages"][0]["cid"] == 100
        assert item["metadata"]["api_path"] == request.url.path
        assert item["metadata"]["wbi_signed"] is True
    finally:
        await adapter.close()


async def test_blocked_wbi_details_stop_without_legacy_fallback_or_retries(env):
    _, _, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(412, text="<html>risk control</html>")
        if r.url.path == "/x/web-interface/wbi/view"
        else None
    )
    adapter = factory()
    try:
        with pytest.raises(GatewayError) as error:
            await adapter.extract(BV)
        assert error.value.code == "RISK_CONTROL"
        assert adapter.http.blocked is error.value
        assert [r.url.path for r in platform.calls] == [
            "/x/web-interface/nav",
            "/x/web-interface/wbi/view",
        ]
    finally:
        await adapter.close()


async def test_comments_page_budget_does_not_claim_complete(env):
    _, _, platform, factory = env
    platform.comments_end = False
    adapter = factory()
    result = await adapter.comments(99, 2)
    assert result["status"] == "partial"
    assert result["reason"] == "comment_page_budget"
    assert result["all_platform_comments"] is False
    await adapter.close()


async def test_comments_failure_keeps_previous_pages(env):
    _, _, platform, factory = env
    platform.comments_end = False
    platform.fail = lambda r: (
        httpx.Response(503)
        if r.url.path.endswith("reply/wbi/main") and r.url.params.get("next") == "1"
        else None
    )
    adapter = factory(SearchRequest(query="test", comment_pages=2))
    result = await adapter.comments(99, 2)
    assert result["status"] == "partial"
    assert len(result["comments"]) == 1
    assert result["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    await adapter.close()


async def test_login_expiry_stops_search(env):
    settings, _, platform, factory = env
    AuthStore(settings.data_dir).save({"SESSDATA": "fixture-only-secret"})
    adapter = factory()
    with pytest.raises(GatewayError, match="登录") as error:
        await adapter.preflight()
    assert error.value.code == "AUTH_EXPIRED"
    assert len(platform.calls) == 1
    await adapter.close()


async def test_missing_subtitles_are_explicit(env):
    _, _, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"bvid": BV, "aid": 99, "cid": 100, "subtitle": {"subtitles": []}},
            },
        )
        if "/player/" in r.url.path
        else None
    )
    adapter = factory()
    adapter.authenticated = True
    result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
    assert result["code"] == "SUBTITLE_NOT_AVAILABLE"
    await adapter.close()


async def test_cc_wbi_player_is_used_with_identity_and_body_checks(env):
    _, _, platform, factory = env
    adapter = factory()
    try:
        result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
        assert result["status"] == "complete"
        assert result["api_path"] == "/x/player/wbi/v2"
        assert result["scope"] == "cc_ext_validated_tracks"
        assert result["tracks"][0]["verification"]["player_identity"]
        player_calls = [r.url.path for r in platform.calls if "/player/" in r.url.path]
        assert player_calls == ["/x/player/wbi/v2"]
        cdn_calls = [r for r in platform.calls if r.url.path == "/fixture.json"]
        assert len(cdn_calls) == 2
        assert cdn_calls[0].url.params["_ts"] != cdn_calls[1].url.params["_ts"]
        assert "example-secret" not in json.dumps(result)
    finally:
        await adapter.close()


async def test_blocked_wbi_player_does_not_try_standard_or_retry(env):
    _, _, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(412, text="<html>risk control</html>")
        if r.url.path == "/x/player/wbi/v2"
        else None
    )
    adapter = factory()
    try:
        with pytest.raises(GatewayError) as error:
            await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
        assert error.value.code == "RISK_CONTROL"
        assert [r.url.path for r in platform.calls] == [
            "/x/web-interface/nav",
            "/x/player/wbi/v2",
        ]
    finally:
        await adapter.close()


async def test_mismatched_subtitle_is_not_returned_archived_or_cached(env):
    _, store, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(200, json={"body": [{"from": 0, "to": 746, "content": "wrong video"}]})
        if r.url.path == "/fixture.json"
        else None
    )
    adapter = factory()
    try:
        result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 282})
        assert result["status"] == "failed"
        assert result["error"]["code"] == "SUBTITLE_CONTENT_MISMATCH"
        assert result["tracks"] == []
        assert not list(store.root.glob("raw/*/*subtitle_100*.json"))
        with store.connect() as db:
            assert not db.execute(
                "SELECT 1 FROM cache WHERE payload LIKE '%wrong video%'"
            ).fetchall()
    finally:
        await adapter.close()


async def test_unvalidated_legacy_subtitle_cache_is_not_reused(env):
    _, store, _, factory = env
    adapter = factory()
    legacy_key = cache_key(
        "subtitles", {"bvid": BV, "cid": 100, "tracks": 2, "scope": adapter.cache_scope}
    )
    store.cache_put(legacy_key, {"status": "complete", "tracks": [{"text": "unvalidated"}]}, 86400)
    try:
        result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
        assert result["status"] == "complete"
        assert "unvalidated" not in json.dumps(result)
        assert adapter.http.requests == 4
    finally:
        await adapter.close()


async def test_throttle_opens_circuit_and_reduces_concurrency(env):
    _, _, platform, factory = env
    platform.fail = lambda r: httpx.Response(429)
    adapter = factory(SearchRequest(query="test", concurrency=3))
    for _ in range(2):
        with pytest.raises(GatewayError) as exc:
            await adapter.http.get("https://api.bilibili.com/x/test")
        assert exc.value.code == "RATE_LIMITED"
    assert adapter.http.requests == 1
    assert adapter.http.gate.limit == 1
    await adapter.close()


async def test_request_budget_counts_all_requests(env):
    _, _, _, factory = env
    adapter = factory(SearchRequest(query="test", max_requests=5))
    for _ in range(5):
        await adapter.http.get("https://api.bilibili.com/x/web-interface/nav")
    with pytest.raises(GatewayError) as exc:
        await adapter.http.get("https://api.bilibili.com/x/web-interface/nav")
    assert exc.value.code == "REQUEST_BUDGET_EXCEEDED"
    assert adapter.http.requests == 5
    await adapter.close()


async def test_worker_deduplicates_and_persists(env):
    settings, store, _, factory = env
    request = SearchRequest(query="FL Studio", keywords=["Rawstyle"], max_items=1)
    task = store.create_task(request.model_dump())
    await execute(
        settings, store, task["id"], adapter_factory=lambda s, db, tid, req: factory(req, tid)
    )
    result = store.task(task["id"])
    assert result["status"] == "completed"
    assert result["candidate_count"] == result["item_count"] == 1
    assert set(store.candidates(task["id"])[0]["matched_keywords"]) == {"FL Studio", "Rawstyle"}
    restarted = Store(settings.data_dir)
    assert restarted.items(task["id"])[0]["title"] == "Fixture FL Studio 教程"
    backup = restarted.backup()
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


async def test_empty_search_is_real_empty_and_success(env):
    settings, store, platform, factory = env
    platform.search_empty = True
    task = store.create_task(SearchRequest(query="nothing", depth="quick").model_dump())
    await execute(
        settings, store, task["id"], adapter_factory=lambda s, db, tid, req: factory(req, tid)
    )
    result = store.task(task["id"])
    assert result["status"] == "completed" and result["candidate_count"] == 0


async def test_one_owner_and_interruption_recovery(env):
    settings, store, _, _ = env
    task = store.create_task(SearchRequest(query="test").model_dump())
    store.update(task["id"], status="running")
    async with TaskManager(settings):
        assert store.task(task["id"])["status"] == "interrupted"
        with pytest.raises(GatewayError) as exc:
            async with TaskManager(settings):
                pass
        assert exc.value.code == "SERVICE_IN_USE"


async def test_worker_crash_becomes_failed_and_cancel_reaps_process(env, monkeypatch):
    settings, _, _, _ = env
    create = asyncio.create_subprocess_exec

    async def short_worker(*args, **kwargs):
        return await create(sys.executable, "-c", "raise SystemExit(7)", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", short_worker)
    async with TaskManager(settings) as manager:
        task = manager.submit(SearchRequest(query="test"))
        for _ in range(100):
            if manager.store.task(task["id"])["status"] == "failed":
                break
            await asyncio.sleep(0.02)
        assert manager.store.task(task["id"])["error"]["code"] == "WORKER_EXITED"

    async def slow_worker(*args, **kwargs):
        return await create(sys.executable, "-c", "import time; time.sleep(60)", **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", slow_worker)
    async with TaskManager(settings) as manager:
        task = manager.submit(SearchRequest(query="test"))
        for _ in range(100):
            if manager.process:
                break
            await asyncio.sleep(0.02)
        process = manager.process
        await manager.cancel(task["id"])
        assert process.returncode is not None
        assert manager.store.task(task["id"])["status"] == "cancelled"


async def test_mcp_real_stdio_ping_and_schemas(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "personal_search.server"],
        env={**os.environ, "GPTCH_DATA_DIR": str(tmp_path)},
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        tools = (await session.list_tools()).tools
        names = {tool.name for tool in tools}
        assert names == {
            "inspect_video",
            "read_video_evidence",
            "get_capabilities",
            "ping_local_server",
            "deep_search",
            "get_task",
            "fetch_content",
            "search_archive",
            "manage_task",
            "auth_status",
            "open_login_panel",
        }
        response = await session.call_tool("ping_local_server", {})
        assert not response.isError
        payload = json.loads(response.content[0].text)
        assert payload["execution"] == "local" and payload["status"] == "ok"
        invalid = await session.call_tool(
            "deep_search", {"request": {"query": "test", "platforms": ["bank"]}}
        )
        assert invalid.isError
        saved = await session.call_tool("get_task", {"task_id": "does-not-exist"})
        assert json.loads(saved.content[0].text)["error"]["code"] == "TASK_NOT_FOUND"
        submitted = await session.call_tool(
            "deep_search", {"request": {"query": "fixture task cancellation", "depth": "quick"}}
        )
        task_id = json.loads(submitted.content[0].text)["task_id"]
        cancelled = await session.call_tool("manage_task", {"task_id": task_id, "action": "cancel"})
        assert json.loads(cancelled.content[0].text)["status"] == "cancelled"


async def test_anonymous_cannot_assert_absent_subtitles(env):
    _, _, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"bvid": BV, "aid": 99, "cid": 100, "subtitle": {"subtitles": []}},
            },
        )
        if "/player/" in r.url.path
        else None
    )
    adapter = factory()
    result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
    assert result["code"] == "AUTH_REQUIRED"
    await adapter.close()


@pytest.mark.parametrize(
    "api_code,expected",
    [
        (-101, "AUTH_REQUIRED"),
        (-352, "RISK_CONTROL"),
        (-403, "PERMISSION_DENIED"),
        (-404, "CONTENT_NOT_AVAILABLE"),
    ],
)
async def test_structured_api_failures(env, api_code, expected):
    _, _, platform, factory = env
    platform.fail = lambda r: httpx.Response(200, json={"code": api_code, "message": "fixture"})
    adapter = factory()
    with pytest.raises(GatewayError) as exc:
        await adapter.http.json("/x/test")
    assert exc.value.code == expected
    await adapter.close()


async def test_worker_parent_monitor_exits_without_network(env):
    settings, store, _, _ = env
    task = store.create_task(SearchRequest(query="test").model_dump())
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "personal_search.worker",
        task["id"],
        "--data-dir",
        str(settings.data_dir),
        "--parent-pid",
        "99999999",
    )
    await asyncio.wait_for(proc.wait(), timeout=5)
    assert proc.returncode == 0
    assert store.task(task["id"])["status"] == "interrupted"


async def test_network_timeout_is_structured(env):
    _, _, platform, factory = env

    def timeout(request):
        raise httpx.ReadTimeout("fixture private URL", request=request)

    platform.fail = timeout
    adapter = factory()
    with pytest.raises(GatewayError) as exc:
        await adapter.http.json("/x/test")
    assert exc.value.code == "NETWORK_TIMEOUT"
    assert adapter.http.requests == 3
    assert "private" not in json.dumps(exc.value.as_dict())
    await adapter.close()


async def test_task_timeout_preserves_committed_candidates(env, monkeypatch):
    settings, store, _, factory = env
    request = SearchRequest(query="test", pages_per_keyword=2, max_items=0)
    task = store.create_task(request.model_dump())
    adapter = factory(request, task["id"])
    search = adapter.search_page

    async def slow_second_page(keyword, page):
        if page == 2:
            await asyncio.sleep(1)
        return await search(keyword, page)

    adapter.search_page = slow_second_page
    actual_timeout = asyncio.timeout
    monkeypatch.setattr(asyncio, "timeout", lambda seconds: actual_timeout(0.03))
    await execute(settings, store, task["id"], adapter_factory=lambda *args: adapter)
    result = store.task(task["id"])
    assert result["status"] == "partial"
    assert result["error"]["code"] == "TIME_BUDGET_EXCEEDED"
    assert result["candidate_count"] == 1
    assert adapter.http.client.is_closed


async def test_fetch_response_budget_and_pagination(env):
    _, store, _, factory = env
    adapter = factory()
    item = await adapter.extract(BV)
    item["comments_hot"]["comments"] = [{"id": str(n), "text": "x" * 10000} for n in range(20)]
    store.save_item(adapter.task_id, item)
    result = fetch_saved(store, adapter.task_id, BV, "comments_hot", limit=20)
    assert len(result["records"]) < 20 and result["has_more"]
    assert result["next_offset"] == len(result["records"])
    assert len(json.dumps(result)) < 45000
    await adapter.close()


async def test_stop_command_targets_actual_gateway(tmp_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "personal_search.server",
        env={**os.environ, "GPTCH_DATA_DIR": str(tmp_path)},
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        for _ in range(100):
            if (tmp_path / "gateway.json").exists():
                break
            await asyncio.sleep(0.02)
        stop = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "personal_search.cli",
            "--data-dir",
            str(tmp_path),
            "stop",
            stdout=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(stop.communicate(), timeout=8)
        assert stop.returncode == 0
        assert json.loads(stdout)["status"] == "stopped"
        await asyncio.wait_for(process.wait(), timeout=5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_config_generation_for_directory_with_spaces(tmp_path):
    destination = tmp_path / "config folder" / "mcp.json"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "personal_search.cli",
        "--data-dir",
        str(tmp_path / "data folder"),
        "config",
        "--output",
        str(destination),
        stdout=asyncio.subprocess.PIPE,
    )
    stdout, _ = await process.communicate()
    assert process.returncode == 0 and json.loads(stdout)["status"] == "ok"
    config = json.loads(destination.read_text())
    assert config["mcpServers"]["gptch-search"]["command"] == sys.executable
    toml = tomllib.loads(destination.with_suffix(".toml").read_text())
    assert toml["mcp_servers"]["gptch-search"]["env"]["GPTCH_DATA_DIR"].endswith("data folder")
