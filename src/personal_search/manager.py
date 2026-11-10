from __future__ import annotations

import asyncio
import os
import sys

import psutil
from filelock import FileLock, Timeout

from .errors import GatewayError
from .models import TERMINAL, SearchRequest
from .storage import Store, atomic_json


class TaskManager:
    """Single queue owner per archive; one disposable worker at a time."""

    def __init__(self, settings):
        self.settings = settings
        settings.prepare()
        self.store = Store(settings.data_dir)
        self.lock = FileLock(settings.data_dir / "gateway.lock", timeout=0)
        self.process = None
        self.current_id = None
        self.dispatcher = None
        self.wake = asyncio.Event()
        self.process_guard = asyncio.Lock()

    async def __aenter__(self):
        try:
            self.lock.acquire()
        except Timeout:
            raise GatewayError(
                "SERVICE_IN_USE",
                "已有网关使用同一数据目录。请通过现有 MCP 服务调用，或关闭它后运行 CLI。",
            ) from None
        self.store.recover()
        atomic_json(
            self.settings.data_dir / "gateway.json",
            {"pid": os.getpid(), "created": psutil.Process().create_time()},
        )
        self.dispatcher = asyncio.create_task(self._dispatch())
        return self

    async def __aexit__(self, *args):
        self.dispatcher.cancel()
        await asyncio.gather(self.dispatcher, return_exceptions=True)
        async with self.process_guard:
            await self._terminate()
            if self.current_id and self.store.task(self.current_id)["status"] == "running":
                self.store.update(
                    self.current_id,
                    status="interrupted",
                    error={"code": "SERVICE_STOPPED", "message": "网关已停止；可恢复此任务。"},
                )
        (self.settings.data_dir / "gateway.json").unlink(missing_ok=True)
        self.lock.release()

    def submit(self, request: SearchRequest):
        if len(self.store.queued()) >= 8:
            raise GatewayError("QUEUE_FULL", "等待队列已达到 8 个任务上限")
        result = self.store.create_task(request.model_dump())
        self.wake.set()
        return result

    async def _terminate(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def cancel(self, task_id):
        async with self.process_guard:
            task = self.store.task(task_id)
            if task["status"] in TERMINAL:
                return task
            self.store.update(
                task_id,
                status="cancelled",
                error={"code": "USER_CANCELLED", "message": "任务已取消；已保存内容仍可读取。"},
            )
            if self.current_id == task_id:
                await self._terminate()
                # A finishing worker cannot overwrite cancellation after it has exited.
                self.store.update(task_id, status="cancelled")
        return self.store.task(task_id)

    def resume(self, task_id):
        task = self.store.task(task_id)
        if task["status"] in ("queued", "running"):
            return task
        if task["status"] == "completed":
            raise GatewayError(
                "TASK_ALREADY_COMPLETED", "任务已完成；需要更新资料时请提交 refresh 搜索"
            )
        if len(self.store.queued()) >= 8:
            raise GatewayError("QUEUE_FULL", "等待队列已达到上限")
        self.store.update(task_id, status="queued", clear_error=True)
        self.wake.set()
        return self.store.task(task_id)

    async def _dispatch(self):
        while True:
            queue = self.store.queued()
            if not queue:
                self.wake.clear()
                # No await between the check and clear; submit runs on this same loop.
                await self.wake.wait()
                continue
            task_id = queue[0]
            async with self.process_guard:
                if self.store.task(task_id)["status"] != "queued":
                    continue
                self.current_id = task_id
                self.store.update(task_id, status="running")
                log_path = self.settings.data_dir / "logs" / f"{task_id}.log"
                environment = dict(
                    os.environ, GPTCH_DATA_DIR=str(self.settings.data_dir), PYTHONUNBUFFERED="1"
                )
                try:
                    with log_path.open("ab") as log:
                        self.process = await asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "personal_search.worker",
                            task_id,
                            "--data-dir",
                            str(self.settings.data_dir),
                            "--parent-pid",
                            str(os.getpid()),
                            stdout=asyncio.subprocess.DEVNULL,
                            stderr=log,
                            env=environment,
                        )
                except Exception:
                    self.store.update(
                        task_id,
                        status="failed",
                        error={"code": "WORKER_START_FAILED", "message": "采集工作进程启动失败"},
                    )
                    self.current_id = None
                    continue
            await self.process.wait()
            async with self.process_guard:
                if self.store.task(task_id)["status"] == "running":
                    self.store.update(
                        task_id,
                        status="failed",
                        error={
                            "code": "WORKER_EXITED",
                            "message": "采集进程异常退出；已保存内容仍可读取。",
                        },
                    )
                self.current_id = None
                self.process = None
