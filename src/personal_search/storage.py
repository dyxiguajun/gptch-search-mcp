from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .errors import GatewayError

SECRET_KEYS = {
    "sessdata",
    "cookie",
    "cookies",
    "bili_jct",
    "refresh_token",
    "access_token",
    "qrcode_key",
    "authorization",
    "set-cookie",
    "subtitle_url",
    "subtitle_url_v2",
}


def scrub(value):
    """Never archive credentials or signed subtitle download URLs."""
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k.lower() not in SECRET_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def atomic_json(path: Path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(path)


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "archive.db"
        root.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, request TEXT NOT NULL,
                    progress TEXT NOT NULL DEFAULT '{}', error TEXT, created REAL NOT NULL,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidates (
                    task_id TEXT NOT NULL, content_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (task_id, content_id)
                );
                CREATE TABLE IF NOT EXISTS items (
                    task_id TEXT NOT NULL, content_id TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (task_id, content_id)
                );
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL,
                    fetched REAL NOT NULL
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
                    content_id UNINDEXED, task_id UNINDEXED, title, text,
                    tokenize='unicode61'
                );
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_task(self, request: dict):
        task_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tasks(id,status,request,created,updated) VALUES (?,?,?,?,?)",
                (task_id, "queued", json.dumps(request, ensure_ascii=False), now, now),
            )
        return self.task(task_id)

    def task(self, task_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise GatewayError("TASK_NOT_FOUND", "没有找到此任务")
            result = dict(row)
            for name in ("request", "progress", "error"):
                result[name] = json.loads(result[name]) if result[name] else None
            result["candidate_count"] = db.execute(
                "SELECT count(*) FROM candidates WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            result["item_count"] = db.execute(
                "SELECT count(*) FROM items WHERE task_id=?", (task_id,)
            ).fetchone()[0]
        return result

    def update(self, task_id, *, status=None, progress=None, error=None, clear_error=False):
        assignments, params = ["updated=?"], [time.time()]
        for key, value in (("status", status), ("progress", progress), ("error", error)):
            if value is not None:
                assignments.append(key + "=?")
                params.append(json.dumps(value, ensure_ascii=False) if key != "status" else value)
        if clear_error:
            assignments.append("error=NULL")
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET " + ",".join(assignments) + " WHERE id=?", [*params, task_id]
            )

    def recover(self):
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET status='interrupted', error=?, updated=? WHERE status='running'",
                (
                    json.dumps(
                        {
                            "code": "WORKER_INTERRUPTED",
                            "message": "服务中断；已保存的数据可读取，任务可恢复。",
                        },
                        ensure_ascii=False,
                    ),
                    time.time(),
                ),
            )

    def queued(self):
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute("SELECT id FROM tasks WHERE status='queued' ORDER BY created")
            ]

    def save_candidate(self, task_id, item):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO candidates VALUES (?,?,?)",
                (task_id, item["id"], json.dumps(scrub(item), ensure_ascii=False)),
            )

    def candidates(self, task_id, offset=0, limit=20):
        with self.connect() as db:
            rows = db.execute(
                "SELECT payload FROM candidates WHERE task_id=? ORDER BY rowid LIMIT ? OFFSET ?",
                (task_id, limit, offset),
            )
            return [json.loads(row[0]) for row in rows]

    def save_item(self, task_id, item):
        item = scrub(item)
        payload = json.dumps(item, ensure_ascii=False)
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO items VALUES (?,?,?)", (task_id, item["id"], payload)
            )
            db.execute(
                "DELETE FROM archive_fts WHERE content_id=? AND task_id=?", (item["id"], task_id)
            )
            db.execute(
                "INSERT INTO archive_fts VALUES (?,?,?,?)",
                (item["id"], task_id, item.get("title", ""), payload),
            )
        folder = self.root / "searches" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        # Transaction commits first; SQLite remains authoritative after interrupted export.
        with (folder / "items.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(payload + "\n")

    def items(self, task_id, offset=0, limit=10, content_id=None):
        with self.connect() as db:
            if content_id:
                rows = db.execute(
                    "SELECT payload FROM items WHERE task_id=? AND content_id=?",
                    (task_id, content_id),
                )
            else:
                rows = db.execute(
                    "SELECT payload FROM items WHERE task_id=? ORDER BY rowid LIMIT ? OFFSET ?",
                    (task_id, limit, offset),
                )
            return [json.loads(row[0]) for row in rows]

    def cache_get(self, key):
        with self.connect() as db:
            row = db.execute(
                "SELECT payload,fetched FROM cache WHERE key=? AND expires>?", (key, time.time())
            ).fetchone()
        return (json.loads(row[0]), row[1]) if row else None

    def cache_put(self, key, value, ttl):
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cache VALUES (?,?,?,?)",
                (key, json.dumps(scrub(value), ensure_ascii=False), now + ttl, now),
            )

    def raw(self, task_id, endpoint, payload):
        folder = self.root / "raw" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-zA-Z0-9_-]", "_", endpoint)[-70:]
        path = folder / f"{time.time_ns()}_{name}.json"
        atomic_json(path, scrub(payload))

    def archive_search(self, query, limit=10):
        tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)[:20]
        if not tokens:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        with self.connect() as db:
            rows = db.execute(
                "SELECT content_id,task_id,title,snippet(archive_fts,3,'[',']','…',30) AS snippet FROM archive_fts WHERE archive_fts MATCH ? ORDER BY bm25(archive_fts) LIMIT ?",
                (expression, limit),
            )
            return [dict(row) for row in rows]

    def backup(self):
        folder = self.root / "backups"
        folder.mkdir(exist_ok=True)
        destination = folder / f"archive-{time.time_ns()}.db"
        with self.connect() as db, sqlite3.connect(destination) as target:
            db.backup(target)
        return destination


def cache_key(kind: str, values: dict):
    return kind + ":" + hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
