"""分析平台元信息路由。

``/api/overview`` 在这里是**数据覆盖视角**而非治理视角：
回答"我手上有多少可用于分析的数据、覆盖到什么时段"，
而不是"有多少资产、谁负责、SLA 履约如何"（后者属于治理平台）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atmos.analytics.environment import EnvironmentService
from atmos.apps.common.deps import get_environment_service, get_insight_service, get_registry
from atmos.catalog.definitions import AssetRegistry
from atmos.db import get_db, healthcheck
from atmos.insights.service import InsightService
from atmos.models import DataAsset
from atmos.settings import get_settings
from atmos.utils import iso_z, utcnow

router = APIRouter(tags=["分析平台元信息"])


@router.get("/health", summary="健康检查")
def health(session: Session = Depends(get_db)) -> dict:
    """分析平台组件连通性。"""
    return {
        "status": "ok" if healthcheck().get("status") == "ok" else "degraded",
        "portal": "analysis",
        "name": "城市环境数据分析平台",
        "version": __import__("atmos").__version__,
        "catalog": healthcheck(),
        "generated_at": iso_z(utcnow()),
    }


@router.get("/meta", summary="平台元数据字典")
def meta() -> dict:
    """枚举字典与数据源信息，供前端渲染中文标签与筛选器。"""
    settings = get_settings()
    return {
        "platform": {
            "name": "城市环境数据分析平台",
            "portal": "analysis",
            "version": __import__("atmos").__version__,
            "env": settings.env,
        },
        "sources": [
            {
                "key": "openmeteo",
                "name": "Open-Meteo",
                "doc": "https://open-meteo.com/en/docs",
                "products": [
                    {"name": "Forecast API", "endpoint": settings.openmeteo_forecast_host + "/v1/forecast"},
                    {"name": "Archive API (ERA5)", "endpoint": settings.openmeteo_archive_host + "/v1/archive"},
                    {"name": "Air Quality API (CAMS)", "endpoint": settings.openmeteo_air_host + "/v1/air-quality"},
                ],
                "license": "CC-BY-4.0（非商业用途免 API Key）",
            }
        ],
        "collect": {
            "max_cities": settings.collect_max_cities,
            "archive_lookback_days": settings.archive_lookback_days,
            "forecast_horizon_days": settings.forecast_horizon_days,
        },
    }


@router.get("/overview", summary="数据覆盖总览")
def overview(
    session: Session = Depends(get_db),
    registry: AssetRegistry = Depends(get_registry),
    insights: InsightService = Depends(get_insight_service),
) -> dict:
    """分析首页 KPI：数据量、覆盖时段、城市数、各数据源可用性。"""
    availability = insights.availability()

    coverage = {}
    for key in ("hourly", "daily", "archive"):
        coverage[key] = {"available": availability.get(f"has_{'environment' if key == 'hourly' else key}")}

    total_rows = session.scalar(select(func.coalesce(func.sum(DataAsset.row_count), 0))) or 0
    total_bytes = session.scalar(select(func.coalesce(func.sum(DataAsset.byte_size), 0))) or 0

    stations = [
        {
            "asset_key": asset.asset_key,
            "name": asset.name,
            "granularity": asset.granularity,
            "row_count": asset.row_count,
            "city_count": asset.city_count,
            "latest_data_time": iso_z(asset.latest_data_time),
        }
        for asset in session.scalars(select(DataAsset).order_by(DataAsset.asset_key)).all()
    ]

    return {
        "total_rows": int(total_rows),
        "total_bytes": int(total_bytes),
        "city_count": len(availability.get("cities") or []),
        "cities": availability.get("cities") or [],
        "dataset_count": len(registry.assets),
        "datasets": stations,
        "availability": availability,
        "analysis_windows": {
            "hourly_days": availability.get("hourly_days"),
            "daily_days": availability.get("daily_days"),
            "archive_days": availability.get("archive_days"),
        },
        "has_data": bool(total_rows),
        "hint": None if total_rows else "尚无数据，请先在「数据刷新」页触发一次采集",
        "generated_at": iso_z(utcnow()),
    }


@router.get("/availability", summary="数据可用性")
def availability(insights: InsightService = Depends(get_insight_service)) -> dict:
    """各数据源是否就绪，供前端在数据不足时给出提示。"""
    return insights.availability()
