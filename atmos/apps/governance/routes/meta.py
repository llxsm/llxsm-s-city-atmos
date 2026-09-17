"""治理平台元信息路由。

与治理业务直接相关：目录总览（资产数、SLA 履约、质量分布）与
枚举/质量模型字典。平台总览刻意只保留治理视角的指标，
数据覆盖与数据量统计由分析平台的 ``/api/overview`` 承担。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from atmos.apps.common.deps import get_registry
from atmos.catalog.definitions import AssetRegistry
from atmos.catalog.service import overview
from atmos.db import get_db, healthcheck
from atmos.models.enums import (
    AssetDomain,
    AssetLayer,
    AssetStatus,
    CheckSeverity,
    CheckStatus,
    Granularity,
    QualityDimension,
    Sensitivity,
    TransformType,
    UpdateFrequency,
)
from atmos.settings import get_settings
from atmos.utils import iso_z, utcnow

router = APIRouter(tags=["治理平台元信息"])


@router.get("/health", summary="健康检查")
def health(session: Session = Depends(get_db)) -> dict:
    """治理平台组件连通性。"""
    registry = get_registry()
    return {
        "status": "ok" if healthcheck().get("status") == "ok" else "degraded",
        "portal": "governance",
        "name": "数据资产治理平台",
        "version": __import__("atmos").__version__,
        "catalog": healthcheck(),
        "registry": {
            "assets": len(registry.assets),
            "cities": len(registry.cities),
            "lineage_edges": len(registry.lineage),
            "quality_checks": len(registry.quality.checks),
        },
        "generated_at": iso_z(utcnow()),
    }


@router.get("/meta", summary="枚举字典与质量模型")
def meta() -> dict:
    """供前端渲染中文标签与质量模型说明。"""
    settings = get_settings()
    registry = get_registry()
    model = registry.quality
    return {
        "platform": {
            "name": "数据资产治理平台",
            "portal": "governance",
            "version": __import__("atmos").__version__,
            "env": settings.env,
        },
        "enums": {
            "asset_layer": [{"value": item.value, "label": item.label} for item in AssetLayer],
            "asset_domain": [{"value": item.value, "label": item.label} for item in AssetDomain],
            "asset_status": [{"value": item.value, "label": item.label} for item in AssetStatus],
            "granularity": [{"value": item.value, "label": item.label} for item in Granularity],
            "update_frequency": [
                {"value": item.value, "label": item.label} for item in UpdateFrequency
            ],
            "sensitivity": [{"value": item.value, "label": item.label} for item in Sensitivity],
            "quality_dimension": [
                {"value": item.value, "label": item.label} for item in QualityDimension
            ],
            "check_severity": [
                {"value": item.value, "label": item.label} for item in CheckSeverity
            ],
            "check_status": [{"value": item.value, "label": item.label} for item in CheckStatus],
            "transform_type": [
                {"value": item.value, "label": item.label} for item in TransformType
            ],
        },
        "quality_model": {
            "version": model.version,
            "description": model.description,
            "dimensions": [
                {
                    "key": item.key,
                    "name": item.name,
                    "weight": item.weight,
                    "description": item.description,
                }
                for item in model.dimensions
            ],
            "grading": list(model.grading),
            "defaults": model.defaults,
            "checks": [
                {
                    "key": item.key,
                    "name": item.name,
                    "dimension": item.dimension,
                    "severity": item.severity,
                    "description": item.description,
                    "applies_to": list(item.applies_to),
                    "params": item.params,
                }
                for item in model.checks
            ],
        },
        "sources": [
            {
                "name": "Open-Meteo",
                "doc": "https://open-meteo.com/en/docs",
                "license": "CC-BY-4.0（非商业用途免 API Key）",
            }
        ],
    }


@router.get("/overview", summary="资产目录总览")
def platform_overview(
    session: Session = Depends(get_db),
    registry: AssetRegistry = Depends(get_registry),
) -> dict:
    """治理首页 KPI：资产数、字段数、SLA 超期、质量等级分布、近 24 小时运行。"""
    return overview(session, registry)
