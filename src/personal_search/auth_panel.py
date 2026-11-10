"""On-demand loopback-only login selector, independent of the collection queue."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import secrets
import subprocess
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import httpx
import psutil
from filelock import FileLock

from .auth import AuthStore
from .browser_session import BrowserSession
from .errors import GatewayError, safe_error
from .platform_auth import AUTH_PLATFORMS, PlatformAuth
from .settings import Settings
from .storage import atomic_json

HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>GPTCH Search · 登录管理</title>
<style>body{font:15px/1.6 system-ui,sans-serif;background:#f6f7f9;color:#1c2430;max-width:880px;margin:44px auto;padding:0 20px}h1{font-size:25px;margin-bottom:4px}p{color:#667080}article{background:white;border:1px solid #e1e5eb;border-radius:12px;padding:18px 20px;margin:12px 0}.top{display:flex;align-items:center;justify-content:space-between;gap:12px}h2{font-size:17px;margin:0}.status{font-size:13px;color:#465569}.scope,.msg,.checked{font-size:13px;color:#667080;margin-top:6px}button{border:1px solid #d0d7e0;border-radius:7px;padding:7px 13px;margin:12px 7px 0 0;background:#fff;color:#24364a;cursor:pointer}button.primary{background:#244c74;color:white;border-color:#244c74}button:disabled{opacity:.5;cursor:wait}#note{color:#9c4027}footer{font-size:13px;color:#78818d;margin:24px 0}.badge{font-size:12px;color:#65758a}</style>
<h1>GPTCH Search 登录管理</h1><p>选择要使用的平台。无需一次登录全部平台。</p>
<p class="badge">登录在各平台的独立窗口中完成。Cookie 仅保存在本机，不会发给 AI。登录成功不保证每次搜索都能访问。</p>
<div id="note" role="status"></div><main id="rows"></main>
<button id="continue" class="primary">完成选择，继续研究</button>
<footer>公开文章与需要登录的内容分别处理。遇到验证码，请本人操作；程序不会自动绕过。微信只处理公众号公开内容，不读取私聊和通讯录。状态是最近一次观测，时间会在核验后显示。</footer>
<script>
const key=location.hash.slice(1)||sessionStorage.getItem('gptch-panel');
if(key)sessionStorage.setItem('gptch-panel',key);history.replaceState(null,'',location.pathname);
const labels={paused:'已暂缓',not_logged_in:'未登录',saved_unverified:'已保存，尚未验证',authenticated:'最近核验已登录',expired:'登录已失效',login_in_progress:'等待本人登录',challenge_required:'平台要求验证',verification_failed:'核验未完成',browser_closed:'登录窗口已关闭',cancelled:'已取消',public:'公开读取，无需登录',public_link_only:'公开链接读取；搜一搜未接通'};
async function api(path,body){const r=await fetch(path,{method:body?'POST':'GET',headers:{'X-GPTCH-Token':key||'','Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});if(!r.ok)throw Error('页面已失效或操作被拒绝，请重新打开登录列表');return r.json()}
function node(tag,txt,cls){const n=document.createElement(tag);if(txt)n.textContent=txt;if(cls)n.className=cls;return n}
async function act(platform,action){try{const v=await api('/api/action',{platform,action});document.querySelector('#note').textContent=v.error?.message||v.message||'';await refresh()}catch(e){document.querySelector('#note').textContent=e.message}}
async function refresh(){try{const v=await api('/api/status');const main=document.querySelector('#rows');main.replaceChildren();for(const p of v.platforms){const box=node('article'),top=node('div','','top');top.append(node('h2',p.display_name),node('span',labels[p.status]||p.status,'status'));box.append(top,node('div',p.scope,'scope'));if(p.message)box.append(node('div',p.message,'msg'));if(p.checked_at)box.append(node('div','最近核验：'+new Date(p.checked_at*1000).toLocaleString(),'checked'));if(p.can_login){for(const [a,t] of [['login','登录 / 重新登录'],['verify','我已完成登录，核验并保存'],['cancel','关闭登录窗口']]){const b=node('button',t,a==='login'?'primary':'');b.onclick=()=>act(p.platform,a);b.disabled=v.busy_action||(a!=='login'&&v.active_platform!==p.platform);box.append(b)}}main.append(box)}}catch(e){document.querySelector('#note').textContent=e.message}}
document.querySelector('#continue').onclick=async()=>{await api('/api/ack',{});document.querySelector('#note').textContent='选择已保存。请回到 ChatGPT / Codex 继续；需要重新登录时可再次打开此列表。'};
refresh();setInterval(refresh,3500);
</script></html>"""


class LoginController:
    def __init__(self, settings):
        self.settings, self.auth = settings, PlatformAuth(settings.data_dir)
        self.active = self.session = self.job = None
        self.created = 0
        self.last_saved = 0
        self.busy_action = False

    def status(self):
        result = self.auth.catalogue()
        if (self.active and self.session and self.session.page.is_closed()
                and self.auth.row(self.active)["status"] == "login_in_progress"):
            self.auth.mark(self.active, "browser_closed", "窗口已关闭，未自动宣告登录成功")
        return {**result, "active_platform": self.active, "busy_action": self.busy_action}

    async def close(self):
        if self.job and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
        if self.session:
            await self.session.close()
        self.active = self.session = self.job = None

    async def action(self, platform, action):
        if platform not in AUTH_PLATFORMS or action not in ("login", "verify", "cancel"):
            raise GatewayError("INVALID_AUTH_ACTION", "未识别的平台或操作")
        if self.busy_action:
            raise GatewayError("AUTH_BUSY", "请等待当前登录操作完成")
        self.busy_action = True
        try:
            if action == "login":
                if self.session and self.session.page.is_closed():
                    await self.close()
                if self.active and self.active != platform:
                    raise GatewayError("AUTH_BUSY", "请先完成或关闭当前平台的登录窗口")
                if AUTH_PLATFORMS[platform]["method"] not in ("qr", "browser"):
                    raise GatewayError("AUTH_NOT_APPLICABLE", "此平台无需或尚无可用登录入口")
                self.auth.acknowledge()
                await self.close()
                self.active, self.created = platform, time.monotonic()
                self.auth.mark(platform, "login_in_progress", "请在独立平台窗口中本人登录，再回到此页核验")
                self.job = asyncio.create_task(self.login(platform))
                return {"status": "login_in_progress"}
            if self.active != platform:
                raise GatewayError("AUTH_NOT_ACTIVE", "请先打开该平台的登录窗口")
            if action == "cancel":
                await self.close()
                return self.auth.mark(platform, "cancelled", "已关闭登录窗口；原保存会话保留")
            if not self.session:
                return {"message": "请等待平台窗口打开；B站扫码完成后列表会自动更新。"}
            status, message = await self.session.verify()
            # Save only the selected site state, never an entire Chrome profile.
            if not self.session.page.is_closed():
                self.auth.save(platform, await self.session.context.storage_state())
            result = self.auth.mark(platform, status, message, verified=True)
            if status == "authenticated":
                await self.close()
            return result
        finally:
            self.busy_action = False

    async def login(self, platform):
        try:
            if platform == "bilibili":
                with contextlib.redirect_stdout(sys.stderr):
                    await AuthStore(self.settings.data_dir).login(open_browser=True)
                self.auth.mark(platform, "authenticated", "B站扫码接口已确认登录", verified=True)
                self.active = None
            else:
                self.session = await BrowserSession(
                    self.settings.data_dir, platform, interactive=True,
                ).open()
                # Human login may involve SSO redirects. No credentials enter this process's logs.
                await self.session.page.goto(AUTH_PLATFORMS[platform]["home"],
                                             wait_until="domcontentloaded", timeout=30000)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.auth.mark(platform, "verification_failed", safe_error(exc)["message"])


async def ensure_panel(settings, *, open_browser=False):
    """Spawn a local selector if absent; no collection process or account probe is started."""
    auth = PlatformAuth(settings.data_dir)
    manifest = auth.folder / "panel.json"

    async def existing():
        try:
            data = json.loads(manifest.read_text())
            p = psutil.Process(data["pid"])
            if abs(p.create_time() - data["created"]) > .01 or not 1 <= data["port"] <= 65535:
                return None
            base = f'http://127.0.0.1:{data["port"]}'
            async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
                response = await client.get(base + "/api/status", headers={"X-GPTCH-Token": data["token"]})
            if response.status_code == 200:
                return base + "/#" + data["token"]
        except (OSError, ValueError, KeyError, psutil.Error, httpx.HTTPError):
            pass
        return None

    url = await existing()
    if not url:
        subprocess.Popen(
            [sys.executable, "-m", "personal_search.auth_panel", "--data-dir", str(settings.data_dir)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        for _ in range(40):
            await asyncio.sleep(.1)
            url = await existing()
            if url:
                break
    if not url:
        raise GatewayError("AUTH_PANEL_UNAVAILABLE", "本机登录列表未能启动，请重试打开登录管理")
    if open_browser:
        await asyncio.to_thread(webbrowser.open, url)
    return {"url": url, "expires_in_seconds_max": 7200, "local_only": True,
            "message": "在本机列表选择平台，登录完成后回到聊天继续；无需发送Cookie。"}


async def serve(settings, *, reuse_address=False):
    auth = PlatformAuth(settings.data_dir)
    try:
        lock = FileLock(str(auth.folder / "panel.lock"), timeout=0)
        lock.acquire()
    except TimeoutError:
        return
    previous = {}
    if reuse_address:
        with contextlib.suppress(OSError, ValueError):
            previous = json.loads((auth.folder / "panel.json").read_text())
    token = previous.get("token") or secrets.token_urlsafe(32)
    loop, controller = asyncio.get_running_loop(), LoginController(settings)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, code, value, html=False):
            body = value.encode() if html else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8" if html else "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def valid(self, protected=True):
            expected = f"127.0.0.1:{self.server.server_port}"
            if self.headers.get("Host") != expected:
                return False
            origin = self.headers.get("Origin")
            if origin and origin != "http://" + expected:
                return False
            return not protected or hmac.compare_digest(self.headers.get("X-GPTCH-Token", ""), token)

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.valid(path != "/"):
                return self.reply(403, {"error": "denied"})
            if path == "/":
                return self.reply(200, HTML, True)
            if path == "/api/status":
                async def get_status():
                    return controller.status()
                value = asyncio.run_coroutine_threadsafe(get_status(), loop).result(timeout=5)
                return self.reply(200, value)
            self.reply(404, {})

        def do_POST(self):
            if not self.valid():
                return self.reply(403, {"error": "denied"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2048:
                    return self.reply(413, {})
                value = json.loads(self.rfile.read(size))
                if self.path == "/api/ack":
                    auth.acknowledge()
                    return self.reply(200, {"status": "ok"})
                if self.path != "/api/action":
                    return self.reply(404, {})
                future = asyncio.run_coroutine_threadsafe(
                    controller.action(value.get("platform"), value.get("action")), loop,
                )
                result = future.result(timeout=45)
                return self.reply(200, result)
            except Exception as exc:
                return self.reply(400, {"error": safe_error(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", previous.get("port", 0)), Handler)
    server.daemon_threads = True
    atomic_json(auth.folder / "panel.json", {
        "pid": psutil.Process().pid, "created": psutil.Process().create_time(),
        "port": server.server_port, "token": token,
    })
    job = asyncio.create_task(asyncio.to_thread(server.serve_forever))
    try:
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            if controller.session and not controller.session.page.is_closed():
                if time.monotonic() - controller.last_saved > 10:
                    try:
                        # Local checkpoint also survives an accidentally closed login window.
                        # It never upgrades the status to authenticated.
                        state = await controller.session.context.storage_state()
                        auth.save(controller.active, state)
                        controller.last_saved = time.monotonic()
                    except Exception:
                        pass
            if controller.active and time.monotonic() - controller.created > 600:
                platform = controller.active
                await controller.close()
                auth.mark(platform, "cancelled", "登录窗口已等待10分钟，已释放；可重新选择")
    finally:
        await controller.close()
        server.shutdown()
        server.server_close()
        await job
        (auth.folder / "panel.json").unlink(missing_ok=True)
        lock.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--reuse-address", action="store_true", help="在受控更新时保留本机选择页地址")
    args = parser.parse_args()
    settings = Settings.load(args.data_dir)
    settings.prepare()
    asyncio.run(serve(settings, reuse_address=args.reuse_address))


if __name__ == "__main__":
    main()
