"""环境数据路由：实况、时序、对比、排名、预警、历史趋势。

城市合法性一律以 **资产目录中启用中的城市** 为准（``service.city_index()``），
而不是配置文件里的定义 —— 这样通过治理平台停用的城市会立即从全部环境接口中消失，
与看板选择器保持同一口径。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from atmos.analytics.environment import EnvironmentService
from atmos.apps.common.deps import get_environment_service

router = APIRouter(prefix="/env", tags=["环境数据"])


def _known_cities(service: EnvironmentService) -> set[str]:
    """当前可选（启用中）的城市 slug 集合。"""
    return set(service.city_index())


def _resolve_cities(cities: str | None, service: EnvironmentService) -> list[str] | None:
    """解析逗号分隔的城市列表并校验。"""
    if not cities:
        return None
    requested = [item.strip() for item in cities.split(",") if item.strip()]
    unknown = sorted(set(requested) - _known_cities(service))
    if unknown:
        raise HTTPException(status_code=400, detail=f"不可用的城市: {', '.join(unknown)}")
    return requested


def _assert_city(slug: str, service: EnvironmentService) -> None:
    if slug not in _known_cities(service):
        raise HTTPException(status_code=404, detail=f"不可用的城市: {slug}")


@router.get("/cities", summary="可选城市与本地时间")
def env_cities(
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """看板城市选择器数据源（仅启用中的城市）。"""
    items = list(service.city_index().values())
    return {
        "total": len(items),
        "items": items,
        "default": [item["slug"] for item in items],
    }


@router.get("/metrics", summary="可查询指标目录")
def env_metrics(service: EnvironmentService = Depends(get_environment_service)) -> dict:
    """融合宽表的字段字典视图，含单位、值域、是否可对比。"""
    items = service.metric_catalog()
    return {
        "total": len(items),
        "items": items,
        "core": [item["name"] for item in items if item["core"]],
        "comparable": [item["name"] for item in items if item["comparable"]],
    }


@router.get("/current", summary="城市实时环境实况")
def current(
    cities: str | None = Query(None, description="逗号分隔的城市 slug；为空表示全部"),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """每个城市的最新一条实况记录（无实况时退化为最新预报）及预期警。"""
    slugs = _resolve_cities(cities, service)
    items = service.current_conditions(slugs)
    return {
        "total": len(items),
        "items": items,
        "has_data": bool(items),
        "note": None if items else "尚无融合宽表数据，请先执行采集管道",
    }


@router.get("/hourly", summary="逐小时环境时序")
def hourly(
    city: str = Query(..., description="城市 slug"),
    hours: int = Query(72, ge=1, le=720),
    metrics: str | None = Query(None, description="逗号分隔的指标名"),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """单城市逐小时序列，返回实况/预报分界时刻。"""
    _assert_city(city, service)
    metric_list = [item.strip() for item in metrics.split(",") if item.strip()] if metrics else None
    return service.hourly_series(city, hours=hours, metrics=metric_list)


@router.get("/daily", summary="逐日环境汇总")
def daily(
    city: str = Query(..., description="城市 slug"),
    days: int = Query(30, ge=1, le=365),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """单城市日汇总序列与区间统计。"""
    _assert_city(city, service)
    return service.daily_series(city, days=days)


@router.get("/compare", summary="多城市指标对比")
def compare(
    cities: str = Query(..., description="逗号分隔的城市 slug，至少 2 个"),
    metric: str = Query("european_aqi", description="对比指标"),
    hours: int = Query(168, ge=1, le=2160),
    aggregate: str = Query("hourly", pattern="^(hourly|daily)$"),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """同一指标的多城市曲线与统计对比。"""
    slugs = _resolve_cities(cities, service) or []
    if len(slugs) < 2:
        raise HTTPException(status_code=400, detail="多城市对比至少需要 2 个城市")
    return service.compare(slugs, metric=metric, hours=hours, aggregate=aggregate)


@router.get("/ranking", summary="空气质量与气象排名")
def ranking(
    limit: int = Query(20, ge=1, le=100),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """当前最差/最佳空气质量、最高气温、最大降水排名。"""
    return service.ranking(limit=limit)


@router.get("/alerts", summary="环境预警")
def alerts(
    cities: str | None = Query(None, description="逗号分隔的城市 slug"),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """当前触发阈值规则的城市清单。"""
    slugs = _resolve_cities(cities, service)
    return service.alerts(city_slugs=slugs)


@router.get("/trend", summary="历史趋势（归档资产）")
def trend(
    city: str = Query(..., description="城市 slug"),
    days: int = Query(90, ge=7, le=1825),
    metric: str = Query("temperature_2m"),
    window: str = Query("day", pattern="^(day|month)$"),
    service: EnvironmentService = Depends(get_environment_service),
) -> dict:
    """基于 ERA5 历史归档资产的长期趋势，用于气候基准对照。"""
    _assert_city(city, service)
    return service.archive_trend(city, days=days, metric=metric, window=window)
