from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

import httpx

from .errors import GatewayError

API = "https://api.bilibili.com"
ALLOWED_HOSTS = {
    "api.bilibili.com",
    "www.bilibili.com",
    "passport.bilibili.com",
    "comment.bilibili.com",
}
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}


def validate_url(url: str, *, allowed_hosts=None, allowed_suffixes=None):
    parts = urlsplit(url)
    host = parts.hostname or ""
    suffixes = (
        ((".hdslb.com",) if allowed_hosts is None else ())
        if allowed_suffixes is None
        else allowed_suffixes
    )
    hosts = ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port not in (None, 443)
        or not (host in hosts or any(host.endswith(suffix) for suffix in suffixes))
    ):
        raise GatewayError("URL_NOT_ALLOWED", "仅允许平台接口与平台字幕 CDN")


class AdaptiveGate:
    def __init__(self, maximum):
        self.maximum = maximum
        self.limit = maximum
        self.active = 0
        self.successes = 0
        self.condition = asyncio.Condition()

    async def enter(self):
        async with self.condition:
            await self.condition.wait_for(lambda: self.active < self.limit)
            self.active += 1

    async def leave(self, throttled=False, success=False):
        async with self.condition:
            self.active -= 1
            if throttled:
                self.limit = max(1, self.limit // 2)
                self.successes = 0
            elif success:
                self.successes += 1
                if self.successes >= 8:
                    self.limit = min(self.maximum, self.limit + 1)
                    self.successes = 0
            self.condition.notify_all()


class Transport:
    """One shared limiter and request budget, including retries and WBI key requests."""

    def __init__(
        self,
        cookies=None,
        *,
        interval=0.8,
        max_requests=120,
        concurrency=2,
        client=None,
        api_base=API,
        allowed_hosts=None,
        auth_label="B 站",
    ):
        self.client = client or httpx.AsyncClient(
            headers=HEADERS, timeout=20, follow_redirects=False, trust_env=True
        )
        for name, value in (cookies or {}).items():
            self.client.cookies.set(name, value, domain=".bilibili.com", path="/")
        self.interval = interval
        self.max_requests = max_requests
        self.requests = 0
        self.gate = AdaptiveGate(concurrency)
        self.pacing = asyncio.Lock()
        self.last_request = 0.0
        self.blocked: GatewayError | None = None
        self.api_base, self.allowed_hosts, self.auth_label = api_base, allowed_hosts, auth_label

    async def close(self):
        await self.client.aclose()

    async def get(self, url, params=None, *, headers=None, method="GET", json_body=None):
        if url.startswith("/"):
            url = self.api_base + url
        validate_url(url, allowed_hosts=self.allowed_hosts)
        for attempt in range(3):
            await self.gate.enter()
            throttled, success = False, False
            try:
                async with self.pacing:
                    if self.blocked:
                        raise self.blocked
                    if self.requests >= self.max_requests:
                        raise GatewayError("REQUEST_BUDGET_EXCEEDED", "已达到本任务请求预算")
                    await asyncio.sleep(
                        max(0, self.interval - (time.monotonic() - self.last_request))
                    )
                    if self.blocked:
                        raise self.blocked
                    self.last_request = time.monotonic()
                    self.requests += 1
                # Bound decompressed responses as well as Content-Length.
                async with self.client.stream(
                    method, url, params=params, headers=headers, json=json_body
                ) as response:
                    chunks, size = [], 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > 5_000_000:
                            raise GatewayError("RESPONSE_TOO_LARGE", "响应超过 5MB 单请求上限")
                        chunks.append(chunk)
                    decoded_headers = dict(response.headers)
                    decoded_headers.pop("content-encoding", None)
                    decoded_headers.pop("content-length", None)
                    result = httpx.Response(
                        response.status_code,
                        content=b"".join(chunks),
                        headers=decoded_headers,
                        request=response.request,
                    )
                if result.status_code in (403, 412, 429):
                    throttled = True
                    code = "RATE_LIMITED" if result.status_code == 429 else "RISK_CONTROL"
                    self.blocked = GatewayError(
                        code,
                        "平台限流或风控；停止本次采集，稍后重试。",
                        http_status=result.status_code,
                    )
                    raise self.blocked
                if result.status_code >= 500:
                    if attempt == 2:
                        raise GatewayError(
                            "UPSTREAM_UNAVAILABLE",
                            "平台服务暂时不可用",
                            http_status=result.status_code,
                        )
                elif result.status_code == 401:
                    raise GatewayError("AUTH_EXPIRED", f"{self.auth_label}登录已失效，需要本人登录")
                elif result.status_code >= 300:
                    raise GatewayError(
                        "UPSTREAM_HTTP_ERROR", "平台未返回可用内容", http_status=result.status_code
                    )
                else:
                    success = True
                    return result
            except httpx.TimeoutException:
                if attempt == 2:
                    raise GatewayError("NETWORK_TIMEOUT", "平台请求超时") from None
            except httpx.RequestError:
                if attempt == 2:
                    raise GatewayError(
                        "NETWORK_ERROR", "网络连接失败，请检查本机代理和网络"
                    ) from None
            finally:
                await self.gate.leave(throttled=throttled, success=success)
            await asyncio.sleep(2**attempt)

    async def json(self, path, params=None, *, authenticated=False, headers=None):
        response = await self.get(path, params, headers=headers)
        try:
            payload = response.json()
            code = payload.get("code")
        except (ValueError, AttributeError):
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "平台没有返回预期的 JSON") from None
        if code in (-101, -111):
            raise GatewayError(
                "AUTH_EXPIRED" if authenticated else "AUTH_REQUIRED", "此接口需要有效的 B 站登录"
            )
        if code in (-352, -412, -509):
            self.blocked = GatewayError(
                "RISK_CONTROL", "平台风控；停止本次采集，稍后重试。", api_code=code
            )
            raise self.blocked
        if code == -403:
            raise GatewayError("PERMISSION_DENIED", "平台拒绝读取此内容", api_code=code)
        if code in (-404, 62002):
            raise GatewayError("CONTENT_NOT_AVAILABLE", "内容已删除或不可访问", api_code=code)
        if code != 0:
            raise GatewayError("UPSTREAM_API_ERROR", "平台接口返回错误", api_code=code)
        data = payload.get("data")
        if not isinstance(data, dict):
            raise GatewayError("UPSTREAM_FORMAT_CHANGED", "平台响应缺少 data 对象")
        return data
