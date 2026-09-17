"""后台任务登记簿。

"开始采集"这类操作耗时可达数十秒，同步等待会让浏览器超时，
因此 API 采用「立即受理 + 轮询状态」的异步语义。任务状态同时落库
（``collection_runs``），进程重启后历史仍可查，内存中的登记簿只负责
提供"正在进行中"的实时视图。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Coroutine

from atmos.logging_setup import get_logger
from atmos.models.enums import RunStatus
from atmos.utils import iso_z, new_run_id, utcnow

logger = get_logger("api.tasks")

MAX_HISTORY = 30


@dataclass
class TaskRecord:
    """一个后台任务的实时状态。"""

    task_id: str
    kind: str
    status: str = RunStatus.PENDING.value
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    def to_payload(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "started_at": iso_z(self.started_at),
            "finished_at": iso_z(self.finished_at),
            "duration_ms": self.duration_ms,
            "params": self.params,
            "result": self.result,
            "error": self.error,
        }


class TaskRegistry:
    """进程内后台任务登记簿（单例）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._handles: dict[str, asyncio.Task[Any]] = {}
        self._lock = asyncio.Lock()

    # ---------- 提交 ----------
    async def submit(
        self,
        kind: str,
        coroutine: Coroutine[Any, Any, dict[str, Any]],
        *,
        params: dict[str, Any] | None = None,
    ) -> TaskRecord:
        """登记并启动一个后台任务。"""
        record = TaskRecord(
            task_id=new_run_id(kind),
            kind=kind,
            status=RunStatus.RUNNING.value,
            params=params or {},
        )
        async with self._lock:
            self._tasks[record.task_id] = record
            self._handles[record.task_id] = asyncio.create_task(
                self._run(record, coroutine), name=record.task_id
            )
            self._trim()
        logger.info("后台任务已提交 %s (%s)", record.task_id, kind)
        return record

    async def _run(
        self, record: TaskRecord, coroutine: Coroutine[Any, Any, dict[str, Any]]
    ) -> None:
        try:
            result = await coroutine
            record.result = result
            record.status = str(result.get("status", RunStatus.SUCCESS.value))
        except Exception as exc:  # pragma: no cover - 由任务体决定可控性
            logger.exception("后台任务 %s 失败", record.task_id)
            record.error = f"{type(exc).__name__}: {exc}"
            record.status = RunStatus.FAILED.value
        finally:
            record.finished_at = utcnow()
            self._handles.pop(record.task_id, None)

    def _trim(self) -> None:
        if len(self._tasks) <= MAX_HISTORY:
            return
        finished = sorted(
            (item for item in self._tasks.values() if item.finished_at is not None),
            key=lambda item: item.finished_at or item.started_at,
        )
        for item in finished[: max(0, len(self._tasks) - MAX_HISTORY)]:
            self._tasks.pop(item.task_id, None)

    # ---------- 查询 ----------
    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def list(self, *, limit: int = 20) -> list[TaskRecord]:
        return sorted(
            self._tasks.values(), key=lambda item: item.started_at, reverse=True
        )[:limit]

    def active(self) -> list[TaskRecord]:
        return [item for item in self._tasks.values() if item.finished_at is None]


_registry: TaskRegistry | None = None


def get_task_registry() -> TaskRegistry:
    """获取进程内单例登记簿。"""
    global _registry
    if _registry is None:
        _registry = TaskRegistry()
    return _registry


__all__ = ["TaskRecord", "TaskRegistry", "get_task_registry"]
