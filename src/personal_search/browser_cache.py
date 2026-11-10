"""Bounded cache of public CDN JS/CSS; never API pages, cookies or account data."""

import hashlib
import json
import os
import time
import uuid
from urllib.parse import urlsplit

from .platform_auth import domain_matches
from .storage import atomic_json


class StaticCache:
    max_bytes = 32 * 1024 * 1024
    entry_bytes = 2 * 1024 * 1024
    ttl = 86400
    cdns = ("xhscdn.com", "zhimg.com", "bdstatic.com")

    def __init__(self, root):
        self.folder = root / "browser-static-cache"
        self.folder.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.folder.chmod(0o700)

    def eligible(self, request):
        url = urlsplit(request.url)
        return (request.method == "GET" and request.resource_type in ("script", "stylesheet")
                and url.scheme == "https" and url.port in (None, 443)
                and any(domain_matches(url.hostname or "", d) for d in self.cdns)
                and url.path.endswith((".js", ".css")))

    def paths(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()
        return self.folder / (key + ".json"), self.folder / (key + ".blob")

    def get(self, url):
        meta, blob = self.paths(url)
        try:
            value = json.loads(meta.read_text())
            if time.time() - value["saved_at"] < self.ttl and blob.stat().st_size <= self.entry_bytes:
                body = blob.read_bytes()
                if hashlib.sha256(body).hexdigest() == value["sha256"]:
                    return body, value["content_type"]
        except (OSError, ValueError, KeyError):
            pass
        return None

    def put(self, url, body, content_type):
        if not body or len(body) > self.entry_bytes:
            return
        files = sorted(self.folder.glob("*.blob"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if total + len(body) <= self.max_bytes:
                break
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
            path.with_suffix(".json").unlink(missing_ok=True)
        meta, blob = self.paths(url)
        temporary = blob.with_suffix("." + uuid.uuid4().hex + ".tmp")
        temporary.write_bytes(body)
        if os.name != "nt":
            temporary.chmod(0o600)
        temporary.replace(blob)
        atomic_json(meta, {"saved_at": time.time(), "content_type": content_type,
                           "sha256": hashlib.sha256(body).hexdigest()})
