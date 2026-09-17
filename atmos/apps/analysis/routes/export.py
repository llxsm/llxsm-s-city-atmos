"""分析平台的数据下载路由。

分析人员除了看结论，也需要把底层数据拿走做二次处理，
因此这里提供数据集级的 CSV / JSON 下载（可按城市与行数限定）。
"""

from __future__ import annotations

import csv
import io
import json

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from atmos.analytics.environment import EnvironmentService
from atmos.apps.common.deps import get_environment_service, get_registry
from atmos.catalog.definitions import AssetRegistry
from atmos.utils import iso_z, utcnow

router = APIRouter(prefix="/export", tags=["数据下载"])


@router.get("/{asset_key:path}.csv", summary="下载数据集为 CSV")
def export_csv(
    asset_key: str,
    city: str | None = Query(None, description="城市 slug"),
    limit: int = Query(5000, ge=1, le=200000),
    service: EnvironmentService = Depends(get_environment_service),
    registry: AssetRegistry = Depends(get_registry),
) -> Response:
    """导出为 UTF-8 BOM 的 CSV，Excel 可直接打开。"""
    if not registry.has_asset(asset_key):
        raise HTTPException(status_code=404, detail=f"未知数据集: {asset_key}")

    sample = service.asset_sample(asset_key, city_slug=city, limit=limit)
    rows = sample.get("rows", [])
    columns = sample.get("columns", [])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    if columns:
        writer.writerow(columns)
        for row in rows:
            writer.writerow([_cell(row.get(column)) for column in columns])

    filename = f"{asset_key.replace('.', '_')}{('_' + city) if city else ''}.csv"
    return Response(
        content=("\ufeff" + buffer.getvalue()).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Row-Count": str(len(rows)),
            "X-Generated-At": iso_z(utcnow()) or "",
        },
    )


@router.get("/{asset_key:path}.json", summary="下载数据集为 JSON")
def export_json(
    asset_key: str,
    city: str | None = Query(None, description="城市 slug"),
    limit: int = Query(2000, ge=1, le=50000),
    service: EnvironmentService = Depends(get_environment_service),
    registry: AssetRegistry = Depends(get_registry),
) -> Response:
    """结构化导出，附带字段字典便于下游登记来源与口径。"""
    if not registry.has_asset(asset_key):
        raise HTTPException(status_code=404, detail=f"未知数据集: {asset_key}")

    definition = registry.asset(asset_key)
    sample = service.asset_sample(asset_key, city_slug=city, limit=limit)
    payload = {
        "dataset": {
            "key": definition.asset_key,
            "name": definition.name,
            "domain": definition.domain,
            "granularity": definition.granularity,
            "source_system": definition.source_system,
            "source_endpoint": definition.source_endpoint,
            "license": definition.license,
            "business_definition": definition.business_definition,
        },
        "columns": [column.to_payload() for column in definition.columns],
        "city_filter": city,
        "row_count": len(sample.get("rows", [])),
        "rows": sample.get("rows", []),
        "exported_at": iso_z(utcnow()),
    }
    filename = f"{asset_key.replace('.', '_')}{('_' + city) if city else ''}.json"
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


__all__ = ["router"]
