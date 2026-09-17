"""治理平台的运维与审计路由。

治理部门关心"数据是怎么进来的、有没有出问题"，因此保留：
作业清单、运行记录明细（行数/字节/API 调用/去重数/schema 漂移/错误）、
元数据同步与统计刷新。

**不包含**触发采集 —— 那是分析平台"刷新数据"的职责，
避免两个平台都能改数据而造成责任边界模糊。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from atmos.apps.common.deps import get_registry
from atmos.catalog.definitions import AssetRegistry
from atmos.catalog.stats import refresh_all_stats
from atmos.collect.jobs import JOB_SPECS
from atmos.collect.runner import recent_runs, run_summary
from atmos.db import get_db, init_db
from atmos.logging_setup import get_logger
from atmos.utils import iso_z, utcnow

logger = get_logger("apps.governance.ops")

router = APIRouter(prefix="/ops", tags=["运维与审计"])


@router.get("/jobs", summary="采集作业清单")
def jobs() -> dict:
    """平台定义的 ETL 作业、负责的资产与执行顺序。"""
    return {
        "total": len(JOB_SPECS),
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


@router.post("/bootstrap", summary="同步配置元数据到资产目录")
async def bootstrap(drop_cache: bool = Query(True, description="是否重载配置缓存")) -> dict:
    """把 config/*.yaml 的定义幂等同步进目录：新增/更新资产与字段，
    移除的城市会被停用（保留历史以维持引用完整性）。"""
    from atmos.catalog.bootstrap import bootstrap_catalog
    from atmos.catalog.definitions import reload_registry

    init_db()
    if drop_cache:
        reload_registry()

    def _run() -> dict:
        report = bootstrap_catalog()
        return {"status": "success", **report.to_payload()}

    return await asyncio.to_thread(_run)


@router.post("/refresh", summary="刷新资产统计与版本快照")
async def refresh() -> dict:
    """扫描数据湖重算资产行数/体积/时间范围，并生成版本快照。"""
    result = await asyncio.to_thread(refresh_all_stats)
    return {
        "status": "success",
        "assets_refreshed": len(result),
        "items": [
            {
                "asset_key": item.get("asset_key"),
                "row_count": item.get("row_count"),
                "byte_size": item.get("byte_size"),
                "city_count": item.get("city_count"),
                "version": item.get("version"),
            }
            for item in result
        ],
        "generated_at": iso_z(utcnow()),
    }


@router.get("/runs", summary="采集运行记录（审计）")
def runs(
    limit: int = Query(100, ge=1, le=1000),
    asset: str | None = Query(None, description="限定资产 key"),
    session: Session = Depends(get_db),
) -> dict:
    """逐条运行明细：状态、行数、字节、API 调用、分区数、错误与 schema 漂移。"""
    items = recent_runs(limit=limit, asset_key=asset)
    return {"total": len(items), "items": items}


@router.get("/summary", summary="运行态势汇总")
def summary(hours: int = Query(24, ge=1, le=720)) -> dict:
    """近 N 小时的运行成功率、写入行数、API 调用统计。"""
    return run_summary(hours)


@router.get("/specs", summary="数据源与作业契约")
def specs(registry: AssetRegistry = Depends(get_registry)) -> dict:
    """每个作业实际向数据源请求的字段清单，用于核对上游契约是否变化。"""
    from atmos.sources.specs import VARIABLES_BY_ASSET

    return {
        "total": len(VARIABLES_BY_ASSET),
        "items": [
            {
                "asset_key": asset_key,
                "name": registry.asset(asset_key).name if registry.has_asset(asset_key) else asset_key,
                "variables": list(variables),
                "variable_count": len(variables),
            }
            for asset_key, variables in VARIABLES_BY_ASSET.items()
        ],
    }
