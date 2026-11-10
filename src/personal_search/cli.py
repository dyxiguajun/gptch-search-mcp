from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import platform
import sys
from pathlib import Path
from typing import get_args

import psutil
from pydantic import ValidationError

from .auth import AuthStore
from .capabilities import capabilities
from .errors import GatewayError, safe_error
from .manager import TaskManager
from .models import TERMINAL, FetchSection, SearchRequest
from .results import fetch_saved, task_result
from .settings import Settings
from .storage import Store, atomic_json


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def run_search(settings, request):
    async with TaskManager(settings) as manager:
        task = manager.submit(request)
        print(f"task_id={task['id']}", file=sys.stderr, flush=True)
        while True:
            task = manager.store.task(task["id"])
            if task["status"] in TERMINAL:
                result = task_result(manager.store, task["id"], view="items", limit=20)
                if not task["item_count"]:
                    result["candidates"] = manager.store.candidates(task["id"], limit=20)
                return result
            await asyncio.sleep(0.5)


def parser():
    root = argparse.ArgumentParser(description="GPTCH 本机只读搜索网关")
    root.add_argument("--data-dir", help="资料与会话目录；也可设置 GPTCH_DATA_DIR")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="通过 stdio 启动 MCP；stdout 仅用于协议")
    commands.add_parser("doctor", help="本地状态，不读取或打印凭证")
    search = commands.add_parser("search", help="执行搜索并等待结果；Ctrl+C 保留断点")
    search.add_argument("query")
    search.add_argument("--platform", choices=list(capabilities()["platforms"]), default="bilibili")
    search.add_argument("--keyword", action="append", default=[])
    search.add_argument("--depth", choices=["quick", "standard", "deep"], default="standard")
    search.add_argument("--pages", type=int)
    search.add_argument("--max-items", type=int)
    search.add_argument("--comment-pages", type=int)
    search.add_argument("--max-comments", type=int)
    search.add_argument("--comment-sort", action="append", choices=["hot", "latest"])
    search.add_argument("--no-comments", action="store_true")
    search.add_argument("--max-danmaku", type=int, default=500)
    search.add_argument("--subtitle-tracks", type=int, default=2)
    search.add_argument("--reply-pages", type=int, default=1)
    search.add_argument("--max-requests", type=int, default=120)
    search.add_argument("--timeout", type=int, default=300)
    search.add_argument(
        "--content",
        action="append",
        default=[],
        help="直接提取指定平台内容标识/标准链接，可重复；需与--platform匹配",
    )
    search.add_argument("--refresh", action="store_true")
    auth = commands.add_parser("auth").add_subparsers(dest="auth_action", required=True)
    login = auth.add_parser("login", help="B站 App 二维码登录；仅本机保存会话")
    login.add_argument("--no-browser", action="store_true")
    auth.add_parser("status")
    auth.add_parser("panel", help="打开所有平台的本机登录选择与状态页")
    imp = auth.add_parser("import-cc", help="从指定 CC Extractor Cookie 文件导入；不会修改原工程")
    imp.add_argument("path", type=Path)
    task = commands.add_parser("task")
    task.add_argument("task_id")
    task.add_argument("--view", choices=["summary", "items", "candidates"], default="summary")
    task.add_argument("--offset", type=int, default=0)
    task.add_argument("--limit", type=int, default=10)
    fetch = commands.add_parser("fetch")
    fetch.add_argument("task_id")
    fetch.add_argument("content_id")
    fetch.add_argument(
        "--section",
        choices=list(get_args(FetchSection)),
        default="metadata",
    )
    fetch.add_argument("--offset", type=int, default=0)
    fetch.add_argument("--limit", type=int, default=100)
    archive = commands.add_parser("archive")
    archive.add_argument("query")
    commands.add_parser("backup")
    commands.add_parser("stop", help="停止使用此数据目录的网关；校验 PID 与进程创建时间")
    configure = commands.add_parser("config", help="生成 stdio 客户端配置；不改动现有全局配置")
    configure.add_argument("--output", type=Path, default=Path("config/mcp.local.json"))
    resume = commands.add_parser("resume")
    resume.add_argument("task_id")
    return root


def main():
    args = parser().parse_args()
    settings = Settings.load(args.data_dir)
    settings.prepare()
    try:
        if args.command == "serve":
            from .server import create_server

            create_server(settings).run(transport="stdio")
            return
        store = Store(settings.data_dir)
        if args.command == "doctor":
            with store.connect() as db:
                integrity = db.execute("PRAGMA quick_check").fetchone()[0]
            output(
                {
                    "status": "ok",
                    "python": platform.python_version(),
                    "system": platform.system(),
                    "architecture": platform.machine(),
                    "data_dir": str(settings.data_dir),
                    "database": integrity,
                    "login_file_present": AuthStore(settings.data_dir).path.exists(),
                    "platforms": list(capabilities()["platforms"]),
                    "versions": {
                        name: importlib.metadata.version(name)
                        for name in ("mcp", "httpx", "pydantic")
                    },
                    "chatgpt_ordinary_chat": "NOT_VERIFIED",
                }
            )
        elif args.command == "search":
            ids = args.content
            request = SearchRequest(
                query=args.query,
                platforms=[args.platform],
                keywords=args.keyword,
                depth=args.depth,
                pages_per_keyword=args.pages,
                max_items=args.max_items,
                comment_pages=args.comment_pages,
                max_comments=args.max_comments,
                comment_sorts=[] if args.no_comments else (args.comment_sort or ["hot", "latest"]),
                max_danmaku=args.max_danmaku,
                subtitle_tracks=args.subtitle_tracks,
                reply_pages=args.reply_pages,
                max_requests=args.max_requests,
                timeout_seconds=args.timeout,
                content_ids=ids,
                refresh=args.refresh,
            )
            result = asyncio.run(run_search(settings, request))
            output(result)
            if result["status"] in ("failed", "partial"):
                sys.exit(2)
        elif args.command == "auth":
            auth = AuthStore(settings.data_dir)
            if args.auth_action == "login":
                output(asyncio.run(auth.login(open_browser=not args.no_browser)))
            elif args.auth_action == "status":
                output(asyncio.run(auth.status()))
            elif args.auth_action == "panel":
                from .auth_panel import ensure_panel

                output(asyncio.run(ensure_panel(settings, open_browser=True)))
            else:
                auth.import_cc(args.path)
                output(asyncio.run(auth.status()))
        elif args.command == "task":
            if args.offset < 0 or not 1 <= args.limit <= 20:
                raise GatewayError("INVALID_PAGINATION", "offset ≥ 0；limit 介于 1 和 20")
            output(
                task_result(
                    store, args.task_id, view=args.view, offset=args.offset, limit=args.limit
                )
            )
        elif args.command == "fetch":
            if args.offset < 0 or not 1 <= args.limit <= 200:
                raise GatewayError("INVALID_PAGINATION", "offset ≥ 0；limit 介于 1 和 200")
            output(
                fetch_saved(
                    store, args.task_id, args.content_id, args.section, args.offset, args.limit
                )
            )
        elif args.command == "archive":
            output({"results": store.archive_search(args.query)})
        elif args.command == "backup":
            output({"status": "ok", "backup": str(store.backup()), "includes_credentials": False})
        elif args.command == "config":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(
                args.output,
                {
                    "mcpServers": {
                        "gptch-search": {
                            "type": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "personal_search.server"],
                            "env": {"GPTCH_DATA_DIR": str(settings.data_dir)},
                        }
                    }
                },
            )
            toml = args.output.with_suffix(".toml")
            toml.write_text(
                "[mcp_servers.gptch-search]\ncommand = "
                + json.dumps(sys.executable)
                + '\nargs = ["-m", "personal_search.server"]\n\n[mcp_servers.gptch-search.env]\nGPTCH_DATA_DIR = '
                + json.dumps(str(settings.data_dir))
                + "\n",
                encoding="utf-8",
            )
            output(
                {
                    "status": "ok",
                    "mcp_config": str(args.output.resolve()),
                    "codex_config": str(toml.resolve()),
                    "registered_globally": False,
                }
            )
        elif args.command == "stop":
            manifest = settings.data_dir / "gateway.json"
            if not manifest.exists():
                output({"status": "not_running"})
                return
            record = json.loads(manifest.read_text(encoding="utf-8"))
            try:
                process = psutil.Process(record["pid"])
                if abs(process.create_time() - record["created"]) > 0.01:
                    raise GatewayError("PID_REUSED", "PID 已被其他程序使用，未停止该进程")
                process.terminate()
                process.wait(timeout=5)
                output(
                    {
                        "status": "stopped",
                        "worker_cleanup": "工作进程检测网关退出后释放资源，最长约 1 秒。",
                    }
                )
            except psutil.NoSuchProcess:
                output({"status": "not_running"})
            manifest.unlink(missing_ok=True)
        elif args.command == "resume":

            async def resume_and_wait():
                async with TaskManager(settings) as manager:
                    manager.resume(args.task_id)
                    while manager.store.task(args.task_id)["status"] not in TERMINAL:
                        await asyncio.sleep(0.5)
                    return task_result(manager.store, args.task_id, view="items")

            output(asyncio.run(resume_and_wait()))
    except KeyboardInterrupt:
        output(
            {"status": "interrupted", "message": "任务已停止，已采集数据保留；可使用 resume 恢复。"}
        )
        sys.exit(130)
    except ValidationError:
        output(
            {
                "status": "failed",
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "参数不符合预算或平台要求；请查看 --help",
                },
            }
        )
        sys.exit(2)
    except Exception as exc:
        output({"status": "failed", "error": safe_error(exc)})
        sys.exit(2)


if __name__ == "__main__":
    main()
