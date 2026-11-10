"""Scrapling-managed Chrome for user login and read-only, bounded site navigation.

No custom request signatures, CAPTCHA solving, personal Chrome profile or account
payload recording. The selected platform alone is restored.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from filelock import FileLock, Timeout
from scrapling.engines.constants import EXTRA_RESOURCES

from .browser_cache import StaticCache
from .errors import GatewayError
from .platform_auth import AUTH_PLATFORMS, PlatformAuth, domain_matches

RESOURCE_DOMAINS = {
    "xiaohongshu": ["xiaohongshu.com", "xhscdn.com"],
    "zhihu": ["zhihu.com", "zhimg.com"],
    "tieba": ["baidu.com", "bdstatic.com", "bcebos.com"],
    "wechat": ["mp.weixin.qq.com", "weixin.sogou.com", "sogoucdn.com", "sogou.com", "wx.gtimg.com", "wx.qlogo.cn"],
}


def account_verified(platform, value):
    if not isinstance(value, dict):
        return False
    if platform == "zhihu":
        return bool(value.get("id") or value.get("uid")) and bool(value.get("name"))
    if platform == "tieba":
        data = value.get("data")
        if not isinstance(data, dict) or value.get("no") not in (0, "0"):
            return False
        # Live json_userinfo uses data.is_login and user_itieba_id, not user_id.
        # The explicit logged-in flag is sufficient; no account identifier is retained.
        return data.get("is_login") in (1, True, "1")
    return False


class BrowserSession:
    def __init__(self, root, platform, *, interactive=False, max_requests=120, interval=2):
        self.auth, self.platform = PlatformAuth(root), platform
        self.interactive, self.max_requests, self.interval = interactive, max_requests, interval
        self.requests, self.blocked = 0, None
        self.gate = SimpleNamespace(limit=1)
        self.context = self.browser = self.driver = self.page = self.engine = None
        self.last_navigation = 0
        self.guard = asyncio.Lock()
        self.events = []
        self.blocked_posts = []
        self.static_cache = StaticCache(root)
        self.static_cache_hits = 0
        self.lease = FileLock(str(self.auth.folder / "browser.lock"), timeout=0)
        self.leased = False

    async def open(self):
        from scrapling.fetchers import AsyncStealthySession

        try:
            self.lease.acquire()
            self.leased = True
        except Timeout:
            raise GatewayError("BROWSER_BUSY", "另一个平台窗口正在使用浏览器；请先完成或关闭它") from None
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        try:
            self.engine = AsyncStealthySession(
                headless=not self.interactive, real_chrome=True,
                **({"executable_path": str(chrome)} if chrome.exists() else {}),
                max_pages=1, retries=1, solve_cloudflare=False, google_search=False,
                record_requests=False, hide_canvas=False, dns_over_https=False,
                additional_args={"accept_downloads": False, "permissions": [],
                                 "ignore_https_errors": False, "service_workers": "block",
                                 "viewport": {"width": 1280, "height": 900},
                                 "color_scheme": "light", "device_scale_factor": 1},
            )
            await self.engine.start()
            self.context = self.engine.context
            if self.platform != "wechat":
                state = self.auth.load(self.platform)
                if state:
                    await self.context.set_storage_state(state)
            self.context.set_default_timeout(10000)
            if not self.interactive:
                await self.context.route("**/*", self.route)
                self.context.on("response", self.response)
            self.page = await self.context.new_page()
            return self
        except Exception:
            await self.close()
            raise GatewayError("BROWSER_START_FAILED", "无法打开项目独立Chrome，请确认已安装Chrome") from None

    async def route(self, route):
        from scrapling.engines.toolbelt.ad_domains import AD_DOMAINS

        request = route.request
        url = urlsplit(request.url)
        allowed = any(domain_matches(url.hostname or "", d) for d in RESOURCE_DOMAINS[self.platform])
        # Normal native search POST is read-only; other background mutation/telemetry is blocked.
        read_post = (self.platform == "xiaohongshu" and url.path in (
            "/api/sns/web/v1/search/notes", "/api/sns/web/v2/search/notes",
            "/api/sns/web/v1/search/onebox", "/api/sns/web/v1/config", "/api/sns/web/v1/feed",
            "/api/redcaptcha/v2/getconfig", "/api/sec/v1/scripting", "/api/sec/v1/sbtsource",
        )) or (self.platform == "tieba" and url.path in ("/c/f/pb/page_pc",))
        if (allowed and request.method == "POST" and not read_post
                and len(self.blocked_posts) < 20):
            self.blocked_posts.append(re.sub(r"/[a-f0-9]{16,}(?=/|$)", "/{id}", url.path))
        if (not allowed or url.scheme != "https" or url.port not in (None, 443)
                or request.resource_type in EXTRA_RESOURCES - {"stylesheet"}
                or any(domain_matches(url.hostname or "", d) for d in AD_DOMAINS)
                or (request.method not in ("GET", "HEAD", "OPTIONS") and not read_post)
                or self.blocked):
            return await route.abort()
        static = self.static_cache.eligible(request)
        cached = self.static_cache.get(request.url) if static else None
        if cached:
            self.static_cache_hits += 1
            return await route.fulfill(status=200, body=cached[0], headers={
                "content-type": cached[1], "access-control-allow-origin": "*",
            })
        if self.requests >= self.max_requests:
            self.blocked = GatewayError("REQUEST_BUDGET_EXCEEDED", "浏览器HTTP请求预算已用完")
            return await route.abort()
        self.requests += 1
        if static:
            try:
                response = await route.fetch(max_redirects=0, max_retries=0, timeout=20000)
                size = int(response.headers.get("content-length", "0") or "0")
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if (response.status == 200 and size <= self.static_cache.entry_bytes
                        and mime in ("application/javascript", "text/javascript", "application/x-javascript", "text/css")):
                    body = await response.body()
                    self.static_cache.put(request.url, body, response.headers["content-type"])
                return await route.fulfill(response=response)
            except Exception:
                return await route.abort()
        await route.continue_()

    async def response(self, response):
        if self.blocked:
            return
        if response.request.resource_type not in ("document", "xhr", "fetch"):
            return
        url = urlsplit(response.url)
        if not any(domain_matches(url.hostname or "", d) for d in AUTH_PLATFORMS[self.platform]["domains"]):
            return
        if (response.request.resource_type in ("xhr", "fetch") and len(self.events) < 20
                and url.path in ("/api/sns/web/v1/search/notes", "/api/sns/web/v2/search/notes", "/api/v4/search_v3", "/f/search/res")):
            event = {"host": url.hostname, "path": url.path, "status": response.status}
            if self.platform == "xiaohongshu" and url.path in ("/api/sns/web/v1/search/notes", "/api/sns/web/v2/search/notes"):
                try:
                    value = await response.json()
                    if isinstance(value, dict):
                        event.update(api_code=value.get("code"), success=value.get("success"))
                        data = value.get("data")
                        if isinstance(data, dict) and isinstance(data.get("items"), list):
                            event["items_count"] = len(data["items"])
                        if value.get("success") is False and value.get("code") not in (None, 0):
                            self.blocked = GatewayError("UPSTREAM_REJECTED", "平台拒绝原生搜索；停止本次任务，不猜测空结果", api_code=value.get("code"))
                except Exception:
                    pass
            self.events.append(event)
        if response.status in (403, 412, 429):
            self.blocked = GatewayError("RISK_CONTROL", "平台要求验证或限制请求；已停止，不自动重试")
            self.auth.mark(self.platform, "challenge_required", "平台要求验证；可在登录页本人处理")
        elif response.status == 401:
            self.blocked = GatewayError("AUTH_EXPIRED", "平台会话已失效，请从登录列表重新登录")
            self.auth.mark(self.platform, "expired", "平台返回401，需要重新登录")

    async def navigate(self, url):
        host = urlsplit(url).hostname or ""
        if not any(domain_matches(host, d) for d in AUTH_PLATFORMS[self.platform]["domains"]):
            raise GatewayError("UNSAFE_URL", "不允许打开此平台范围以外的地址")
        if self.blocked:
            raise self.blocked
        await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last_navigation)))
        self.last_navigation = time.monotonic()
        try:
            response = await self.page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await self.page.wait_for_timeout(1800)
        except Exception:
            if self.blocked:
                raise self.blocked from None
            raise GatewayError("BROWSER_NAVIGATION_FAILED", "平台页面未完成加载，停止本次读取") from None
        if self.blocked:
            raise self.blocked
        title = await self.page.title()
        if (any(t in title for t in ("安全验证", "访问验证", "人机验证", "验证中心"))
                or "/antispider/" in urlsplit(self.page.url).path
                or await self.page.locator('form[action*="antispider"], input[name="seccode"]').count()):
            self.auth.mark(self.platform, "challenge_required", "需要本人完成平台验证")
            self.blocked = GatewayError("RISK_CONTROL", "平台要求本人验证，已停止")
            raise self.blocked
        return response

    async def verify(self):
        """Return only a boolean/status; account payloads are neither saved nor returned."""
        if self.page.is_closed():
            return "browser_closed", "登录窗口已关闭，尚未完成验证"
        host = urlsplit(self.page.url).hostname or ""
        domains = AUTH_PLATFORMS[self.platform]["domains"]
        if not any(domain_matches(host, d) for d in domains):
            return "verification_failed", "请回到所选平台页面再确认"
        title = await self.page.title()
        if any(t in title for t in ("安全验证", "访问验证", "人机验证", "验证中心")):
            return "challenge_required", "请在平台窗口中完成验证后重试确认"
        if self.platform == "xiaohongshu":
            # The site's own sidebar exposes '我' only after a logged-in page load.
            node = self.page.locator('a[href*="/user/profile/"]').filter(has_text="我")
            ok = await node.count() > 0 and await node.first.is_visible()
            return ("authenticated", "已确认当前页面的本人入口") if ok else (
                "saved_unverified", "尚未找到已登录的本人入口；请完成登录后再次确认")
        endpoint = "https://www.zhihu.com/api/v4/me" if self.platform == "zhihu" else (
            "https://tieba.baidu.com/f/user/json_userinfo")
        try:
            response = await self.context.request.get(endpoint, timeout=15000)
            if response.status in (403, 412, 429):
                return "challenge_required", "登录核验被平台限制，不能据此确认登录"
            if response.status == 401:
                return "expired", "平台确认会话无效"
            if response.status != 200:
                return "verification_failed", "平台没有返回可验证的登录状态"
            value = await response.json()
            ok = account_verified(self.platform, value)
            return ("authenticated", "平台账户接口已确认登录") if ok else (
                "saved_unverified", "未取得明确的已登录状态；不会仅凭Cookie宣告成功")
        except Exception:
            return "verification_failed", "登录核验未完成，已保存会话仍需重新核验"

    async def close(self):
        try:
            if self.engine:
                with contextlib.suppress(Exception):
                    await self.engine.close()
                self.engine = None
            if self.browser:
                with contextlib.suppress(Exception):
                    await self.browser.close()
                self.browser = None
            if self.driver:
                with contextlib.suppress(Exception):
                    await self.driver.stop()
                self.driver = None
        finally:
            if self.leased:
                self.lease.release()
                self.leased = False
