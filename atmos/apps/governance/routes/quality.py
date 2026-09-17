"""数据质量路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from atmos.apps.common.deps import get_quality_engine
from atmos.apps.common.schemas import QualityRequest
from atmos.apps.common.tasks import get_task_registry
from atmos.db import get_db, session_scope
from atmos.quality.engine import QualityEngine
from atmos.quality.service import (
    latest_scores,
    list_checks,
    quality_summary,
    recent_results,
    score_history,
)
from atmos.utils import iso_z, utcnow

router = APIRouter(prefix="/quality", tags=["数据质量"])


@router.get("/summary", summary="质量态势总览")
def summary(session: Session = Depends(get_db)) -> dict:
    """五维均值、等级分布、最差资产与高频失败检查项。"""
    return quality_summary(session)


@router.get("/scores", summary="资产质量评分")
def scores(
    asset: str | None = Query(None, description="资产 key"),
    limit: int = Query(50, ge=1, le=500),
    session: Session = Depends(get_db),
) -> dict:
    """每个资产的最新一次五维评分。"""
    items = latest_scores(session, asset_key=asset, limit=limit)
    return {"total": len(items), "items": items}


@router.get("/scores/{asset_key:path}/history", summary="质量评分趋势")
def history(
    asset_key: str,
    limit: int = Query(60, ge=1, le=500),
    session: Session = Depends(get_db),
) -> dict:
    """某资产的历史评分序列，用于观察治理成效。"""
    items = score_history(session, asset_key, limit=limit)
    if not items:
        raise HTTPException(status_code=404, detail=f"暂无质量评分记录: {asset_key}")
    return {"asset_key": asset_key, "total": len(items), "items": items}


@router.get("/results", summary="质量检查明细")
def results(
    asset: str | None = Query(None),
    status: str | None = Query(None, pattern="^(pass|warn|fail|error|skipped)$"),
    dimension: str | None = Query(
        None, pattern="^(completeness|timeliness|validity|consistency|uniqueness)$"
    ),
    limit: int = Query(100, ge=1, le=1000),
    session: Session = Depends(get_db),
) -> dict:
    """最近的质量检查逐项结果。"""
    items = recent_results(session, asset_key=asset, status=status, dimension=dimension, limit=limit)
    return {"total": len(items), "items": items}


@router.get("/checks", summary="质量检查项定义")
def checks(
    asset: str | None = Query(None, description="资产 key"),
    session: Session = Depends(get_db),
) -> dict:
    """五维模型下每个资产实际生效的检查项。"""
    items = list_checks(session, asset_key=asset)
    return {"total": len(items), "items": items}


@router.post("/evaluate", summary="触发质量评测")
async def evaluate(
    payload: QualityRequest,
    engine: QualityEngine = Depends(get_quality_engine),
) -> dict:
    """对全部或指定资产生成一次五维质量评测。

    默认异地执行并立即返回任务 ID；``wait=true`` 时同步等待结果。
    """
    params = {"assets": payload.assets, "window_hours": payload.window_hours}

    if payload.wait:
        return await _evaluate(engine, params)

    registry = get_task_registry()
    record = await registry.submit("quality", _evaluate(engine, params), params=params)
    return {
        "status": "accepted",
        "task_id": record.task_id,
        "message": "质量评测已在后台启动，可通过 /api/quality/tasks 轮询进度",
    }


async def _evaluate(engine: QualityEngine, params: dict) -> dict:
    """在线程池中执行评测，避免阻塞事件循环。"""
    import asyncio

    def _run() -> dict:
        with session_scope() as session:
            reports = engine.evaluate_all(
                session, window_hours=params.get("window_hours"), asset_keys=params.get("assets")
            )
        return {
            "status": "success",
            "evaluated": len(reports),
            "window_hours": params.get("window_hours"),
            "results": [
                {
                    "asset_key": report.asset_key,
                    "overall": round(report.overall, 2),
                    "grade": report.grade,
                    "grade_label": report.grade_label,
                    "checks_total": report.checks_total,
                    "checks_failed": report.checks_failed,
                    "checks_warned": report.checks_warned,
                    "cities_evaluated": report.cities_evaluated,
                    "rows_evaluated": report.rows_evaluated,
                }
                for report in reports
            ],
            "generated_at": iso_z(utcnow()),
        }

    return await asyncio.to_thread(_run)


@router.get("/evaluate/{asset_key:path}", summary="单资产评测（同步）")
def evaluate_single(
    asset_key: str,
    window_hours: int | None = Query(None, ge=1, le=8760),
    engine: QualityEngine = Depends(get_quality_engine),
) -> dict:
    """同步评测单个资产并返回完整明细，便于排查。"""
    with session_scope() as session:
        try:
            report = engine.evaluate_asset(session, asset_key, window_hours=window_hours)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return report.to_payload()


@router.get("/tasks", summary="质量评测任务列表")
async def tasks(limit: int = Query(20, ge=1, le=100)) -> dict:
    """后台评测任务的实时状态。"""
    registry = get_task_registry()
    items = [item for item in registry.list(limit=limit) if item.kind == "quality"]
    return {"total": len(items), "items": [item.to_payload() for item in items]}
