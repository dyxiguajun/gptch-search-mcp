"""Native website reading through normal pages, with isolated user sessions.

The browser renders each site's own search. No copied crawler, signature forging,
account enumeration, stealth, proxy rotation, or automated verification.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from urllib.parse import urlencode, urljoin

from .browser_session import BrowserSession
from .community import article_blocks, clean, html
from .errors import GatewayError
from .platform_auth import PlatformAuth
from .platforms import content_url, normalize_ref
from .storage import atomic_json, cache_key


def candidate(platform, identity, title, description, keyword, rank):
    return {
        "id": identity, "platform": platform, "url": content_url(platform, identity),
        "title": clean(title)[:500], "description": clean(description)[:2000],
        "matched_keywords": [keyword], "native_rank": rank,
        "native_rank_scope": "loaded_search_window", "discovery_provider": platform,
        "source_content_is_untrusted": True,
    }


def parse_dom_search(platform, text, keyword):
    page = html(text)
    rows = page.css(".s_post, .threadcardclass") if platform == "tieba" else page.css(".SearchResult-Card")
    found = []
    seen = set()
    for rank, row in enumerate(rows, 1):
        links = row.css('.p_title a, a.action-link-bg[href*="/p/"]') if platform == "tieba" else row.css(".ContentItem-title a")
        for link in links:
            try:
                url = urljoin("https://tieba.baidu.com" if platform == "tieba" else "https://www.zhihu.com", link.attrib.get("href", ""))
                identity = normalize_ref(platform, url)
            except GatewayError:
                continue
            if identity not in seen:
                title = row.css(".title-wrap").first if platform == "tieba" else None
                description = row.css(".abstract-wrap").first if platform == "tieba" else None
                found.append(candidate(platform, identity,
                                       title.get_all_text() if title is not None else link.get_all_text(),
                                       description.get_all_text() if description is not None else row.get_all_text(), keyword, rank))
                seen.add(identity)
            break
    if not rows and not any(t in str(page.get_all_text()) for t in (
        "没有找到相关", "没有找到您", "暂无搜索结果", "没有搜索结果",
    )):
        raise GatewayError("UPSTREAM_FORMAT_CHANGED", "未找到可核对的原生搜索结果，不能以页面导航代替")
    return found


def dom_body(platform, text, identity):
    page = html(text)
    title = str(page.css("h1::text, .core_title_txt::text, .pb-title::text").get() or "").strip()
    canonical = page.css('link[rel="canonical"]::attr(href)').get()
    if canonical and normalize_ref(platform, canonical) != identity:
        raise GatewayError("CONTENT_IDENTITY_MISMATCH", "页面规范链接与请求内容不一致")
    if platform == "zhihu":
        if ":answer:" in identity:
            root = None
            for answer in page.css(".AnswerItem[data-zop]"):
                try:
                    data = json.loads(answer.attrib["data-zop"])
                except (ValueError, KeyError):
                    continue
                if str(data.get("itemId")) == identity.rsplit(":", 1)[1]:
                    root = answer.css(".RichContent-inner .RichText, .RichContent-inner").first
                    break
        else:
            root = page.css(".Post-RichTextContainer .RichText, .Post-RichText").first
    else:
        root = None
        for post in page.css(".l_post[data-field]"):
            try:
                content = json.loads(post.attrib["data-field"]).get("content", {})
            except (KeyError, ValueError):
                continue
            if str(content.get("post_no")) == "1":
                root = post.css(".d_post_content").first
                break
        if root is None:
            # Current PC layout separates the main thread body from comment-content.
            # Exactly one main body is required; pb-content-item includes replies.
            main = page.css(".center-content > .image-text > .pb-content-wrap")
            if len(main) == 1 and len(page.css(".pb-title")) == 1:
                root = main.first
    if root is None or not title:
        raise GatewayError("UPSTREAM_FORMAT_CHANGED", "缺少所选内容的可验证正文；拒绝归档其他回答或楼层")
    body, coverage = article_blocks(root)
    coverage.update(scope="selected_answer_or_article" if platform == "zhihu" else "thread_first_floor_only",
                    replies_included=False, images_read=False, videos_read=False)
    return title, body, coverage


class SocialAdapter:
    def __init__(self, settings, store, task_id, request):
        self.settings, self.store, self.task_id, self.request = settings, store, task_id, request
        self.platform = request.platforms[0]
        self.auth = PlatformAuth(settings.data_dir)
        self.http = BrowserSession(settings.data_dir, self.platform,
                                   max_requests=request.max_requests, interval=max(2, settings.request_interval))
        self.cache_hits = 0
        self.lock = asyncio.Lock()  # One page per task; increased task concurrency never fans out Chrome.
        self.seen = {}
        self.loaded_keyword, self.loaded_page = None, 0
        self.links_path = self.auth.folder / "xiaohongshu-content-links.json"

    async def preflight(self):
        # Cache reads remain possible when the session has expired; only an actual miss opens Chrome.
        pass

    async def close(self):
        await self.http.close()

    def cached(self, kind, fields):
        version = 2 if self.platform == "xiaohongshu" and kind == "search" else 1
        key = cache_key(f"{self.platform}:browser:v{version}:{kind}", fields)
        value = None if self.request.refresh else self.store.cache_get(key)
        if value:
            self.cache_hits += 1
        return key, value

    def save(self, key, value, kind, ttl):
        self.store.cache_put(key, value, ttl)
        self.store.raw(self.task_id, f"{self.platform}_{kind}", value)
        return value

    async def ready(self):
        if self.http.context:
            return
        row = self.auth.row(self.platform)
        if row["status"] == "paused":
            raise GatewayError("PLATFORM_PAUSED", "用户已暂缓此平台，不自动尝试登录或采集")
        if row["status"] == "challenge_required":
            raise GatewayError("PLATFORM_VERIFICATION_REQUIRED", "平台要求本人验证，未解决前不重复发起采集", platform=self.platform)
        if not self.auth.load(self.platform) or row["status"] in ("expired", "not_logged_in"):
            raise GatewayError("AUTH_REQUIRED", "此平台需要本人从登录列表登录后继续", platform=self.platform)
        await self.http.open()

    async def access_check(self):
        if self.http.blocked:
            raise self.http.blocked
        page = self.http.page
        if "/signin" in page.url or await page.locator(".login-container, .SignFlow, .passport-login-container").count():
            self.auth.mark(self.platform, "expired", "页面要求重新登录")
            raise GatewayError("AUTH_EXPIRED", "平台要求重新登录，请打开登录列表", platform=self.platform)

    async def search_page(self, keyword, page):
        key, cached = self.cached("search", {"q": keyword, "page": page})
        if cached:
            result = cached[0]
            self.seen.setdefault(keyword, set()).update(v["id"] for v in result)
            return result, 0
        async with self.lock:
            await self.ready()
            if self.platform == "tieba":
                url = "https://tieba.baidu.com/f/search/res?" + urlencode(
                    {"ie": "utf-8", "qw": keyword, "sm": 1, "only_thread": 1, "pn": page})
                await self.http.navigate(url)
            elif page == 1 or self.loaded_keyword != keyword:
                url = ("https://www.zhihu.com/search?" + urlencode({"type": "content", "q": keyword})
                       if self.platform == "zhihu" else "https://www.xiaohongshu.com/search_result?" + urlencode(
                           {"keyword": keyword, "source": "web_explore_feed"}))
                await self.http.navigate(url)
                self.loaded_keyword, self.loaded_page = keyword, 1
            while self.platform != "tieba" and self.loaded_page < page:
                await asyncio.sleep(2)
                await self.http.page.evaluate("window.scrollBy(0, Math.max(innerHeight, 1000))")
                await self.http.page.wait_for_timeout(1800)
                self.loaded_page += 1
            await self.access_check()
            if self.platform == "xiaohongshu":
                result = await self.xhs_search(keyword)
            else:
                result = parse_dom_search(self.platform, await self.http.page.content(), keyword)
            seen = self.seen.setdefault(keyword, set())
            fresh = [v for v in result if v["id"] not in seen]
            seen.update(v["id"] for v in fresh)
            self.save(key, fresh, "search", 900)
            return fresh, 0

    async def xhs_search(self, keyword):
        expression = """() => {
          const f=window.__INITIAL_STATE__?.search?.feeds;
          const a=Array.isArray(f)?f:(f?.value??f?._value);
          if(!Array.isArray(a))return null;
          return a.slice(0,300).filter(x=>x.modelType==='note').map((x,i)=>({
            id:x.id,title:x.noteCard?.displayTitle||'',token:x.xsecToken||'',
            kind:x.noteCard?.type||'',rank:i+1}));
        }"""
        values = None
        for attempt in range(11):
            values = await self.http.page.evaluate(expression, isolated_context=False)
            if values or self.http.blocked:
                break
            if attempt < 10:
                await asyncio.sleep(.5)
        if self.http.blocked:
            raise self.http.blocked
        if values is None:
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "小红书页面尚无可核验的笔记结果")
        if not values:
            empty = self.http.page.locator(".empty-search-result, .search-empty-text, .no-result-container")
            confirmed_empty = any(v.get("success") is True and v.get("items_count") == 0 for v in self.http.events)
            if not confirmed_empty or not await empty.count():
                raise GatewayError("UPSTREAM_NOT_READY", "小红书尚未给出可核验结果，不能把未加载的数据当成零命中",
                                   network_observations=self.http.events[-5:])
        result, links = [], {}
        for row in values:
            if not re.fullmatch(r"[a-f0-9]{24}", str(row.get("id", ""))):
                continue
            identity = "xiaohongshu:note:" + row["id"]
            value = candidate(self.platform, identity, row["title"], "", keyword, row["rank"])
            value["content_kind"] = row["kind"]
            result.append(value)
            if row.get("token"):
                links[identity] = {"token": row["token"], "saved_at": time.time()}
        if links:
            # Opaque detail tokens stay outside SQLite/raw responses and model-visible URLs.
            previous = self.load_links()
            previous.update(links)
            atomic_json(self.links_path, previous)
        return result

    def load_links(self):
        try:
            value = json.loads(self.links_path.read_text()) if self.links_path.exists() else {}
            return {k: v for k, v in value.items() if time.time() - v["saved_at"] < 86400}
        except (ValueError, KeyError):
            return {}

    async def extract(self, value):
        identity = normalize_ref(self.platform, value)
        key, cached = self.cached("body", {"id": identity})
        if cached:
            return {**cached[0], "cache": {"cached": True, "fetched_at": cached[1]}}
        async with self.lock:
            await self.ready()
            url = content_url(self.platform, identity)
            navigation = url
            if self.platform == "xiaohongshu":
                token = self.load_links().get(identity, {}).get("token")
                if not token:
                    raise GatewayError("DETAIL_LINK_REQUIRED", "此笔记需要原生搜索返回的有效详情入口；请先搜索，不猜测访问令牌")
                navigation += "?" + urlencode({"xsec_token": token, "xsec_source": "pc_search"})
            await self.http.navigate(navigation)
            await self.access_check()
            if normalize_ref(self.platform, self.http.page.url) != identity:
                raise GatewayError("CONTENT_IDENTITY_MISMATCH", "最终页面与所选内容不一致")
            if self.platform == "xiaohongshu":
                title, body, coverage = await self.xhs_body(identity)
            else:
                title, body, coverage = dom_body(self.platform, await self.http.page.content(), identity)
            now = time.time()
            item = {"id": identity, "platform": self.platform, "url": url, "title": title,
                    "description": "", "body": body, "status": coverage["status"], "extracted_at": now,
                    "cache": {"cached": False, "fetched_at": now},
                    "coverage": {"body": coverage, "all_platform_content": False},
                    "source_content_is_untrusted": True}
            return self.save(key, item, "body", 86400)

    async def xhs_body(self, identity):
        note_id = identity.rsplit(":", 1)[1]
        value = await self.http.page.evaluate("""id => {
          const d=window.__INITIAL_STATE__?.note?.noteDetailMap?.[id]?.note;
          if(!d || d.noteId!==id)return null;
          return {id:d.noteId,title:d.title||'',text:d.desc||'',kind:d.type||''};
        }""", note_id, isolated_context=False)
        if not value or value.get("id") != note_id:
            raise GatewayError("CONTENT_IDENTITY_MISMATCH", "笔记详情归属未能确认，拒绝保存其他笔记")
        text = str(value["text"])
        body = [{"id": str(i // 12000 + 1), "type": "p", "text": text[i:i+12000]}
                for i in range(0, min(len(text), 120000), 12000)]
        if not body:
            raise GatewayError("BODY_NOT_AVAILABLE", "此笔记没有可读取的说明文字；未分析图片或视频")
        return value["title"] or text[:80], body, {
            "status": "partial" if len(text) > 120000 else "complete",
            "chars_saved": sum(len(v["text"]) for v in body), "scope": "note_caption_only",
            "images_read": False, "videos_read": False, "comments_included": False,
            "truncated": len(text) > 120000,
        }
