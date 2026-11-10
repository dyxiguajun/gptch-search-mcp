import json

import httpx
import pytest

from personal_search.errors import GatewayError
from personal_search.models import SearchRequest
from personal_search.results import fetch_saved
from personal_search.storage import Store

BV = "BV1GJ411x7h7"


def test_explicit_part_is_preserved_without_tracking_and_bad_parts_are_rejected():
    request = SearchRequest(
        query="parts",
        content_ids=[
            f"https://www.bilibili.com/video/{BV}/?p=2&vd_source=private",
            f"{BV}?p=1",
        ],
    )
    assert request.content_ids == [f"{BV}?p=2", f"{BV}?p=1"]
    assert request.max_items == 1
    for suffix in ("?p=0", "?p=-1", "?p=", "?p=2&p=3", "?p=two"):
        with pytest.raises(GatewayError):
            SearchRequest(query="bad part", content_ids=[BV + suffix])
    assert SearchRequest(query="encoded", content_ids=[BV + "?p=%32"]).content_ids == [BV + "?p=2"]


async def test_player_identity_mismatch_stops_before_cdn(env):
    _, store, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(200, json={"code": 0, "data": {"bvid": BV, "aid": 99, "cid": 999}})
        if "/player/" in r.url.path
        else None
    )
    adapter = factory()
    try:
        with pytest.raises(GatewayError) as error:
            await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
        assert error.value.code == "SUBTITLE_CONTENT_MISMATCH"
        assert len(platform.calls) == 2
        assert not list(store.root.glob("raw/*/*subtitle*.json"))
    finally:
        await adapter.close()


async def test_too_short_ai_subtitle_is_rejected_even_if_downloads_match(env):
    _, _, platform, factory = env

    def reply(r):
        if "/player/" in r.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "bvid": BV,
                        "aid": 99,
                        "cid": 100,
                        "subtitle": {
                            "subtitles": [
                                {
                                    "lan": "ai-zh",
                                    "subtitle_url": "https://aisubtitle.hdslb.com/fixture.json",
                                }
                            ]
                        },
                    },
                },
            )

    platform.fail = reply
    adapter = factory()
    try:
        result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 198})
        assert result["status"] == "failed"
        assert result["error"]["code"] == "SUBTITLE_CONTENT_MISMATCH"
        assert result["tracks"] == []
    finally:
        await adapter.close()


async def test_full_body_changes_after_original_fingerprint_are_rejected(env):
    _, store, platform, factory = env
    downloads = 0

    def reply(r):
        nonlocal downloads
        if r.url.path == "/fixture.json":
            downloads += 1
            return httpx.Response(
                200,
                json={
                    "body": [
                        {
                            "from": 0,
                            "to": 2,
                            "content": "字幕" * 1600 + ("甲" if downloads % 2 else "乙"),
                        },
                    ]
                },
            )

    platform.fail = reply
    adapter = factory()
    try:
        result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
        assert result["status"] == "failed"
        assert result["error"]["code"] == "SUBTITLE_UNSTABLE"
        assert not list(store.root.glob("raw/*/*subtitle*.json"))
    finally:
        await adapter.close()


async def test_wrong_track_language_is_rejected(env):
    _, _, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(200, json={"body": [{"from": 0, "to": 2, "content": "all English"}]})
        if r.url.path == "/fixture.json"
        else None
    )
    adapter = factory()
    try:
        result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
        assert result["error"]["code"] == "SUBTITLE_LANGUAGE_MISMATCH"
        assert result["tracks"] == []
    finally:
        await adapter.close()


async def test_part_two_and_sqlite_readback_keep_the_cid(env):
    settings, store, platform, factory = env

    def reply(r):
        if r.url.path.endswith("/view"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "bvid": BV,
                        "aid": 99,
                        "title": "two parts",
                        "pages": [
                            {"page": 1, "cid": 100, "duration": 5},
                            {"page": 2, "cid": 101, "duration": 5},
                        ],
                    },
                },
            )

    platform.fail = reply
    adapter = factory(SearchRequest(query="part two", content_ids=[BV + "?p=2"]))
    try:
        item = await adapter.extract(BV)
        assert [p["page"] for p in item["pages"]] == [2]
        assert item["status"] == "complete"
        store.save_item(adapter.task_id, item)
        readback = fetch_saved(Store(settings.data_dir), adapter.task_id, BV, "subtitles", limit=1)
        assert readback["records"][0]["cid"] == 101
        assert readback["records"][0]["page"] == 2
        assert readback["records"][0]["bvid"] == BV
        assert item["pages"][0]["subtitles"]["binding"]["cid"] == 101
        assert all(r.url.params["cid"] == "101" for r in platform.calls if "/player/" in r.url.path)
        assert "private" not in json.dumps(item)
    finally:
        await adapter.close()


async def test_nonexistent_part_is_not_silently_replaced(env):
    _, _, platform, factory = env
    adapter = factory(SearchRequest(query="part two", content_ids=[BV + "?p=2"]))
    try:
        with pytest.raises(GatewayError) as error:
            await adapter.extract(BV)
        assert error.value.code == "INVALID_VIDEO_PAGE"
        assert not any("/player/" in r.url.path for r in platform.calls)
    finally:
        await adapter.close()


async def test_validation_cache_is_bound_to_the_exact_part(env):
    _, _, platform, factory = env
    adapter = factory()
    try:
        first = await adapter.subtitles(BV, 99, {"page": 1, "cid": 100, "duration": 5})
        before = len(platform.calls)
        again = await adapter.subtitles(BV, 99, {"page": 1, "cid": 100, "duration": 5})
        assert again["cached"] is True
        assert len(platform.calls) == before
        second = await adapter.subtitles(BV, 99, {"page": 2, "cid": 101, "duration": 5})
        assert second["cached"] is False
        assert first["binding"]["cid"] != second["binding"]["cid"]
        assert len(platform.calls) == before + 3
    finally:
        await adapter.close()


async def test_empty_wbi_can_use_a_validated_standard_listing(env):
    _, _, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "bvid": BV,
                    "aid": 99,
                    "cid": 100,
                    "subtitle": {"subtitles": []},
                },
            },
        )
        if r.url.path == "/x/player/wbi/v2"
        else None
    )
    adapter = factory()
    try:
        result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
        assert result["status"] == "complete"
        assert result["api_path"] == "/x/player/v2"
        assert [r.url.path for r in platform.calls if "/player/" in r.url.path] == [
            "/x/player/wbi/v2",
            "/x/player/v2",
        ]
        assert result["tracks"][0]["verification"]["double_fetch_equal"]
    finally:
        await adapter.close()


async def test_budget_failure_keeps_the_first_validated_track(env):
    _, _, platform, factory = env

    def reply(r):
        if "/player/" in r.url.path:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "bvid": BV,
                        "aid": 99,
                        "cid": 100,
                        "subtitle": {
                            "subtitles": [
                                {
                                    "id": str(i),
                                    "lan": "zh-CN",
                                    "subtitle_url": "https://aisubtitle.hdslb.com/fixture.json",
                                }
                                for i in (1, 2)
                            ]
                        },
                    },
                },
            )

    platform.fail = reply
    adapter = factory(SearchRequest(query="limited budget", max_requests=5))
    try:
        result = await adapter.subtitles(BV, 99, {"cid": 100, "duration": 5})
        assert result["status"] == "partial"
        assert result["error"]["code"] == "REQUEST_BUDGET_EXCEEDED"
        assert len(result["tracks"]) == 1
        assert result["tracks"][0]["id"] == "1"
        assert adapter.http.requests == 5
        assert not any(r.url.path == "/x/player/v2" for r in platform.calls)
    finally:
        await adapter.close()
