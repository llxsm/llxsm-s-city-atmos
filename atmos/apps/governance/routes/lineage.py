"""数据血缘路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from atmos.catalog.lineage import build_graph, orphan_report, trace
from atmos.db import get_db

router = APIRouter(prefix="/lineage", tags=["数据血缘"])


@router.get("/graph", summary="血缘图谱")
def lineage_graph(session: Session = Depends(get_db)) -> dict:
    """返回前端可直接渲染的节点与边集合，含拓扑深度与血缘健康度。"""
    return build_graph(session)


@router.get("/orphans", summary="血缘孤岛巡检")
def lineage_orphans(session: Session = Depends(get_db)) -> dict:
    """未接入血缘或缺少边说明的资产清单。"""
    return orphan_report(session)


@router.get("/{asset_key:path}/impact", summary="影响分析")
def impact(
    asset_key: str,
    direction: str = Query("downstream", pattern="^(upstream|downstream|both)$"),
    max_depth: int = Query(10, ge=1, le=20),
    session: Session = Depends(get_db),
) -> dict:
    """从某资产出发追溯上下游。

    ``downstream`` 用于回答"这个资产坏了会影响谁"，
    ``upstream`` 用于回答"这个资产脏了，问题可能出在哪"。
    """
    result = trace(session, asset_key, direction=direction, max_depth=max_depth)  # type: ignore[arg-type]
    if not result.get("found"):
        raise HTTPException(status_code=404, detail=f"未找到数据资产: {asset_key}")
    return result
