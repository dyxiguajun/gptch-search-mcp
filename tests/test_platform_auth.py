import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

import httpx
import psutil
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from personal_search.auth_panel import LoginController, ensure_panel
from personal_search.browser_cache import StaticCache
from personal_search.browser_session import BrowserSession, account_verified
from personal_search.errors import GatewayError
from personal_search.models import SearchRequest
from personal_search.platform_auth import PlatformAuth
from personal_search.platforms import content_url, normalize_ref
from personal_search.settings import Settings
from personal_search.social import SocialAdapter, dom_body, parse_dom_search
from personal_search.storage import Store
from personal_search.wechat import parse_index, public_body


def stop_owned_panel(root):
    manifest = root / "auth/panel.json"
    if manifest.exists():
        value = json.loads(manifest.read_text())
        try:
            process = psutil.Process(value["pid"])
            if abs(process.create_time() - value["created"]) < .01:
                process.terminate()
                process.wait(5)
        except psutil.NoSuchProcess:
            pass


def test_sessions_are_platform_scoped_and_never_reported_as_authenticated_by_presence(tmp_path):
    auth = PlatformAuth(tmp_path)
    auth.save("xiaohongshu", {
        "cookies": [{"name": "web_session", "value": "PRIVATE", "domain": ".xiaohongshu.com"},
                    {"name": "other", "value": "DROP", "domain": ".zhihu.com"},
                    {"name": "spoof", "value": "DROP", "domain": ".evilxiaohongshu.com"}],
        "origins": [{"origin": "https://www.xiaohongshu.com", "localStorage": []},
                    {"origin": "http://www.xiaohongshu.com", "localStorage": []},
                    {"origin": "https://www.zhihu.com", "localStorage": []}],
    })
    state = auth.load("xiaohongshu")
    assert len(state["cookies"]) == len(state["origins"]) == 1
    assert auth.row("xiaohongshu")["status"] == "saved_unverified"
    assert "PRIVATE" not in json.dumps(auth.catalogue())
    assert '"cookies":' not in json.dumps(auth.catalogue())
    if os.name != "nt":
        assert auth.folder.stat().st_mode & 0o777 == 0o700
        assert auth.state_path("xiaohongshu").stat().st_mode & 0o777 == 0o600
    assert auth.catalogue()["first_use"]
    auth.acknowledge()
    assert not auth.catalogue()["first_use"]


@pytest.mark.parametrize("platform,link,identity", [
    ("xiaohongshu", "https://www.xiaohongshu.com/explore/0123456789abcdef01234567?xsec_token=PRIVATE", "xiaohongshu:note:0123456789abcdef01234567"),
    ("zhihu", "https://www.zhihu.com/question/123/answer/456?utm=foo", "zhihu:answer:123:456"),
    ("zhihu", "https://zhuanlan.zhihu.com/p/1234", "zhihu:article:1234"),
    ("tieba", "https://tieba.baidu.com/p/1234567890?pn=2", "tieba:thread:1234567890"),
    ("wechat", "https://mp.weixin.qq.com/s/public_link_12345", "wechat:short:public_link_12345"),
    ("wechat", "https://mp.weixin.qq.com/s?__biz=MzABCDEF==&mid=123456&idx=1&scene=2", "wechat:article:MzABCDEF==:123456:1"),
    ("wechat", "https://mp.weixin.qq.com/s?__biz=MzABCDEF==&mid=123456&idx=1&sn=0123456789abcdef0123456789abcdef", "wechat:article:MzABCDEF==:123456:1:0123456789abcdef0123456789abcdef"),
])
def test_identity_separates_public_content_from_tracking_and_session_tokens(platform, link, identity):
    request = SearchRequest(query="selected", platforms=[platform], content_ids=[link])
    assert request.content_ids == [identity]
    assert normalize_ref(platform, content_url(platform, identity)) == identity
    assert "PRIVATE" not in json.dumps(request.model_dump())


def test_wrong_hosts_ports_and_other_platform_ids_are_rejected():
    for link in ("https://www.zhihu.com.evil/question/1/answer/2", "http://www.zhihu.com/question/1/answer/2",
                 "https://www.zhihu.com:8443/question/1/answer/2", "tieba:thread:123456"):
        with pytest.raises(GatewayError):
            normalize_ref("zhihu", link)


def test_tieba_real_login_response_shape_does_not_require_nonexistent_user_id():
    assert account_verified("tieba", {"no": 0, "data": {"is_login": 1, "user_itieba_id": "synthetic"}})
    assert account_verified("tieba", {"no": 0, "data": {"is_login": 1}})
    assert not account_verified("tieba", None)
    assert not account_verified("tieba", {"no": 0, "data": {"is_login": 0, "user_itieba_id": "synthetic"}})
    assert not account_verified("tieba", {"no": 1, "data": {"is_login": 1}})


@pytest.mark.parametrize("status", ["paused", "challenge_required"])
async def test_user_paused_or_unresolved_challenge_never_opens_browser(tmp_path, status):
    auth = PlatformAuth(tmp_path)
    auth.mark("zhihu", status, "未解决的访问状态")
    request = SearchRequest(query="test", platforms=["zhihu"])
    adapter = SocialAdapter(Settings(tmp_path), Store(tmp_path), "fixture", request)
    adapter.http.open = AsyncMock()
    with pytest.raises(GatewayError):
        await adapter.ready()
    adapter.http.open.assert_not_awaited()


async def test_human_platform_selection_acknowledges_first_use_without_faking_auth(tmp_path):
    controller = LoginController(Settings(tmp_path))
    controller.login = AsyncMock()
    try:
        assert controller.auth.catalogue()["first_use"]
        await controller.action("xiaohongshu", "login")
        assert not controller.auth.catalogue()["first_use"]
        assert controller.auth.row("xiaohongshu")["status"] == "login_in_progress"
        assert not controller.auth.state_path("xiaohongshu").exists()
    finally:
        await controller.close()


def test_native_search_and_selected_answer_do_not_mix_other_answers():
    search = '''<div class="SearchResult-Card"><div class="ContentItem-title"><a href="//www.zhihu.com/question/123/answer/456">操作教程</a></div></div>'''
    rows = parse_dom_search("zhihu", search, "教程")
    assert rows[0]["id"] == "zhihu:answer:123:456"
    text = '''<h1>问题</h1><link rel="canonical" href="https://www.zhihu.com/question/123/answer/456">
    <div class="AnswerItem" data-zop='{"itemId":"999"}'><div class="RichContent-inner">WRONG</div></div>
    <div class="AnswerItem" data-zop='{"itemId":"456"}'><div class="RichContent-inner"><div class="RichText"><p>正确内容</p><pre><code>async def f():\n    pass</code></pre></div></div></div>'''
    _, body, _ = dom_body("zhihu", text, rows[0]["id"])
    assert "WRONG" not in json.dumps(body)
    assert body[-1]["text"] == "async def f():\n    pass"
    with pytest.raises(GatewayError):
        dom_body("zhihu", text.replace('"456"', '"777"'), rows[0]["id"])


def test_tieba_reads_first_floor_only():
    page = '''<h1 class="core_title_txt">主题</h1>
    <div class="l_post" data-field='{"content":{"post_no":2}}'><div class="d_post_content">WRONG</div></div>
    <div class="l_post" data-field='{"content":{"post_no":1}}'><div class="d_post_content"><p>主帖</p></div></div>'''
    _, body, coverage = dom_body("tieba", page, "tieba:thread:123456")
    assert body[0]["text"] == "主帖"
    assert not coverage["replies_included"]
    assert "WRONG" not in json.dumps(body)


def test_modern_tieba_native_search_uses_title_and_abstract_instead_of_overlay_text():
    rows = parse_dom_search("tieba", '''<div class="threadcardclass">
      <a class="action-link-bg" href="/p/123456789"></a>
      <div class="title-wrap">新卡片标题</div><div class="abstract-wrap">摘要</div>
      <span>不需要的账号资料</span></div>''', "Claude")
    assert len(rows) == 1
    assert rows[0]["title"] == "新卡片标题"
    assert rows[0]["description"] == "摘要"
    assert "账号资料" not in json.dumps(rows, ensure_ascii=False)


def test_modern_tieba_body_is_scoped_to_main_post_not_comments():
    text = '''<span class="pb-title">主题</span><div class="center-content"><div class="image-text">
      <div class="pb-content-wrap"><div class="richtext-item">主帖内容</div></div></div>
      <div class="comment-content"><div class="pb-content-item">WRONG</div></div></div>'''
    title, body, coverage = dom_body("tieba", text, "tieba:thread:123456789")
    assert title == "主题" and body[0]["text"] == "主帖内容"
    assert "WRONG" not in json.dumps(body)
    assert coverage["scope"] == "thread_first_floor_only"


def test_public_static_cache_excludes_api_and_account_responses_and_is_bounded(tmp_path):
    cache = StaticCache(tmp_path)
    def request(url, resource="script"):
        return SimpleNamespace(url=url, resource_type=resource, method="GET")
    assert cache.eligible(request("https://static.xhscdn.com/hash/app.js"))
    assert not cache.eligible(request("https://www.xiaohongshu.com/api/user", "fetch"))
    assert not cache.eligible(request("https://evilxhscdn.com/app.js"))
    cache.put("https://static.xhscdn.com/hash/app.js", b"public script", "application/javascript")
    assert cache.get("https://static.xhscdn.com/hash/app.js")[0] == b"public script"
    cache.entry_bytes = 5
    cache.put("https://static.xhscdn.com/big.js", b"too large body", "application/javascript")
    assert cache.get("https://static.xhscdn.com/big.js") is None


def test_wechat_index_is_labelled_and_does_not_archive_redirect_tokens():
    page = '''<ul class="news-list"><li><h3><a href="/link?url=opaque&amp;token=PRIVATE">公开文章</a></h3><p class="txt-info">摘要</p></li></ul>'''
    rows, private_links = parse_index(page, "教程", 1)
    assert rows[0]["platform_native_search"] is False
    assert rows[0]["discovery_provider"] == "sogou_wechat_index"
    assert "PRIVATE" not in json.dumps(rows)
    assert "PRIVATE" in json.dumps(private_links)
    with pytest.raises(GatewayError):
        public_body("<title>搜狗搜索</title><form>验证码</form>")
    _, body, _ = public_body('<h1 id="activity-name">标题</h1><div id="js_content"><p>正文</p></div>')
    assert body[0]["text"] == "正文"


async def test_xhs_opaque_detail_tokens_only_go_to_protected_auth_storage(tmp_path):
    request = SearchRequest(query="教程", platforms=["xiaohongshu"])
    adapter = SocialAdapter(Settings(tmp_path), Store(tmp_path), "fixture", request)
    adapter.http.page = SimpleNamespace(evaluate=AsyncMock(return_value=[{
        "id": "0123456789abcdef01234567", "title": "标题", "token": "PRIVATE", "kind": "normal", "rank": 1,
    }]))
    rows = await adapter.xhs_search("教程")
    assert "PRIVATE" not in json.dumps(rows)
    assert "PRIVATE" in adapter.links_path.read_text()
    if os.name != "nt":
        assert adapter.links_path.stat().st_mode & 0o777 == 0o600


async def test_scrapling_chrome_reads_main_world_site_state_and_releases_single_browser(tmp_path):
    if not Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists():
        pytest.skip("本机Chrome集成验证，不下载浏览器")
    browser = BrowserSession(tmp_path, "xiaohongshu", max_requests=5)
    second = BrowserSession(tmp_path, "xiaohongshu", max_requests=5)
    try:
        await browser.open()
        with pytest.raises(GatewayError, match="另一个平台窗口"):
            await second.open()
        await browser.page.set_content('''<script>window.__INITIAL_STATE__={search:{feeds:{value:[{
          id:'0123456789abcdef01234567',modelType:'note',xsecToken:'PRIVATE',noteCard:{displayTitle:'页面原生状态',type:'normal'}
        }]}}};</script>''')
        request = SearchRequest(query="教程", platforms=["xiaohongshu"])
        adapter = SocialAdapter(Settings(tmp_path), Store(tmp_path), "fixture", request)
        adapter.http = browser
        rows = await adapter.xhs_search("教程")
        assert rows[0]["title"] == "页面原生状态"
        assert "PRIVATE" not in json.dumps(rows)
        assert browser.requests == 0
        assert not browser.engine._config.solve_cloudflare
        assert not browser.engine.network.enabled
    finally:
        await browser.close()
    # Closing the session releases the OS-level lease for the next caller.
    second.lease.acquire(timeout=0)
    second.lease.release()


async def test_real_loopback_selector_rejects_missing_token_cross_origin_and_foreign_host(tmp_path):
    settings = Settings(tmp_path)
    settings.prepare()
    panel = await ensure_panel(settings)
    url = urlsplit(panel["url"])
    base = f"http://{url.netloc}"
    headers = {"X-GPTCH-Token": url.fragment}
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            assert (await client.get(base + "/api/status")).status_code == 403
            assert (await client.get(base + "/api/status", headers={**headers, "Origin": "https://evil.example"})).status_code == 403
            assert (await client.get(base + "/api/status", headers={**headers, "Host": "evil.example"})).status_code == 403
            response = await client.get(base + "/api/status", headers=headers)
            assert response.status_code == 200
            assert len(response.json()["platforms"]) == 7
            assert response.json()["active_platform"] is None
            assert '"cookies":' not in response.text
            assert (await client.post(base + "/api/ack", headers=headers, json={})).status_code == 200
            assert not PlatformAuth(tmp_path).catalogue()["first_use"]
    finally:
        stop_owned_panel(tmp_path)


async def test_real_mcp_auth_required_returns_selector_without_queueing_or_opening_platform(tmp_path):
    params = StdioServerParameters(command=sys.executable, args=["-m", "personal_search.server"],
                                   env={**os.environ, "GPTCH_DATA_DIR": str(tmp_path)})
    try:
        async with stdio_client(params) as (reader, writer), ClientSession(reader, writer) as session:
            await session.initialize()
            status = await session.call_tool("auth_status", {"platform": "all"})
            assert len(json.loads(status.content[0].text)["platforms"]) == 7
            result = await session.call_tool("deep_search", {"request": {
                "query": "教程", "platforms": ["xiaohongshu"], "depth": "quick",
            }})
            value = json.loads(result.content[0].text)
            assert value["status"] == "requires_login"
            assert value["login_panel"]["local_only"]
            assert not Store(tmp_path).queued()
            assert not PlatformAuth(tmp_path).state_path("xiaohongshu").exists()
            assert PlatformAuth(tmp_path).row("xiaohongshu")["status"] == "not_logged_in"
    finally:
        stop_owned_panel(tmp_path)
