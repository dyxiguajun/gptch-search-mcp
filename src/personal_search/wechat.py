"""Public WeChat articles and explicitly labelled Sogou index discovery.

This is not the native in-app WeChat search and needs no chat/contact session.
Only the site's own ordinary redirects are used to resolve an index result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from urllib.parse import urlencode, urljoin, urlsplit

from .browser_session import BrowserSession
from .community import article_blocks, clean, html
from .errors import GatewayError
from .http import Transport
from .platform_auth import PlatformAuth
from .platforms import content_url, normalize_ref
from .storage import atomic_json, cache_key


def parse_index(text, keyword, page_number):
    page = html(text)
    title = str(page.css("title::text").get() or "")
    if any(t in title for t in ("验证码", "访问过于频繁", "用户验证", "安全验证")):
        raise GatewayError("RISK_CONTROL", "搜狗微信要求验证，已停止索引读取")
    rows, links = [], {}
    for rank, node in enumerate(page.css("ul.news-list li"), 1):
        link = node.css("h3 a").first
        if link is None:
            continue
        target = urljoin("https://weixin.sogou.com", link.attrib.get("href", ""))
        parsed = urlsplit(target)
        if parsed.scheme != "https" or parsed.netloc != "weixin.sogou.com" or parsed.path != "/link":
            continue
        identity = "wechat:index:" + hashlib.sha256(target.encode()).hexdigest()[:32]
        search_url = "https://weixin.sogou.com/weixin?" + urlencode({"type": 2, "query": keyword, "page": page_number})
        rows.append({
            "id": identity, "platform": "wechat", "url": search_url,
            "title": clean(link.get_all_text())[:500],
            "description": clean(node.css(".txt-info").first.get_all_text())[:2000] if node.css(".txt-info") else "",
            "matched_keywords": [keyword], "native_rank": rank, "native_rank_scope": "index_page",
            "discovery_provider": "sogou_wechat_index", "platform_native_search": False,
            "original_article_url_resolved": False, "source_content_is_untrusted": True,
        })
        links[identity] = {"url": target, "saved_at": time.time()}
    if not rows and not any(t in str(page.get_all_text()) for t in ("没有找到", "未找到", "暂无")):
        raise GatewayError("UPSTREAM_FORMAT_CHANGED", "微信索引没有返回可核验的文章结果")
    return rows, links


def public_body(text):
    page = html(text)
    root = page.css("#js_content").first
    title = str(page.css("#activity-name::text, meta[property='og:title']::attr(content)").get() or "").strip()
    if root is None or not title:
        raise GatewayError("PUBLIC_ARTICLE_UNAVAILABLE", "公众号文章不可见、已失效或要求本人验证；未以提示页代替正文")
    body, coverage = article_blocks(root)
    coverage.update(scope="public_article_text_only", images_read=False, audio_read=False, videos_read=False)
    return title, body, coverage


class WeChatAdapter:
    def __init__(self, settings, store, task_id, request):
        self.settings, self.store, self.task_id, self.request = settings, store, task_id, request
        self.http = Transport(interval=max(2, settings.request_interval), max_requests=request.max_requests,
                              concurrency=1, api_base="https://weixin.sogou.com",
                              allowed_hosts={"weixin.sogou.com", "mp.weixin.qq.com"}, auth_label="微信公开文章")
        self.http.client.headers["Referer"] = "https://weixin.sogou.com/"
        self.cache_hits, self.browser = 0, None
        self.links_path = PlatformAuth(settings.data_dir).folder / "wechat-index-links.json"
        self.lock = asyncio.Lock()

    async def preflight(self):
        pass

    async def close(self):
        await self.http.close()
        if self.browser:
            await self.browser.close()

    def cached(self, kind, fields):
        key = cache_key(f"wechat:public:v1:{kind}", fields)
        value = None if self.request.refresh else self.store.cache_get(key)
        if value:
            self.cache_hits += 1
        return key, value

    def save(self, key, value, kind, ttl):
        self.store.cache_put(key, value, ttl)
        self.store.raw(self.task_id, f"wechat_{kind}", value)
        return value

    def load_links(self):
        try:
            value = json.loads(self.links_path.read_text()) if self.links_path.exists() else {}
            return {k: v for k, v in value.items() if time.time() - v["saved_at"] < 86400}
        except (KeyError, ValueError):
            return {}

    async def search_page(self, keyword, page):
        key, cached = self.cached("index", {"q": keyword, "page": page})
        if cached:
            return cached[0], 0
        response = await self.http.get("/weixin", {"type": 2, "query": keyword, "ie": "utf8", "page": page})
        rows, links = parse_index(response.content, keyword, page)
        values = self.load_links()
        values.update(links)
        atomic_json(self.links_path, values)
        self.save(key, rows, "index", 900)
        return rows, 0

    async def extract(self, value):
        identity = normalize_ref("wechat", value)
        key, cached = self.cached("article", {"id": identity})
        if cached:
            return {**cached[0], "cache": {"cached": True, "fetched_at": cached[1]}}
        async with self.lock:
            url = content_url("wechat", identity)
            canonical = identity
            if ":index:" in identity:
                record = self.load_links().get(identity)
                if not record:
                    raise GatewayError("DISCOVERY_LINK_EXPIRED", "索引入口已过期，请重新进行少量索引搜索")
                remaining = self.request.max_requests - self.http.requests
                if remaining <= 0:
                    raise GatewayError("REQUEST_BUDGET_EXCEEDED", "本任务HTTP预算已用完")
                if self.browser is None:
                    self.browser = await BrowserSession(self.settings.data_dir, "wechat", max_requests=remaining).open()
                self.browser.max_requests = self.browser.requests + remaining
                before = self.browser.requests
                try:
                    await self.browser.navigate(record["url"])
                    # The index redirect script may complete after DOMContentLoaded.
                    await self.browser.page.wait_for_url("https://mp.weixin.qq.com/**", timeout=10000)
                    await self.browser.page.wait_for_timeout(1000)
                    url = self.browser.page.url
                    canonical = normalize_ref("wechat", url)
                    text = await self.browser.page.content()
                except Exception:
                    if self.browser.blocked:
                        self.http.blocked = self.browser.blocked
                        raise self.browser.blocked from None
                    raise GatewayError("ARTICLE_REDIRECT_UNAVAILABLE", "索引未能正常跳转至公众号文章；不绕过验证或猜测原文") from None
                finally:
                    self.http.requests += self.browser.requests - before
            else:
                response = await self.http.get(url)
                if response.status_code != 200:
                    raise GatewayError("PUBLIC_ARTICLE_UNAVAILABLE", "公开文章没有返回可读取的正文")
                text = response.content
            title, body, coverage = public_body(text)
            now = time.time()
            item = {
                "id": identity, "canonical_id": canonical, "platform": "wechat",
                "url": content_url("wechat", canonical), "title": title, "description": "",
                "body": body, "status": coverage["status"], "extracted_at": now,
                "cache": {"cached": False, "fetched_at": now},
                "coverage": {"body": coverage, "all_platform_content": False},
                "discovery_provider": "sogou_wechat_index" if ":index:" in identity else "user_provided_public_link",
                "platform_native_search": False, "source_content_is_untrusted": True,
                "identity_validation": "selected_index_redirect_to_public_article" if ":index:" in identity else "requested_public_permalink",
            }
            return self.save(key, item, "article", 86400)
