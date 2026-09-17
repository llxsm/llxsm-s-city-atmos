"""环境数据查询服务。

面向业务语义的读模型：实时实况、时序曲线、多城市对比、排名、预警、历史趋势。
所有查询都建立在融合宽表 ``atmos.city_environment.hourly`` 与其日汇总之上，
从而保证看板、API、导出三处口径完全一致。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from atmos.analytics.query import LakeQuery
from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.logging_setup import get_logger
from atmos.models.enums import AssetStatus
from atmos.settings import Settings, get_settings
from atmos.sources.openmeteo import local_now
from atmos.sources.specs import (
    ASSET_AIR_QUALITY_HOURLY,
    ASSET_ARCHIVE_HOURLY,
    ASSET_ENVIRONMENT_DAILY,
    ASSET_ENVIRONMENT_HOURLY,
    ASSET_FORECAST_HOURLY,
)
from atmos.standards import (
    AQI_HEALTH_ADVICE,
    GOOD_AQI_CEILING,
    aqi_level,
    evaluate_alerts,
    weather_label,
)
from atmos.utils import iso_z, safe_ratio

logger = get_logger("analytics.environment")

# 看板默认展示的核心指标（融合宽表列名）
CORE_METRICS: tuple[str, ...] = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "pressure_msl",
    "pm2_5",
    "pm10",
    "ozone",
    "nitrogen_dioxide",
    "sulphur_dioxide",
    "carbon_monoxide",
    "european_aqi",
    "us_aqi",
    "aqi_level",
    "primary_pollutant",
    "comfort_index",
    "ventilation_index",
    "is_stagnant",
)

# 多城市对比与排名可用的数值指标
NUMERIC_METRICS: tuple[str, ...] = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation",
    "wind_speed_10m",
    "pressure_msl",
    "cloud_cover",
    "pm2_5",
    "pm10",
    "ozone",
    "nitrogen_dioxide",
    "sulphur_dioxide",
    "carbon_monoxide",
    "european_aqi",
    "us_aqi",
    "comfort_index",
    "ventilation_index",
)


class EnvironmentService:
    """环境数据读模型。"""

    def __init__(
        self,
        registry: AssetRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry or load_registry()
        self.settings = settings or get_settings()
        self.query = LakeQuery(self.settings)

    # ==============================================================
    # 元数据
    # ==============================================================
    def city_index(self) -> dict[str, dict[str, Any]]:
        """可选城市 slug → 城市信息（含本地时间）。

        以**资产目录**为准（bootstrap 把 ``config/cities.yaml`` 投影进来），
        这样停用的城市会立即从看板选择器与全部环境接口中消失，
        而不是等到下一次重启；目录尚未引导时回退到配置定义，
        保证零配置首次启动也能工作。
        """
        payloads = self._catalog_cities()
        if not payloads:
            payloads = [city.to_payload() for city in self.registry.active_cities]

        result: dict[str, dict[str, Any]] = {}
        for info in payloads:
            enriched = dict(info)
            enriched["local_now"] = iso_z(local_now(str(info.get("timezone") or "UTC")))
            result[str(enriched["slug"])] = enriched
        return result

    def _catalog_cities(self) -> list[dict[str, Any]]:
        """从资产目录读取启用中的城市；失败时返回空列表以触发配置回退。"""
        from sqlalchemy import select
        from sqlalchemy.exc import SQLAlchemyError

        from atmos.db import session_scope
        from atmos.models import City

        try:
            with session_scope() as session:
                rows = list(
                    session.scalars(
                        select(City).where(City.is_active.is_(True)).order_by(City.slug)
                    ).all()
                )
                return [
                    {
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
                    for city in rows
                ]
        except SQLAlchemyError as exc:
            logger.warning("读取城市目录失败，回退到配置定义: %s", exc)
            return []

    def metric_catalog(self) -> list[dict[str, Any]]:
        """可查询指标目录（直接来自融合宽表的字段字典）。"""
        asset = self.registry.asset(ASSET_ENVIRONMENT_HOURLY)
        catalog: list[dict[str, Any]] = []
        numeric_types = {"double", "integer"}
        for column in asset.data_columns:
            if column.name in {"city_id", "time"}:
                continue
            catalog.append(
                {
                    "name": column.name,
                    "label": _metric_label(column.name, column.description),
                    "description": column.description,
                    "unit": column.unit,
                    "data_type": column.data_type,
                    "numeric": column.data_type in numeric_types,
                    "is_derived": column.is_derived,
                    "value_range": list(column.value_range) if column.value_range else None,
                    "core": column.name in CORE_METRICS,
                    "comparable": column.name in NUMERIC_METRICS,
                }
            )
        return catalog

    def availability(self) -> dict[str, Any]:
        """各资产的数据可用性（供前端禁用空图表）。"""
        from atmos.lake.layout import list_partitions

        assets = []
        for definition in self.registry.assets:
            if definition.status != AssetStatus.ACTIVE.value:
                continue
            cities = sorted(
                {str(item["city"]) for item in list_partitions(definition.asset_key, self.settings)}
            )
            assets.append(
                {
                    "asset_key": definition.asset_key,
                    "name": definition.name,
                    "layer": definition.layer,
                    "available": bool(cities),
                    "cities": cities,
                }
            )
        return {"assets": assets, "environment_ready": self.query.has_data(ASSET_ENVIRONMENT_HOURLY)}

    # ==============================================================
    # 实时实况
    # ==============================================================
    def current_conditions(
        self, city_slugs: list[str] | None = None
    ) -> list[dict[str, Any]]:
        """每个城市的最新一条环境记录。

        取值优先级：**实况（is_forecast=0）中最新的一条**；若该城市只有预报，
        则退化为预报中最新的一条。这样"当前状况"不会被未来预报值污染。
        """
        relation = self.query.relation_sql(ASSET_ENVIRONMENT_HOURLY)
        if relation is None:
            return []

        sql = f"""
            WITH ranked AS (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY city_id
                           ORDER BY is_forecast ASC, time DESC
                       ) AS _rank
                FROM {relation}
            )
            SELECT * EXCLUDE (_rank) FROM ranked WHERE _rank = 1
        """
        frame = self.query.execute(sql)
        if frame.empty:
            return []
        if city_slugs:
            frame = frame[frame["city_id"].isin(city_slugs)]

        cities = self.city_index()
        payloads: list[dict[str, Any]] = []
        for record in frame.to_dict("records"):
            slug = str(record.get("city_id"))
            city = cities.get(slug, {})
            row = {key: _clean(value) for key, value in record.items()}
            aqi = _num(record.get("european_aqi"))
            row.update(
                {
                    "city_slug": slug,
                    "city_name": city.get("name_zh", slug),
                    "city_name_en": city.get("name_en", slug),
                    "country": city.get("country"),
                    "latitude": city.get("latitude"),
                    "longitude": city.get("longitude"),
                    "timezone": city.get("timezone"),
                    "observed_at": iso_z(_as_datetime(record.get("time"))),
                    "weather_label": weather_label(_num(record.get("weather_code"))),
                    "aqi_level": aqi_level(aqi),
                    "aqi_color": _aqi_color(aqi),
                    "health_advice": AQI_HEALTH_ADVICE.get(aqi_level(aqi) or "", ""),
                    "is_forecast": int(record.get("is_forecast") or 0),
                    "data_kind": "预报" if int(record.get("is_forecast") or 0) else "实况",
                    "alerts": evaluate_alerts(_numeric_view(record)),
                }
            )
            payloads.append(row)

        payloads.sort(key=lambda item: (-(_num(item.get("european_aqi")) or 0), item["city_slug"]))
        return payloads

    # ==============================================================
    # 时序
    # ==============================================================
    def hourly_series(
        self,
        city_slug: str,
        *,
        hours: int = 72,
        metrics: list[str] | None = None,
        include_forecast: bool = True,
    ) -> dict[str, Any]:
        """逐小时时序（默认返回最近 N 小时至未来预报）。"""
        metrics = _sanitize_metrics(metrics) or ["temperature_2m", "european_aqi", "pm2_5", "pm10"]
        city = self.city_index().get(city_slug)
        reference = _as_datetime(city["local_now"]) if city else datetime.utcnow()
        start = reference - timedelta(hours=hours)
        end = reference + timedelta(hours=max(0, hours))

        columns = ["time", "is_forecast", "weather_code", *metrics]
        frame = self.query.fetch(
            ASSET_ENVIRONMENT_HOURLY,
            city_slug=city_slug,
            columns=[name for name in columns if name],
            time_column="time",
            start=start,
            end=end,
            order_by="time",
        )
        return {
            "city_slug": city_slug,
            "city_name": (city or {}).get("name_zh", city_slug),
            "timezone": (city or {}).get("timezone"),
            "reference_time": iso_z(reference),
            "metrics": metrics,
            "points": _frame_to_points(frame, metrics),
            "boundary": _forecast_boundary(frame),
        }

    def daily_series(self, city_slug: str, *, days: int = 30) -> dict[str, Any]:
        """逐日汇总时序。"""
        city = self.city_index().get(city_slug)
        reference = _as_datetime(city["local_now"]).date() if city else date.today()
        start = reference - timedelta(days=days)

        frame = self.query.fetch(
            ASSET_ENVIRONMENT_DAILY,
            city_slug=city_slug,
            time_column="date",
            start=datetime.combine(start, datetime.min.time()),
            end=datetime.combine(reference, datetime.min.time()),
            order_by="date",
        )
        metrics = [
            "temp_avg",
            "temp_max",
            "temp_min",
            "aqi_avg",
            "aqi_max",
            "pm2_5_avg",
            "pm10_avg",
            "precipitation_sum",
            "exceed_hours",
        ]
        for record in frame.to_dict("records") if not frame.empty else []:
            record["aqi_level"] = aqi_level(_num(record.get("aqi_max")))
        return {
            "city_slug": city_slug,
            "city_name": (city or {}).get("name_zh", city_slug),
            "metrics": metrics,
            "points": _frame_to_points(frame, metrics + ["aqi_level"]),
            "summary": _daily_summary(frame),
        }

    # ==============================================================
    # 多城市对比与排名
    # ==============================================================
    def compare(
        self,
        city_slugs: list[str],
        *,
        metric: str = "european_aqi",
        hours: int = 168,
        aggregate: str = "hourly",
    ) -> dict[str, Any]:
        """多城市同指标对比。

        ``aggregate='daily'`` 时按日聚合均值，适合拉长周期观察趋势差异。
        """
        if metric not in NUMERIC_METRICS:
            metric = "european_aqi"
        reference = datetime.utcnow()
        start = reference - timedelta(hours=hours)

        frame = self.query.environments(
            ASSET_ENVIRONMENT_HOURLY,
            city_slugs=city_slugs,
            columns=["city_id", "time", metric],
            start=start,
            order_by="time",
        )
        cities = self.city_index()
        if frame.empty:
            return {
                "metric": metric,
                "metric_label": _metric_label(metric, ""),
                "unit": self._unit_of(metric),
                "aggregate": aggregate,
                "series": [],
                "statistics": [],
            }

        frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
        frame = frame.dropna(subset=["time"])
        if aggregate == "daily":
            frame["bucket"] = frame["time"].dt.date.astype(str)
        else:
            frame["bucket"] = frame["time"].dt.strftime("%Y-%m-%dT%H:00")

        series: list[dict[str, Any]] = []
        for slug, group in frame.groupby("city_id"):
            grouped = group.groupby("bucket")[metric].mean()
            series.append(
                {
                    "city_slug": slug,
                    "city_name": cities.get(str(slug), {}).get("name_zh", str(slug)),
                    "points": [
                        {"time": str(index), "value": _clean(value)}
                        for index, value in grouped.items()
                    ],
                }
            )

        statistics = []
        for slug, group in frame.groupby("city_id"):
            values = pd.to_numeric(group[metric], errors="coerce").dropna()
            if values.empty:
                continue
            statistics.append(
                {
                    "city_slug": slug,
                    "city_name": cities.get(str(slug), {}).get("name_zh", str(slug)),
                    "avg": round(float(values.mean()), 2),
                    "max": round(float(values.max()), 2),
                    "min": round(float(values.min()), 2),
                    "p95": round(float(values.quantile(0.95)), 2),
                    "samples": int(len(values)),
                }
            )
        statistics.sort(key=lambda item: -item["avg"])

        return {
            "metric": metric,
            "metric_label": _metric_label(metric, ""),
            "unit": self._unit_of(metric),
            "aggregate": aggregate,
            "window_hours": hours,
            "series": sorted(series, key=lambda item: item["city_slug"]),
            "statistics": statistics,
        }

    def ranking(self, *, limit: int = 20) -> dict[str, Any]:
        """当前空气质量与气象排名。"""
        conditions = self.current_conditions()
        ranked = [item for item in conditions if _num(item.get("european_aqi")) is not None]
        ranked.sort(key=lambda item: _num(item.get("european_aqi")) or 0, reverse=True)

        def brief(item: dict[str, Any]) -> dict[str, Any]:
            return {
                "city_slug": item["city_slug"],
                "city_name": item["city_name"],
                "european_aqi": item.get("european_aqi"),
                "aqi_level": item.get("aqi_level"),
                "aqi_color": item.get("aqi_color"),
                "pm2_5": item.get("pm2_5"),
                "pm10": item.get("pm10"),
                "temperature_2m": item.get("temperature_2m"),
                "weather_label": item.get("weather_label"),
                "observed_at": item.get("observed_at"),
            }

        hottest = sorted(
            [item for item in conditions if _num(item.get("temperature_2m")) is not None],
            key=lambda item: _num(item.get("temperature_2m")) or -999,
            reverse=True,
        )[:limit]
        wettest = sorted(
            [item for item in conditions if _num(item.get("precipitation")) is not None],
            key=lambda item: _num(item.get("precipitation")) or 0,
            reverse=True,
        )[:limit]

        return {
            "worst_air": [brief(item) for item in ranked[:limit]],
            "best_air": [brief(item) for item in list(reversed(ranked))[:limit]],
            "hottest": [brief(item) for item in hottest],
            "wettest": [brief(item) for item in wettest],
            "city_count": len(conditions),
        }

    def alerts(self, *, city_slugs: list[str] | None = None) -> dict[str, Any]:
        """当前生效的环境预警。"""
        conditions = self.current_conditions(city_slugs)
        items: list[dict[str, Any]] = []
        for condition in conditions:
            for alert in condition.get("alerts", []):
                items.append(
                    {
                        "city_slug": condition["city_slug"],
                        "city_name": condition["city_name"],
                        "observed_at": condition["observed_at"],
                        **alert,
                    }
                )
        order = {"severe": 0, "warning": 1, "info": 2}
        items.sort(key=lambda item: (order.get(str(item["severity"]), 9), item["city_name"]))
        severities: dict[str, int] = {}
        for item in items:
            severities[str(item["severity"])] = severities.get(str(item["severity"]), 0) + 1
        return {
            "total": len(items),
            "by_severity": severities,
            "alerts": items,
            "cities_monitored": len(conditions),
        }

    # ==============================================================
    # 历史趋势（归档资产）
    # ==============================================================
    def archive_trend(
        self,
        city_slug: str,
        *,
        days: int = 90,
        metric: str = "temperature_2m",
        window: str = "day",
    ) -> dict[str, Any]:
        """基于历史再分析资产的长期趋势。"""
        from atmos.analytics.query import is_safe_identifier

        if not is_safe_identifier(metric):
            raise ValueError(f"非法指标名: {metric}")
        end = date.today() - timedelta(days=6)
        start = end - timedelta(days=days)

        relation = self.query.relation_sql(ASSET_ARCHIVE_HOURLY, city_slug)
        if relation is None:
            return {
                "city_slug": city_slug,
                "metric": metric,
                "points": [],
                "available": False,
                "note": "尚无历史归档数据，请执行 collect_weather_archive 作业",
            }

        bucket = "CAST(time AS DATE)" if window == "day" else "date_trunc('month', time)"
        sql = f"""
            SELECT {bucket} AS bucket, AVG("{metric}") AS value, COUNT(*) AS samples
            FROM {relation}
            WHERE city_id = '{city_slug}' AND time >= TIMESTAMP '{start} 00:00:00'
            GROUP BY 1 ORDER BY 1
        """
        frame = self.query.execute(sql)
        city = self.city_index().get(city_slug, {})
        return {
            "city_slug": city_slug,
            "city_name": city.get("name_zh", city_slug),
            "metric": metric,
            "metric_label": _metric_label(metric, ""),
            "unit": self._unit_of(metric, fallback_asset=ASSET_ARCHIVE_HOURLY),
            "window": window,
            "range": [start.isoformat(), end.isoformat()],
            "available": not frame.empty,
            "points": [
                {"time": str(row["bucket"]), "value": _clean(row["value"]), "samples": int(row["samples"])}
                for _, row in frame.iterrows()
            ],
        }

    # ==============================================================
    # 资产样例与统计（目录页）
    # ==============================================================
    def asset_sample(
        self, asset_key: str, *, city_slug: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        """资产样例数据。"""
        if not self.registry.has_asset(asset_key):
            return {"asset_key": asset_key, "rows": [], "columns": []}
        definition = self.registry.asset(asset_key)
        frame = self.query.fetch(
            asset_key,
            city_slug=city_slug,
            time_column=definition.time_column or None,
            order_by=definition.time_column or None,
            limit=limit,
            descending=True,
        )
        if frame.empty:
            return {"asset_key": asset_key, "rows": [], "columns": []}
        if definition.time_column and definition.time_column in frame.columns:
            frame = frame.sort_values(definition.time_column, kind="stable")
        return {
            "asset_key": asset_key,
            "city_slug": city_slug,
            "columns": list(frame.columns),
            "rows": [
                {key: _clean(value) for key, value in record.items()}
                for record in frame.to_dict("records")
            ],
        }

    def asset_profiling(self, asset_key: str, *, city_slug: str | None = None) -> dict[str, Any]:
        """列级画像：空值率、唯一值数、极值。供目录详情页的"数据预览"使用。"""
        if not self.registry.has_asset(asset_key):
            return {"asset_key": asset_key, "columns": []}
        definition = self.registry.asset(asset_key)
        frame = self.query.fetch(
            asset_key,
            city_slug=city_slug,
            time_column=definition.time_column or None,
        )
        if frame.empty:
            return {"asset_key": asset_key, "city_slug": city_slug, "row_count": 0, "columns": []}

        total = len(frame)
        profiles: list[dict[str, Any]] = []
        for column in definition.columns:
            if column.name not in frame.columns:
                continue
            series = frame[column.name]
            nulls = int(series.isna().sum())
            entry: dict[str, Any] = {
                "name": column.name,
                "data_type": column.data_type,
                "unit": column.unit,
                "null_count": nulls,
                "null_rate": round(safe_ratio(nulls, total) * 100, 3),
                "distinct": int(series.nunique(dropna=True)),
            }
            numeric = pd.to_numeric(series, errors="coerce").dropna()
            if not numeric.empty and column.data_type in {"double", "integer"}:
                entry.update(
                    {
                        "min": round(float(numeric.min()), 4),
                        "max": round(float(numeric.max()), 4),
                        "mean": round(float(numeric.mean()), 4),
                        "p50": round(float(numeric.quantile(0.5)), 4),
                        "std": round(float(numeric.std() or 0.0), 4),
                        "out_of_range": _count_out_of_range(numeric, column.value_range),
                    }
                )
            elif column.enum_values:
                counts = series.dropna().astype(str).value_counts().head(5)
                entry["top_values"] = [
                    {"value": str(index), "count": int(value)} for index, value in counts.items()
                ]
            profiles.append(entry)

        return {
            "asset_key": asset_key,
            "city_slug": city_slug,
            "row_count": total,
            "columns": profiles,
        }

    # ---------- 辅助 ----------
    def _unit_of(self, metric: str, *, fallback_asset: str = ASSET_ENVIRONMENT_HOURLY) -> str | None:
        for asset_key in (fallback_asset, ASSET_ENVIRONMENT_HOURLY, ASSET_ARCHIVE_HOURLY, ASSET_FORECAST_HOURLY, ASSET_AIR_QUALITY_HOURLY):
            if not self.registry.has_asset(asset_key):
                continue
            column = self.registry.asset(asset_key).column(metric)
            if column is not None:
                return column.unit
        return None


# ==================================================================
# 模块级辅助
# ==================================================================
def _sanitize_metrics(metrics: list[str] | None) -> list[str]:
    if not metrics:
        return []
    allowed = set(CORE_METRICS) | set(NUMERIC_METRICS) | {"weather_code"}
    return [name for name in metrics if name in allowed]


def _frame_to_points(frame: pd.DataFrame, metrics: list[str]) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    points: list[dict[str, Any]] = []
    for record in frame.to_dict("records"):
        point: dict[str, Any] = {
            "time": _as_time_label(record.get("time") or record.get("date")),
        }
        if "is_forecast" in record:
            point["is_forecast"] = int(record.get("is_forecast") or 0)
        for name in metrics:
            if name in record:
                point[name] = _clean(record[name])
        points.append(point)
    return points


def _as_time_label(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _forecast_boundary(frame: pd.DataFrame) -> dict[str, Any] | None:
    """实况与预报的分界时刻，供前端在图上去分。"""
    if frame.empty or "is_forecast" not in frame.columns:
        return None
    stamps = pd.to_datetime(frame["time"], errors="coerce")
    forecast = pd.to_numeric(frame["is_forecast"], errors="coerce").fillna(0) > 0
    if not forecast.any() or forecast.all():
        return None
    boundary = stamps[forecast].min()
    return {"time": boundary.isoformat() if pd.notna(boundary) else None}


def _daily_summary(frame: pd.DataFrame) -> dict[str, Any]:
    if frame.empty:
        return {}
    aqi = pd.to_numeric(frame.get("aqi_avg"), errors="coerce").dropna()
    temp = pd.to_numeric(frame.get("temp_avg"), errors="coerce").dropna()
    rain = pd.to_numeric(frame.get("precipitation_sum"), errors="coerce").dropna()
    exceed = pd.to_numeric(frame.get("exceed_hours"), errors="coerce").dropna()
    good_days = int(frame["good_air_day"].fillna(0).astype(float).sum()) if "good_air_day" in frame else None
    return {
        "days": int(len(frame)),
        "aqi_avg": round(float(aqi.mean()), 2) if not aqi.empty else None,
        "aqi_max": round(float(aqi.max()), 2) if not aqi.empty else None,
        "temp_avg": round(float(temp.mean()), 2) if not temp.empty else None,
        "precipitation_total": round(float(rain.sum()), 2) if not rain.empty else None,
        "exceed_hours_total": int(exceed.sum()) if not exceed.empty else None,
        "good_air_days": good_days,
        "good_air_rate": round(safe_ratio(good_days or 0, len(frame)) * 100, 1) if good_days is not None else None,
        "best_day": _extreme_day(frame, "aqi_avg", best=True),
        "worst_day": _extreme_day(frame, "aqi_avg", best=False),
    }


def _extreme_day(frame: pd.DataFrame, column: str, *, best: bool) -> dict[str, Any] | None:
    if column not in frame.columns or frame.empty:
        return None
    values = pd.to_numeric(frame[column], errors="coerce")
    valid = frame.loc[values.notna()]
    if valid.empty:
        return None
    index = pd.to_numeric(valid[column], errors="coerce").idxmin() if best else pd.to_numeric(
        valid[column], errors="coerce"
    ).idxmax()
    row = valid.loc[index]
    return {
        "date": str(row.get("date")),
        "aqi_avg": _clean(row.get(column)),
        "aqi_level": aqi_level(_num(row.get("aqi_avg"))),
    }


def _numeric_view(record: dict[str, Any]) -> dict[str, float | None]:
    return {key: _num(value) for key, value in record.items()}


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return None if numeric != numeric else numeric


def _clean(value: Any) -> Any:
    """把 pandas/arrow 标量转成 JSON 友好的原生类型。"""
    if value is None:
        return None
    if isinstance(value, float) and value != value:
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except (ValueError, AttributeError):  # pragma: no cover
            return str(value)
    if isinstance(value, pd.Timedelta):
        return str(value)
    return value


def _as_datetime(value: Any) -> datetime:
    if value is None:
        return datetime.utcnow()
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()


def _aqi_color(value: float | None) -> str:
    level = aqi_level(value)
    from atmos.models.enums import AQILevel

    for item in AQILevel:
        if item.value == level:
            return item.color
    return "#94a3b8"


def _count_out_of_range(values: pd.Series, value_range: list[float] | None) -> int | None:
    if not value_range or len(value_range) != 2:
        return None
    low, high = float(value_range[0]), float(value_range[1])
    return int(((values < low) | (values > high)).sum())


def _metric_label(name: str, description: str) -> str:
    """指标中文短标签：优先取字段描述的首段，回退到列名。"""
    if description:
        head = description.split("（")[0].split("(")[0].strip()
        if head:
            return head
    return name


def current_city_slugs(registry: AssetRegistry | None = None) -> list[str]:
    """全部启用城市的 slug。"""
    registry = registry or load_registry()
    return [city.slug for city in registry.active_cities]


__all__ = [
    "EnvironmentService",
    "CORE_METRICS",
    "NUMERIC_METRICS",
    "current_city_slugs",
]
