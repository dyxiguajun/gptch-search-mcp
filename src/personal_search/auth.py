from __future__ import annotations

import asyncio
import json
import time
import webbrowser
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import qrcode

from .errors import GatewayError
from .http import Transport
from .storage import atomic_json

COOKIE_NAMES = {"SESSDATA", "bili_jct", "buvid3", "buvid4", "DedeUserID"}


class AuthStore:
    def __init__(self, root: Path):
        self.folder = root / "auth"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "bilibili.json"

    def load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return {k: str(v) for k, v in data.items() if k in COOKIE_NAMES and v}
        except (ValueError, AttributeError):
            raise GatewayError("AUTH_FILE_INVALID", "本地登录文件损坏；请重新登录") from None

    def save(self, cookies):
        atomic_json(self.path, {k: v for k, v in cookies.items() if k in COOKIE_NAMES and v})

    def import_cc(self, path: Path):
        # Explicit local CLI action; never a model-facing Cookie argument.
        cookie = SimpleCookie()
        cookie.load(path.read_text(encoding="utf-8").strip())
        values = {name: item.value for name, item in cookie.items() if name in COOKIE_NAMES}
        if not values.get("SESSDATA"):
            raise GatewayError("AUTH_FILE_INVALID", "该文件没有可识别的 B 站会话")
        self.save(values)

    async def status(self):
        cookies = self.load()
        if not cookies.get("SESSDATA"):
            return {
                "platform": "bilibili",
                "status": "anonymous",
                "message": "可尝试公开搜索；字幕及部分评论可能需要登录。",
            }
        transport = Transport(cookies, max_requests=5, concurrency=1)
        try:
            nav = await transport.json("/x/web-interface/nav", authenticated=True)
            if not nav.get("isLogin"):
                raise GatewayError("AUTH_EXPIRED", "B 站会话已过期，请重新扫码登录")
            return {"platform": "bilibili", "status": "authenticated"}
        finally:
            await transport.close()

    async def login(self, *, open_browser=True):
        transport = Transport(max_requests=110, concurrency=1)
        try:
            data = await transport.json(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
            )
            key, link = data.get("qrcode_key"), data.get("url")
            if not key or not link:
                raise GatewayError("UPSTREAM_FORMAT_CHANGED", "登录接口没有返回二维码")
            png = self.folder / "login-qr.png"
            qrcode.make(link).save(png)
            html = self.folder / "login.html"
            html.write_text(
                '<!doctype html><meta charset="utf-8"><title>GPTCH B站登录</title><h1>请使用哔哩哔哩 App 扫码</h1><p>凭证仅保存在本机。二维码约三分钟后失效。</p><img src="login-qr.png" width="320">',
                encoding="utf-8",
            )
            print(f"使用 B站 App 扫码并确认：{html}\n二维码：{png}", flush=True)
            if open_browser:
                webbrowser.open(html.as_uri())
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                poll = await transport.json(
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                    {"qrcode_key": key},
                )
                code = poll.get("code")
                if code == 0:
                    values = {
                        name: morsel[0]
                        for name, morsel in parse_qs(urlsplit(poll.get("url", "")).query).items()
                        if name in COOKIE_NAMES
                    }
                    for cookie in transport.client.cookies.jar:
                        if cookie.name in COOKIE_NAMES:
                            values[cookie.name] = cookie.value
                    if not values.get("SESSDATA"):
                        raise GatewayError("AUTH_FAILED", "扫码完成但未获取会话，请重新登录")
                    self.save(values)
                    return {"status": "authenticated", "platform": "bilibili"}
                if code == 86038:
                    break
                if code not in (86101, 86090):
                    raise GatewayError("AUTH_FAILED", "二维码登录返回未知状态", api_code=code)
                await asyncio.sleep(2)
            raise GatewayError("QR_EXPIRED", "二维码已过期，请重新运行登录命令")
        finally:
            await transport.close()
            for filename in ("login-qr.png", "login.html"):
                (self.folder / filename).unlink(missing_ok=True)
