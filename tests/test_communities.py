"""Simulated native-platform protocol regressions, separate from real acceptance logs."""

import json

import httpx
import pytest

from personal_search.community import JuejinAdapter, SegmentFaultAdapter, article_blocks, html
from personal_search.errors import GatewayError
from personal_search.http import HEADERS, Transport, validate_url
from personal_search.models import SearchRequest
from personal_search.platforms import content_key, normalize_ref
from personal_search.results import fetch_saved
from personal_search.settings import Settings
from personal_search.storage import Store
from personal_search.worker import execute

JID = "7346233811212566569"
SID = "1190000043800818"


class NativeFixture:
    def __init__(self, platform):
        self.platform = platform
        self.calls = []
        self.wrong_canonical = False
        self.filtered_first_page = False
        self.bad_cursor = False
        self.changed_format = False

    def __call__(self, request):
        self.calls.append(request)
        assert "cookie" not in request.headers
        if self.platform == "juejin" and request.url.path.endswith("/search"):
            data = json.loads(request.content)
            assert data["search_type"] == 0
            cursor = data["cursor"]
            native = JID if cursor == "0" else str(int(JID) + 1)
            return httpx.Response(
                200,
                json={
                    "err_no": 0,
                    "data": [
                        {
                            "result_type": 2,
                            "result_model": {
                                "article_info": {
                                    "article_id": None if self.changed_format else native,
                                    "title": "<em>asyncio</em>教程",
                                    "brief_content": "Python async",
                                    "ctime": "1700000000",
                                    "is_original": 1,
                                },
                                "author_user_info": {
                                    "user_name": "Public author",
                                    "user_id": "UNNECESSARY_PROFILE",
                                    "avatar_large": "UNNECESSARY_PROFILE",
                                },
                            },
                        }
                    ],
                    "has_more": cursor == "0",
                    "cursor": cursor
                    if self.bad_cursor
                    else "opaque-next"
                    if cursor == "0"
                    else "end",
                },
            )
        if self.platform == "segmentfault" and request.url.path == "/search":
            page = int(request.url.params["page"])
            q = request.url.params["q"]
            rows = (
                [{"type": "question", "contents": {"url": "/q/1010000000000000"}}]
                if self.filtered_first_page and page == 1
                else [
                    {
                        "type": "article",
                        "contents": {
                            "url": f"/a/{SID}",
                            "title": "asyncio教程",
                            "excerpt": "native article",
                            "created": 1700000000,
                        },
                    }
                ]
            )
            state = {
                "search": {
                    "result": {
                        "q": q,
                        "page": page,
                        "rows": rows,
                        "total_page": 2 if self.filtered_first_page else 1,
                    }
                }
            }
            return httpx.Response(
                200,
                text='<script id="__NEXT_DATA__" type="application/json">'
                + json.dumps({"props": {"pageProps": {"initialState": state}}})
                + "</script>",
            )
        native = request.url.path.rsplit("/", 1)[1]
        url = (
            f"https://juejin.cn/post/{native}"
            if self.platform == "juejin"
            else f"https://segmentfault.com/a/{native}"
        )
        if self.wrong_canonical:
            url = url.replace(native, str(int(native) + 100))
        head = f'<link rel="canonical" href="{url}"><h1>asyncio教程</h1><time datetime="2024-01-01T00:00:00Z" itemprop="datePublished">date</time>'
        body = '<h2>Example</h2><p>Python asyncio evidence</p><pre><code class="language-python">async def run():\n    return 42\n</code></pre><p>Last paragraph <a href="https://docs.python.org/3/library/asyncio.html">source</a></p>'
        if self.platform == "juejin":
            return httpx.Response(
                200, text=head + '<article id="article-root">' + body + "</article>"
            )
        state = {
            "articleDetail": {
                "artDetail": {
                    native: {
                        "article": {
                            "id": native,
                            "title": "asyncio教程",
                            "user": {"name": "Public author", "avatar": "UNNECESSARY_PROFILE"},
                            "origin_url": "https://docs.python.org/3/library/asyncio.html",
                        }
                    }
                }
            }
        }
        return httpx.Response(
            200,
            text=head
            + '<article class="article-content">'
            + body
            + '</article><script id="__NEXT_DATA__">'
            + json.dumps({"props": {"pageProps": {"initialState": state}}})
            + "</script>",
        )


def environment(tmp_path, platform):
    settings = Settings(tmp_path, request_interval=0)
    settings.prepare()
    store = Store(tmp_path)
    fixture = NativeFixture(platform)
    cls = JuejinAdapter if platform == "juejin" else SegmentFaultAdapter

    def factory(_settings=None, _store=None, task_id=None, request=None):
        request = request or SearchRequest(
            query="asyncio", platforms=[platform], max_items=1, pages_per_keyword=1
        )
        task_id = task_id or store.create_task(request.model_dump())["id"]
        transport = Transport(
            client=httpx.AsyncClient(transport=httpx.MockTransport(fixture), headers=HEADERS),
            interval=0,
            max_requests=request.max_requests,
            concurrency=1,
            api_base=cls.base,
            allowed_hosts=cls.hosts,
        )
        return cls(settings, store, task_id, request, transport=transport)

    return settings, store, fixture, factory


@pytest.mark.parametrize(
    "platform,value,expected",
    [
        ("bilibili", "https://www.bilibili.com/video/BV1GJ411x7h7?p=2", "BV1GJ411x7h7?p=2"),
        ("juejin", f"https://juejin.cn/post/{JID}?utm_source=test", f"juejin:post:{JID}"),
        (
            "segmentfault",
            f"https://segmentfault.com/a/{SID}#heading",
            f"segmentfault:article:{SID}",
        ),
    ],
)
def test_namespaced_identity_normalization_and_budget_defaults(platform, value, expected):
    request = SearchRequest(query="test", platforms=[platform], content_ids=[value])
    assert request.content_ids == [expected] and request.max_items == 1
    assert normalize_ref(platform, expected) == expected
    if platform == "bilibili":
        assert content_key(platform, expected) == "BV1GJ411x7h7"


def test_cross_platform_or_arbitrary_content_targets_are_rejected():
    for value in (
        f"https://evil.test/post/{JID}",
        f"https://juejin.cn@evil.test/post/{JID}",
        f"http://juejin.cn/post/{JID}",
        f"https://segmentfault.com/a/{SID}",
    ):
        with pytest.raises(GatewayError):
            normalize_ref("juejin", value)
    validate_url("https://juejin.cn/post/" + JID, allowed_hosts={"juejin.cn"})
    with pytest.raises(GatewayError):
        validate_url("https://api.bilibili.com/x", allowed_hosts={"juejin.cn"})
    with pytest.raises(GatewayError):
        validate_url("https://a.hdslb.com/x", allowed_hosts={"juejin.cn"})


def test_parser_preserves_code_order_whitespace_and_exact_budget_boundary():
    code = "async def run():\n    return 42\n"
    page = html(
        '<article><p>first</p><pre><code class="language-python">'
        + code
        + "</code><button>copy</button></pre><ul><li>one</li><li>two</li></ul><script>UNTRUSTED_SCRIPT</script></article>"
    )
    records, cov = article_blocks(page.css("article").first)
    assert [r["type"] for r in records] == ["p", "code", "ul"]
    assert records[1]["text"] == code and records[1]["language"] == "python"
    assert "UNTRUSTED_SCRIPT" not in "".join(r["text"] for r in records)
    exact, coverage = article_blocks(
        html("<article><p>12345</p></article>").css("article").first, limit=5
    )
    assert exact[0]["text"] == "12345" and coverage["status"] == "complete"
    _, coverage = article_blocks(
        html("<article><p>123456</p></article>").css("article").first, limit=5
    )
    assert coverage["status"] == "partial" and coverage["chars_saved"] == 5


async def test_juejin_opaque_pagination_cache_and_minimal_raw_archives(tmp_path):
    _, store, fixture, factory = environment(tmp_path, "juejin")
    adapter = factory()
    first, total = await adapter.search_page("asyncio", 1)
    second, total = await adapter.search_page("asyncio", 2)
    assert total == 2 and first[0]["id"] != second[0]["id"]
    assert json.loads(fixture.calls[1].content)["cursor"] == "opaque-next"
    await adapter.close()
    before = len(fixture.calls)
    another = factory()
    await another.search_page("asyncio", 2)
    assert another.http.requests == 0 and another.cache_hits == 1 and len(fixture.calls) == before
    await another.close()
    content = "".join(p.read_text() for p in (store.root / "raw").rglob("*.json"))
    with store.connect() as db:
        content += "".join(r[0] for r in db.execute("select payload from cache"))
    assert "UNNECESSARY_PROFILE" not in content


@pytest.mark.parametrize("platform", ["juejin", "segmentfault"])
async def test_worker_body_archive_readback_fts_and_no_bilibili_auth_requests(tmp_path, platform):
    settings, store, fixture, factory = environment(tmp_path, platform)
    request = SearchRequest(query="asyncio", platforms=[platform], pages_per_keyword=1, max_items=1)
    task = store.create_task(request.model_dump())
    await execute(settings, store, task["id"], adapter_factory=factory)
    assert store.task(task["id"])["status"] == "completed"
    item = store.items(task["id"])[0]
    assert item["platform"] == platform and item["source_content_is_untrusted"]
    body = fetch_saved(store, task["id"], item["id"], "body", limit=1)
    assert body["platform"] == platform and body["has_more"]
    rest = fetch_saved(store, task["id"], item["id"], "body", offset=body["next_offset"], limit=200)
    assert any(r["type"] == "code" and "    return 42" in r["text"] for r in rest["records"])
    meta = fetch_saved(store, task["id"], item["id"], "metadata")
    assert meta["metadata"]["links"] == ["https://docs.python.org/3/library/asyncio.html"]
    assert store.archive_search("asyncio")[0]["content_id"] == item["id"]
    with pytest.raises(GatewayError) as error:
        fetch_saved(store, task["id"], item["id"], "subtitles")
    assert error.value.code == "SECTION_NOT_SUPPORTED_FOR_PLATFORM"
    assert not any(r.url.host == "api.bilibili.com" for r in fixture.calls)


async def test_empty_filtered_native_page_does_not_hide_later_articles(tmp_path):
    settings, store, fixture, factory = environment(tmp_path, "segmentfault")
    fixture.filtered_first_page = True
    request = SearchRequest(
        query="asyncio", platforms=["segmentfault"], pages_per_keyword=2, max_items=0
    )
    task = store.create_task(request.model_dump())
    await execute(settings, store, task["id"], adapter_factory=factory)
    assert store.task(task["id"])["candidate_count"] == 1
    assert [r.url.params["page"] for r in fixture.calls] == ["1", "2"]
    assert not store.items(task["id"])


@pytest.mark.parametrize("platform", ["juejin", "segmentfault"])
async def test_wrong_article_identity_is_not_archived(tmp_path, platform):
    _, store, fixture, factory = environment(tmp_path, platform)
    fixture.wrong_canonical = True
    adapter = factory()
    value = f"juejin:post:{JID}" if platform == "juejin" else f"segmentfault:article:{SID}"
    with pytest.raises(GatewayError) as error:
        await adapter.extract(value)
    assert error.value.code == "CONTENT_IDENTITY_MISMATCH"
    assert not store.items(adapter.task_id)
    await adapter.close()


async def test_bad_cursor_and_changed_article_schema_fail_instead_of_returning_zero(tmp_path):
    _, _, fixture, factory = environment(tmp_path, "juejin")
    fixture.bad_cursor = True
    adapter = factory()
    with pytest.raises(GatewayError) as error:
        await adapter.search_page("asyncio", 1)
    assert error.value.code == "PAGINATION_STALLED"
    await adapter.close()
    fixture.bad_cursor = False
    fixture.changed_format = True
    adapter = factory()
    with pytest.raises(GatewayError) as error:
        await adapter.search_page("different", 1)
    assert error.value.code == "UPSTREAM_FORMAT_CHANGED"
    await adapter.close()


async def test_community_risk_control_does_not_retry_or_request_another_page(tmp_path):
    settings = Settings(tmp_path)
    settings.prepare()
    store = Store(tmp_path)
    calls = []

    def blocked(r):
        calls.append(r)
        return httpx.Response(403)

    request = SearchRequest(query="test", platforms=["juejin"])
    transport = Transport(
        client=httpx.AsyncClient(transport=httpx.MockTransport(blocked)),
        api_base=JuejinAdapter.base,
        allowed_hosts=JuejinAdapter.hosts,
        interval=0,
    )
    adapter = JuejinAdapter(settings, store, "test", request, transport=transport)
    for _ in range(2):
        with pytest.raises(GatewayError) as error:
            await adapter.search_page("test", 1)
        assert error.value.code == "RISK_CONTROL"
    assert len(calls) == 1
    await adapter.close()


def test_highlighted_and_nested_code_keeps_whitespace_and_document_order():
    source = html(
        '<article><p>Before <em>inline</em> after</p><ul><li>First step<pre><code class="language-python"><span>async</span> <span>def</span> run():\n    return 42\n</code></pre>after code</li><li>Second step<pre><code>import asyncio\n\nasync def next_step():\n    pass\n</code></pre></li></ul></article>'
    )
    records, coverage = article_blocks(source.css("article").first)
    codes = [r for r in records if r["type"] == "code"]
    assert len(codes) == 2
    assert codes[0]["text"] == "async def run():\n    return 42\n"
    assert codes[1]["text"].startswith("import asyncio\n\nasync def")
    assert [r["text"] for r in records] == [
        "Before inline after",
        "First step",
        codes[0]["text"],
        "after code",
        "Second step",
        codes[1]["text"],
    ]
    assert coverage["status"] == "complete"


def test_cli_advertises_and_accepts_new_platform_body_contract():
    from personal_search.cli import parser

    search = parser().parse_args(["search", "asyncio", "--platform", "juejin", "--depth", "quick"])
    assert search.platform == "juejin" and search.depth == "quick"
    read = parser().parse_args(["fetch", "task", f"juejin:post:{JID}", "--section", "body"])
    assert read.section == "body"
