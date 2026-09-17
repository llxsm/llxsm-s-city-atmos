"""治理平台的目录元数据导出。

治理部门需要把资产目录交付给外部治理工具（或作为配置备份），
因此提供一份包含资产定义、字段字典、血缘与质量模型的完整 JSON。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from atmos.apps.common.deps import get_registry
from atmos.catalog.definitions import AssetRegistry
from atmos.utils import iso_z, utcnow

router = APIRouter(prefix="/export", tags=["元数据导出"])


@router.get("/metadata", summary="导出资产目录元数据")
def export_metadata(registry: AssetRegistry = Depends(get_registry)) -> Response:
    """导出全部资产定义、字段字典、血缘、城市主数据与质量模型。"""
    payload = {
        "platform": "city-atmos-governance",
        "exported_at": iso_z(utcnow()),
        "assets": [
            {
                **definition.to_payload(),
                "columns": [column.to_payload() for column in definition.columns],
            }
            for definition in registry.assets
        ],
        "lineage": [
            {
                "upstream": edge.upstream,
                "downstream": edge.downstream,
                "transform_type": edge.transform_type,
                "transform_ref": edge.transform_ref,
                "description": edge.description,
            }
            for edge in registry.lineage
        ],
        "cities": [city.to_payload() for city in registry.cities],
        "quality_model": {
            "version": registry.quality.version,
            "description": registry.quality.description,
            "dimensions": [
                {
                    "key": item.key,
                    "name": item.name,
                    "weight": item.weight,
                    "description": item.description,
                }
                for item in registry.quality.dimensions
            ],
            "grading": list(registry.quality.grading),
            "defaults": registry.quality.defaults,
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
                for item in registry.quality.checks
            ],
        },
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="atmos_catalog_metadata.json"'},
    )


__all__ = ["router"]
