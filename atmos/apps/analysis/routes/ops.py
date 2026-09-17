"""分析平台的数据刷新路由。

分析人员需要的是"让数据变新"，而不是运维监控，因此这里只保留：
触发采集、查看任务进度、作业清单（说明会拉取什么）。
运行记录的逐条审计（行数/字节/错误/schema 漂移）属于治理平台。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query

from atmos.apps.common.deps import get_quality_engine
from atmos.apps.common.schemas import CollectRequest
from atmos.apps.common.tasks import get_task_registry
from atmos.collect.jobs import JOB_SPECS
from atmos.collect.runner import SCOPE_STAGES, Collector
from atmos.db import session_scope
from atmos.logging_setup import get_logger
from atmos.quality.engine import QualityEngine
from atmos.utils import iso_z, utcnow

logger = get_logger("apps.analysis.ops")

router = APIRouter(prefix="/ops", tags=["数据刷新"])


@router.get("/jobs", summary="采集作业清单")
def jobs() -> dict:
    """平台定义的 ETL 作业及其负责的数据集。"""
    return {
        "total": len(JOB_SPECS),
        "scopes": {key: list(value) for key, value in SCOPE_STAGES.items()},
        "items": [
            {
                "name": spec.name,
                "asset_key": spec.asset_key,
                "stage": spec.stage,
                "description": spec.description,
            }
            for spec in JOB_SPECS
        ],
    }


@router.post("/collect", summary="刷新数据（触发采集）")
async def collect(
    payload: CollectRequest,
    wait: bool | None = Query(None, description="覆盖请求体中的 wait"),
) -> dict:
    """启动采集。默认立即返回任务 ID，避免浏览器超时。"""
    should_wait = payload.wait if wait is None else wait
    params = {
        "scope": payload.scope,
        "cities": payload.cities,
        "include_archive": payload.include_archive,
        "quality_after": payload.quality_after,
    }

    async def _run() -> dict:
        report = await Collector().run(
            scope=payload.scope,
            city_slugs=payload.cities,
            trigger="api",
            include_archive=payload.include_archive,
        )
        result = report.to_payload()
        if payload.quality_after:
            result["quality"] = await asyncio.to_thread(_evaluate_quality)
        result["status"] = report.status
        return result

    if should_wait:
        return await _run()

    registry = get_task_registry()
    record = await registry.submit("collect", _run(), params=params)
    return {
        "status": "accepted",
        "task_id": record.task_id,
        "message": "采集管道已在后台启动，可通过 /api/ops/tasks 轮询进度",
    }


def _evaluate_quality() -> dict:
    """采集后立即评测，保证治理平台看到的质量分与最新数据一致。"""
    engine: QualityEngine = get_quality_engine()
    with session_scope() as session:
        reports = engine.evaluate_all(session)
    return {
        "evaluated": len(reports),
        "average": round(sum(item.overall for item in reports) / len(reports), 2) if reports else None,
    }


@router.get("/tasks", summary="后台任务列表")
async def tasks(limit: int = Query(20, ge=1, le=100)) -> dict:
    """采集任务的实时状态（含正在运行的）。"""
    registry = get_task_registry()
    items = registry.list(limit=limit)
    return {
        "total": len(items),
        "active": len(registry.active()),
        "items": [item.to_payload() for item in items],
        "generated_at": iso_z(utcnow()),
    }
