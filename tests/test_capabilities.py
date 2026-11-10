import json
import os
import sys
from typing import Literal

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import Field

from personal_search import capabilities as discovery
from personal_search.models import SearchRequest
from personal_search.storage import Store
from personal_search.worker import execute


def test_catalogue_matches_request_schema_and_real_supported_operations():
    report = discovery.capabilities()
    assert set(report["platforms"]) == {"bilibili", "juejin", "segmentfault", "xiaohongshu", "zhihu", "tieba", "wechat"}
    assert report["unlisted_platforms_supported"] is False
    for name, platform in report["platforms"].items():
        request = SearchRequest(query="test", platforms=[name])
        assert request.platforms == [name]
        assert platform["search"] is True
        if name == "bilibili":
            assert all(platform[k] for k in ("video_details", "subtitles", "comments", "danmaku"))
            assert "body" not in platform["fetch_sections"]
        else:
            assert platform["article_body"] is True
            assert platform["fetch_sections"] == ["metadata", "body"]
            assert not any(
                platform[k] for k in ("video_details", "subtitles", "comments", "danmaku")
            )
    assert report["request_limits"]["max_requests"] == {"minimum": 5, "maximum": 300}
    assert report["request_limits"]["platforms"]["maxItems"] == 1
    for depth, defaults in report["search_modes"].items():
        request = SearchRequest(query="test", depth=depth)
        assert defaults == {
            "pages_per_keyword": request.pages_per_keyword,
            "max_items": request.max_items,
        }


def test_registry_only_roadmap_does_not_advertise_an_unaccepted_platform(monkeypatch):
    monkeypatch.setitem(
        discovery.PLATFORM_CAPABILITIES,
        "future_example",
        {
            "implementation": "available",
            "search": True,
        },
    )
    assert "future_example" not in discovery.capabilities()["platforms"]


def test_future_accepted_adapter_is_discovered_without_skill_changes(monkeypatch):
    class FutureRequest(SearchRequest):
        platforms: list[Literal["bilibili", "fixture_forum"]] = Field(
            default_factory=lambda: ["bilibili"],
            min_length=1,
            max_length=2,
        )

    monkeypatch.setattr(discovery, "SearchRequest", FutureRequest)
    monkeypatch.setitem(
        discovery.PLATFORM_CAPABILITIES,
        "fixture_forum",
        {
            "implementation": "available",
            "search": True,
            "comments": False,
            "verification": {"level": "simulation_only"},
        },
    )
    report = discovery.capabilities()
    assert report["platforms"]["fixture_forum"]["search"] is True
    assert report["platforms"]["fixture_forum"]["comments"] is False
    assert report["request_limits"]["platforms"]["maxItems"] == 2


def test_discovery_results_cannot_mutate_the_registry():
    first = discovery.capabilities()
    first["platforms"]["bilibili"]["search"] = False
    first["platforms"]["bilibili"]["coverage_notes"].append("mutation")
    second = discovery.capabilities()
    assert second["platforms"]["bilibili"]["search"] is True
    assert "mutation" not in second["platforms"]["bilibili"]["coverage_notes"]


async def test_real_mcp_discovery_is_read_only_and_preserves_legacy_tools(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "personal_search.server"],
        env={**os.environ, "GPTCH_DATA_DIR": str(tmp_path)},
    )
    async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()
        tools = {t.name: t for t in (await session.list_tools()).tools}
        tool = tools["get_capabilities"]
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.idempotentHint is True
        assert len(tools) == 11
        response = await session.call_tool("get_capabilities", {})
        assert not response.isError
        report = json.loads(response.content[0].text)
        assert report == discovery.capabilities()
        ping = json.loads((await session.call_tool("ping_local_server", {})).content[0].text)
        assert ping["platforms"] == list(report["platforms"])
        assert ping["queued_tasks"] == 0
        store = Store(tmp_path)
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0

        invalid = await session.call_tool(
            "deep_search",
            {
                "request": {
                    "query": "unsupported sources",
                    "platforms": ["xiaohongshu", "zhihu"],
                }
            },
        )
        assert invalid.isError
        with store.connect() as db:
            assert db.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0


async def test_simulated_candidate_followup_skips_research_and_reuses_archive_cache(env):
    """Protocol strategy regression with simulated platform data, not AI activation."""
    settings, store, platform, factory = env

    async def run(request):
        task = store.create_task(request.model_dump())
        adapter = factory(request, task["id"])
        await execute(settings, store, task["id"], adapter_factory=lambda *args: adapter)
        return store.task(task["id"])

    candidates = await run(SearchRequest(query="FL Studio", depth="quick", max_items=0))
    assert candidates["candidate_count"] == 1
    assert candidates["item_count"] == 0
    assert all(not r.url.path.endswith("/view") for r in platform.calls)
    search_count = sum(r.url.path.endswith("search/type") for r in platform.calls)
    selected = store.candidates(candidates["id"])[0]["id"]
    followup = await run(SearchRequest(query="process and feedback", content_ids=[selected]))
    assert followup["item_count"] == 1
    assert sum(r.url.path.endswith("search/type") for r in platform.calls) == search_count
    before = len(platform.calls)
    cached = await run(SearchRequest(query="another followup", content_ids=[selected]))
    assert cached["item_count"] == 1
    assert cached["progress"]["requests"] == 0
    assert len(platform.calls) == before
