"""Simulated platform regressions: identity, actual ranking, paging, privacy and slice caches."""

import json

import httpx
import pytest
from pydantic import ValidationError

from personal_search.models import SearchRequest
from personal_search.results import fetch_saved
from personal_search.storage import cache_key
from personal_search.worker import execute

BV = "BV1GJ411x7h7"


def record(n, **kwargs):
    return {
        "rpid": n,
        "oid": 99,
        "root": 0,
        "content": {"message": f"comment {n}", "members": ["private"]},
        "like": 200 - n,
        "ctime": 1000 + n,
        "rcount": 9,
        "member": {"uname": "private", "mid": 123, "avatar": "private"},
        "mid": 123,
        "replies": [{"mid": 99, "content": {"message": "private"}}],
        **kwargs,
    }


def pages(platform, count=120, opaque=True):
    calls = []

    def response(r):
        if not r.url.path.endswith("reply/wbi/main"):
            return None
        q = r.url.params
        offset = json.loads(q["pagination_str"])["offset"] if "pagination_str" in q else "0"
        pn = int(offset if opaque else q.get("next", 0))
        calls.append((pn, int(q["mode"])))
        rows = [record(n) for n in range(pn * 20 + 1, min(count + 1, (pn + 1) * 20 + 1))]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "cursor": {
                        "is_end": (pn + 1) * 20 >= count,
                        "mode": int(q["mode"]),
                        "next": pn + 1,
                        "pagination_reply": {"next_offset": str(pn + 1)} if opaque else {},
                        "all_count": count * 2,
                    },
                    "top_replies": [record(999)],
                    "replies": rows,
                },
            },
        )

    platform.fail = response
    return calls


def test_budget_schema_and_default_compatibility():
    old = SearchRequest(query="test")
    assert old.comment_pages == 1 and old.comment_sorts == ["hot", "latest"]
    assert SearchRequest(query="test", max_comments=100).comment_pages == 7
    assert SearchRequest(query="test", max_comments=100, comment_pages=2).comment_pages == 2
    assert SearchRequest.model_validate(old.model_dump()) == old
    with pytest.raises(ValidationError):
        SearchRequest(query="test", max_comments=101)


async def test_100_platform_ranked_comments_and_profile_free_archive(env):
    _, store, platform, factory = env
    calls = pages(platform)
    req = SearchRequest(query="hot", comment_sorts=["hot"], max_comments=100, reply_pages=0)
    a = factory(req)
    try:
        r = await a.comments(99, 3)
        assert calls == [(n, 3) for n in range(5)]
        assert r["status"] == "complete" and r["reason"] == "comment_limit"
        assert r["coverage"]["visible_end"] is False
        assert len(r["comments"]) == 100
        assert [v["id"] for v in r["comments"]] == [str(n) for n in range(1, 101)]
        assert [v["rank"] for v in r["comments"]] == list(range(1, 101))
        assert r["coverage"]["pinned_comments_omitted"] == 1
        assert r["replies_status"] == "not_requested"
        assert all(
            set(v)
            == {
                "id",
                "root_id",
                "parent_id",
                "text",
                "likes",
                "published_at",
                "reply_count_reported",
                "rank",
                "sort",
            }
            for v in r["comments"]
        )
        raw = "".join(p.read_text() for p in (store.root / "raw" / a.task_id).glob("*.json"))
        with store.connect() as db:
            cached = "".join(row[0] for row in db.execute("select payload from cache"))
        assert '"member"' not in raw + cached and '"mid"' not in raw + cached
        assert "private" not in raw + cached
    finally:
        await a.close()


async def test_smaller_comment_budget_reuses_page_and_stops_without_reply_requests(env):
    _, _, platform, factory = env
    calls = pages(platform)
    a = factory(SearchRequest(query="test", max_comments=20, reply_pages=0))
    first = await a.comments(99, 3)
    await a.close()
    b = factory(SearchRequest(query="test", max_comments=10, reply_pages=0))
    second = await b.comments(99, 3)
    await b.close()
    assert len(calls) == 1 and len(first["comments"]) == 20
    assert len(second["comments"]) == 10 and b.http.requests == 0
    assert b.cache_hits == 1
    assert not any(r.url.path.endswith("reply/reply") for r in platform.calls)


async def test_duplicate_page_stops_with_prior_comments_preserved(env):
    _, _, platform, factory = env

    def response(r):
        if r.url.path.endswith("reply/wbi/main"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cursor": {
                            "is_end": False,
                            "pagination_reply": {"next_offset": "opaque-same"},
                        },
                        "replies": [record(1), record(1)],
                    },
                },
            )

    platform.fail = response
    a = factory(SearchRequest(query="test", max_comments=100, reply_pages=0))
    r = await a.comments(99, 3)
    await a.close()
    assert r["status"] == "partial" and r["error"]["code"] == "PAGINATION_STALLED"
    assert len(r["comments"]) == 1 and r["pages_fetched"] == 2


async def test_comment_identity_mismatch_is_rejected(env):
    _, _, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(
            200,
            json={"code": 0, "data": {"cursor": {"is_end": True}, "replies": [record(1, oid=123)]}},
        )
        if r.url.path.endswith("reply/wbi/main")
        else None
    )
    a = factory(SearchRequest(query="test", reply_pages=0))
    r = await a.comments(99, 3)
    await a.close()
    assert r["status"] == "failed" and r["error"]["code"] == "CONTENT_IDENTITY_MISMATCH"
    assert r["comments"] == []


async def test_latest_numeric_cursor_can_decrease(env):
    _, _, platform, factory = env
    calls = []

    def response(r):
        if r.url.path.endswith("reply/wbi/main"):
            c = int(r.url.params["next"])
            calls.append(c)
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cursor": {
                            "is_end": len(calls) == 3,
                            "next": 100 if c == 0 else c - 20,
                            "mode": 2,
                        },
                        "replies": [record(200 - len(calls))],
                    },
                },
            )

    platform.fail = response
    a = factory(SearchRequest(query="test", max_comments=100, comment_pages=3, reply_pages=0))
    r = await a.comments(99, 2)
    await a.close()
    assert calls == [0, 100, 80] and r["sort"] == "latest" and r["status"] == "complete"


async def test_danmaku_one_bulk_fetch_across_budgets_and_cid_binding(env):
    _, store, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(
            200,
            text="<i><chatid>100</chatid>"
            + "".join(f'<d p="{n},1,25,16777215,1000,0,private,{n}">line{n}</d>' for n in range(3))
            + "</i>",
        )
        if r.url.path.endswith(".xml")
        else None
    )
    a = factory(SearchRequest(query="test", max_danmaku=1))
    first = await a.danmaku(100)
    await a.close()
    b = factory(SearchRequest(query="test", max_danmaku=3))
    second = await b.danmaku(100)
    await b.close()
    assert len(first["danmaku"]) == 1 and first["status"] == "partial"
    assert len(second["danmaku"]) == 3 and second["status"] == "complete"
    assert second["cached"] and b.http.requests == 0
    assert sum(r.url.path.endswith(".xml") for r in platform.calls) == 1
    key = cache_key("danmaku-pool-v2", {"cid": 101})
    store.cache_put(key, {"cid": 100}, 1800)
    c = factory()
    with pytest.raises(Exception, match="CID"):
        await c.danmaku(101)
    await c.close()


async def test_tiered_extraction_disables_unneeded_components_and_fetch_compatible(env):
    settings, store, platform, factory = env
    for sorts, expected_modes in (([], []), (["hot"], ["3"])):
        platform.calls.clear()
        req = SearchRequest(
            query="test",
            content_ids=[BV],
            comment_sorts=sorts,
            max_comments=20,
            reply_pages=0,
            subtitle_tracks=0,
            max_danmaku=0,
        )
        task = store.create_task(req.model_dump())
        a = factory(req, task["id"])
        await execute(settings, store, task["id"], adapter_factory=lambda *args: a)
        modes = [
            r.url.params["mode"] for r in platform.calls if r.url.path.endswith("reply/wbi/main")
        ]
        assert modes == expected_modes
        assert not any(
            "/player/" in r.url.path
            or r.url.path.endswith(".xml")
            or r.url.path.endswith("reply/reply")
            for r in platform.calls
        )
        saved = fetch_saved(store, task["id"], BV, "comments_hot")
        assert saved["coverage"]["status"] == ("complete" if sorts else "not_requested")
        assert store.task(task["id"])["status"] == "completed"


async def test_auth_preflight_reuses_signing_keys_without_second_nav(env):
    from personal_search.auth import AuthStore

    settings, _, platform, factory = env
    AuthStore(settings.data_dir).save({"SESSDATA": "fixture-only"})
    platform.fail = lambda r: (
        httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "isLogin": True,
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/" + "a" * 32 + ".png",
                        "sub_url": "https://i0.hdslb.com/" + "b" * 32 + ".png",
                    },
                },
            },
        )
        if r.url.path.endswith("/nav")
        else None
    )
    a = factory()
    await a.preflight()
    await a.video_details(BV)
    await a.close()
    assert sum(r.url.path.endswith("/nav") for r in platform.calls) == 1
    assert a.http.requests == 2


async def test_opaque_cursor_may_repeat_while_unique_comment_rows_advance(env):
    _, _, platform, factory = env
    count = 0

    def response(r):
        nonlocal count
        if r.url.path.endswith("reply/wbi/main"):
            count += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "cursor": {
                            "is_end": count == 3,
                            "pagination_reply": {"next_offset": "same"},
                        },
                        "replies": [record(count)],
                    },
                },
            )

    platform.fail = response
    a = factory(SearchRequest(query="test", max_comments=20, reply_pages=0))
    r = await a.comments(99, 3)
    await a.close()
    assert r["status"] == "complete" and len(r["comments"]) == 3
    assert count == 3


async def test_danmaku_xml_wrong_cid_and_risk_control_are_not_cached(env):
    _, store, platform, factory = env
    platform.fail = lambda r: (
        httpx.Response(200, text="<i><chatid>999</chatid></i>")
        if r.url.path.endswith(".xml")
        else None
    )
    a = factory()
    with pytest.raises(Exception, match="CID"):
        await a.danmaku(100)
    assert store.cache_get(cache_key("danmaku-pool-v2", {"cid": 100})) is None
    await a.close()
    platform.fail = lambda r: httpx.Response(412) if r.url.path.endswith("reply/wbi/main") else None
    b = factory(SearchRequest(query="test", max_comments=100, reply_pages=0))
    r = await b.comments(99, 3)
    assert r["error"]["code"] == "RISK_CONTROL"
    before = len(platform.calls)
    later = await b.comments(99, 2)
    assert later["error"]["code"] == "RISK_CONTROL" and len(platform.calls) == before
    await b.close()
