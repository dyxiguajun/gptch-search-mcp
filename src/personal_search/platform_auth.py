"""Local session catalogue. Browser state is never returned to MCP or the archive."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from filelock import FileLock

from .errors import GatewayError
from .storage import atomic_json

AUTH_PLATFORMS = {
    "bilibili": {"name": "哔哩哔哩", "method": "qr", "scope": "公开搜索、视频与已开放组件"},
    "juejin": {"name": "掘金", "method": "public", "scope": "公开文章与原生搜索，无需登录"},
    "segmentfault": {"name": "SegmentFault 思否", "method": "public", "scope": "公开文章与原生搜索，无需登录"},
    "xiaohongshu": {
        "name": "小红书", "method": "browser", "home": "https://www.xiaohongshu.com/explore",
        "domains": ["xiaohongshu.com"], "scope": "网页可见笔记及原生搜索；不发布、不私信",
    },
    "zhihu": {
        "name": "知乎", "method": "browser", "home": "https://www.zhihu.com/signin",
        "domains": ["zhihu.com"], "scope": "网页可见回答、文章与原生搜索；不读私信",
    },
    "tieba": {
        "name": "百度贴吧", "method": "browser", "home": "https://tieba.baidu.com/",
        "domains": ["baidu.com"], "scope": "公开帖子和原生搜索；百度统一登录会话仅用于贴吧",
    },
    "wechat": {
        "name": "微信公众文章", "method": "public_link",
        "domains": ["mp.weixin.qq.com", "weixin.sogou.com"],
        "scope": "公众号公开文章与搜狗微信索引；客户端搜一搜未接通，不读取私聊/通讯录",
    },
}

STATUSES = {
    "not_logged_in", "saved_unverified", "login_in_progress", "authenticated", "expired",
    "challenge_required", "verification_failed", "browser_closed", "cancelled",
    "public", "public_link_only", "paused",
}


def domain_matches(host, domain):
    return host == domain or host.endswith("." + domain)


class PlatformAuth:
    def __init__(self, root: Path):
        self.root = root
        self.folder = root / "auth"
        self.folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.folder.chmod(0o700)
        self.path = self.folder / "platform-status.json"
        self.lock = FileLock(str(self.folder / "platform-status.lock"))

    def state_path(self, platform):
        if platform not in AUTH_PLATFORMS or AUTH_PLATFORMS[platform]["method"] != "browser":
            raise GatewayError("AUTH_NOT_APPLICABLE", "此平台不使用浏览器会话")
        return self.folder / f"{platform}-browser.json"

    def _read(self):
        try:
            return json.loads(self.path.read_text()) if self.path.exists() else {}
        except (ValueError, OSError):
            return {}

    def mark(self, platform, status, message="", *, verified=False):
        if platform not in AUTH_PLATFORMS or status not in STATUSES:
            raise GatewayError("INVALID_AUTH_ACTION", "无效的登录状态")
        with self.lock:
            states = self._read()
            previous = states.get(platform, {})
            states[platform] = {"status": status, "message": message, "updated_at": time.time()}
            if verified:
                states[platform]["checked_at"] = time.time()
            elif previous.get("checked_at"):
                states[platform]["checked_at"] = previous["checked_at"]
            atomic_json(self.path, states)
        return self.row(platform)

    def row(self, platform):
        spec = AUTH_PLATFORMS[platform]
        if spec["method"] in ("public", "public_link"):
            state = {"status": "public" if spec["method"] == "public" else "public_link_only"}
            observed = self._read().get(platform, {})
            if observed:
                state.update(access_status=observed.get("status"), message=observed.get("message"),
                             updated_at=observed.get("updated_at"))
        else:
            state = self._read().get(platform, {})
            if not state:
                path = self.folder / "bilibili.json" if platform == "bilibili" else self.state_path(platform)
                state = {"status": "saved_unverified" if path.exists() else "not_logged_in"}
        return {
            "platform": platform, "display_name": spec["name"], "scope": spec["scope"],
            "login_method": spec["method"], "can_login": spec["method"] in ("browser", "qr"),
            **{k: state[k] for k in ("status", "access_status", "message", "updated_at", "checked_at") if k in state},
            "status_is_last_observation": True,
        }

    def catalogue(self):
        return {"platforms": [self.row(p) for p in AUTH_PLATFORMS],
                "credentials_returned": False, "cookies_do_not_guarantee_access": True,
                "first_use": not (self.folder / "selection-seen.json").exists()}

    def acknowledge(self):
        atomic_json(self.folder / "selection-seen.json", {"seen_at": time.time()})

    def load(self, platform):
        path = self.state_path(platform)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            return self.filter_state(platform, value)
        except (ValueError, TypeError, KeyError):
            raise GatewayError("AUTH_FILE_INVALID", "本地会话损坏，请在登录列表中重新登录") from None

    def filter_state(self, platform, value):
        from urllib.parse import urlsplit

        domains = AUTH_PLATFORMS[platform]["domains"]
        return {
            "cookies": [c for c in value.get("cookies", [])
                        if any(domain_matches(c["domain"].lstrip("."), d) for d in domains)],
            "origins": [o for o in value.get("origins", [])
                        if urlsplit(o["origin"]).scheme == "https" and any(
                            domain_matches(urlsplit(o["origin"]).hostname or "", d) for d in domains)],
        }

    def save(self, platform, value):
        atomic_json(self.state_path(platform), self.filter_state(platform, value))
