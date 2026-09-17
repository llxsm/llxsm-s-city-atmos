"""数据资产目录与城市主数据路由。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from atmos.analytics.environment import EnvironmentService
from atmos.apps.common.deps import get_environment_service, get_registry
from atmos.apps.common.schemas import CityCreateRequest, CityUpdateRequest
from atmos.catalog.definitions import AssetRegistry
from atmos.catalog.service import get_asset_detail, list_assets, list_cities
from atmos.db import get_db
from atmos.models import City
from atmos.utils import iso_z, utcnow

router = APIRouter(tags=["资产目录"])


# ============================================================
# 数据资产
# ============================================================
@router.get("/assets", summary="数据资产列表")
def get_assets(
    session: Session = Depends(get_db),
    registry: AssetRegistry = Depends(get_registry),
    domain: str | None = Query(None, description="业务域筛选"),
    layer: str | None = Query(None, description="分层筛选"),
    status: str | None = Query(None, description="状态筛选"),
    owner: str | None = Query(None, description="责任人筛选"),
    search: str | None = Query(None, description="关键字（资产名/字段名/标签）"),
) -> dict:
    """按条件检索资产卡片；同时返回筛选维度上的计数，便于前端展示分布。"""
    items = list_assets(
        session, domain=domain, layer=layer, status=status, owner=owner, search=search
    )
    all_items = list_assets(session)
    return {
        "total": len(items),
        "items": items,
        "facets": {
            "domain": _count_by(all_items, "domain"),
            "layer": _count_by(all_items, "layer"),
            "status": _count_by(all_items, "status"),
            "owner": _count_by(all_items, "owner"),
            "grade": _count_by(all_items, "latest_grade"),
        },
        "registry_assets": len(registry.assets),
    }


@router.get("/assets/{asset_key:path}/sample", summary="资产样例数据")
def asset_sample(
    asset_key: str,
    city: str | None = Query(None, description="城市 slug"),
    limit: int = Query(20, ge=1, le=500),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """从数据湖读取若干行样例。"""
    return service.asset_sample(asset_key, city_slug=city, limit=limit)


@router.get("/assets/{asset_key:path}/profiling", summary="资产列级画像")
def asset_profiling(
    asset_key: str,
    city: str | None = Query(None, description="城市 slug"),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """空值率、唯一值数、极值等列级统计。"""
    return service.asset_profiling(asset_key, city_slug=city)


@router.get("/assets/{asset_key:path}", summary="资产详情")
def asset_detail(
    asset_key: str,
    session: Session = Depends(get_db),
) -> dict:
    """资产卡片 + 字段字典 + 版本快照 + 检查项 + 最近运行。"""
    detail = get_asset_detail(session, asset_key)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"未找到数据资产: {asset_key}")
    return detail


# ============================================================
# 城市主数据
# ============================================================
@router.get("/cities", summary="城市主数据列表")
def get_cities(
    session: Session = Depends(get_db),
    active_only: bool = Query(False, description="仅返回启用城市"),
) -> dict:
    """城市清单，含资产覆盖情况。"""
    items = list_cities(session, active_only=active_only)
    return {
        "total": len(items),
        "active": sum(1 for item in items if item["is_active"]),
        "items": items,
        "countries": _count_by(items, "country"),
    }


@router.post("/cities", status_code=201, summary="新增城市")
def create_city(payload: CityCreateRequest, session: Session = Depends(get_db)) -> dict:
    """注册新的监测城市。

    注意：该接口只写入资产目录；要让配置与代码仓库保持一致，
    应同时把城市追加到 ``config/cities.yaml``。
    """
    existing = session.scalar(select(City).where(City.slug == payload.slug))
    if existing is not None:
        raise HTTPException(status_code=409, detail=f"城市已存在: {payload.slug}")

    city = City(**payload.model_dump())
    session.add(city)
    session.commit()
    session.refresh(city)
    return {
        "ok": True,
        "message": f"城市 {city.name_zh} 已注册",
        "city": list_cities(session) and _city_payload(city),
        "hint": "建议同步更新 config/cities.yaml 以保持配置即代码",
        "generated_at": iso_z(utcnow()),
    }


@router.patch("/cities/{slug}", summary="更新城市")
def update_city(
    slug: str, payload: CityUpdateRequest, session: Session = Depends(get_db)
) -> dict:
    """部分更新城市主数据。"""
    city = session.scalar(select(City).where(City.slug == slug))
    if city is None:
        raise HTTPException(status_code=404, detail=f"未找到城市: {slug}")
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    for key, value in changes.items():
        setattr(city, key, value)
    session.commit()
    session.refresh(city)
    return {"ok": True, "message": f"已更新 {len(changes)} 个字段", "city": _city_payload(city)}


def _city_payload(city: City) -> dict[str, Any]:
    return {
        "slug": city.slug,
        "name_zh": city.name_zh,
        "name_en": city.name_en,
        "country": city.country,
        "country_code": city.country_code,
        "admin1": city.admin1,
        "latitude": city.latitude,
        "longitude": city.longitude,
        "timezone": city.timezone,
        "elevation": city.elevation,
        "population": city.population,
        "climate_zone": city.climate_zone,
        "is_active": city.is_active,
        "tags": city.tags or [],
    }


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = item.get(key)
        if value is None:
            continue
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items(), key=lambda pair: -pair[1]))
