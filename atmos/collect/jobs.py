"""ETL 作业实现。

作业分两类
----------
* **采集作业**（async）：调用 Open-Meteo 把外部数据落到原始层资产；
* **派生作业**（sync）：读取数据湖内多个上游资产，加工出精炼层/服务层资产。

每个作业都是「按城市独立执行 + 单城市失败隔离」：一个城市失败只影响该城市，
其余城市照常落湖，整体状态记为 ``partial``，从而不让偶发的上游抖动
演变成整条管道的中断。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from atmos.analytics.query import LakeQuery
from atmos.catalog.definitions import AssetRegistry
from atmos.errors import SourceError
from atmos.lake.writer import LakeWriter, WriteReport
from atmos.logging_setup import get_logger
from atmos.models.enums import RunStatus, TriggerType
from atmos.settings import Settings, get_settings
from atmos.sources.openmeteo import OpenMeteoClient, FetchRequest, FetchResult, local_now, make_request
from atmos.sources.specs import (
    AIR_QUALITY_HOURLY_VARIABLES,
    ARCHIVE_HOURLY_VARIABLES,
    ASSET_AIR_QUALITY_HOURLY,
    ASSET_ARCHIVE_HOURLY,
    ASSET_ENVIRONMENT_DAILY,
    ASSET_ENVIRONMENT_HOURLY,
    ASSET_FORECAST_DAILY,
    ASSET_FORECAST_HOURLY,
    ASSET_GEOCODING_PLACES,
    WEATHER_DAILY_VARIABLES,
    WEATHER_HOURLY_VARIABLES,
)
from atmos.standards import (
    CHINA_GOOD_CEILING,
    GOOD_AQI_CEILING,
    aqi_level,
    china_aqi,
    comfort_index,
    is_stagnant,
    primary_pollutant,
    ventilation_index,
)
from atmos.utils import iso_z, new_run_id, utcnow

logger = get_logger("collect.jobs")


# ==================================================================
# 结果结构
# ==================================================================
@dataclass
class CityOutcome:
    """单城市在单作业中的执行结果。"""

    city_slug: str
    status: str = RunStatus.SUCCESS.value
    rows_written: int = 0
    rows_received: int = 0
    bytes_written: int = 0
    api_calls: int = 0
    partitions_written: int = 0
    duplicates_removed: int = 0
    latest_data_time: datetime | None = None
    error_type: str | None = None
    error_message: str | None = None
    schema_drift: dict[str, list[str]] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == RunStatus.FAILED.value

    @classmethod
    def from_report(cls, report: WriteReport) -> CityOutcome:
        return cls(
            city_slug=report.city_slug,
            status=RunStatus.SUCCESS.value,
            rows_written=report.rows_written,
            rows_received=report.rows_received,
            bytes_written=report.bytes_written,
            partitions_written=report.partitions_written,
            duplicates_removed=report.duplicates_removed,
            latest_data_time=report.latest_data_time,
            schema_drift={
                "missing": report.missing_columns,
                "extra": report.extra_columns,
            }
            if report.has_schema_drift
            else {},
        )


@dataclass
class JobOutcome:
    """单作业的汇总结果。"""

    job: str
    asset_key: str | None
    run_id: str
    trigger: str
    status: str = RunStatus.SUCCESS.value
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    cities: list[CityOutcome] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def rows_written(self) -> int:
        return sum(item.rows_written for item in self.cities)

    @property
    def rows_received(self) -> int:
        return sum(item.rows_received for item in self.cities)

    @property
    def bytes_written(self) -> int:
        return sum(item.bytes_written for item in self.cities)

    @property
    def api_calls(self) -> int:
        return sum(item.api_calls for item in self.cities)

    @property
    def partitions_written(self) -> int:
        return sum(item.partitions_written for item in self.cities)

    @property
    def duplicates_removed(self) -> int:
        return sum(item.duplicates_removed for item in self.cities)

    @property
    def failed_cities(self) -> list[CityOutcome]:
        return [item for item in self.cities if item.failed]

    @property
    def latest_data_time(self) -> datetime | None:
        stamps = [item.latest_data_time for item in self.cities if item.latest_data_time]
        return max(stamps) if stamps else None

    def finalize(self, *, empty_is_skipped: bool = False) -> JobOutcome:
        """收尾并推导整体状态。"""
        self.finished_at = utcnow()
        failed = len(self.failed_cities)
        total = len(self.cities)
        if total == 0:
            self.status = RunStatus.SKIPPED.value
        elif failed == 0:
            self.status = (
                RunStatus.SKIPPED.value
                if empty_is_skipped and self.rows_received == 0
                else RunStatus.SUCCESS.value
            )
        elif failed == total:
            self.status = RunStatus.FAILED.value
        else:
            self.status = RunStatus.PARTIAL.value
        return self

    def to_payload(self) -> dict[str, Any]:
        return {
            "job": self.job,
            "asset_key": self.asset_key,
            "run_id": self.run_id,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": iso_z(self.started_at),
            "finished_at": iso_z(self.finished_at),
            "duration_ms": self.duration_ms,
            "rows_received": self.rows_received,
            "rows_written": self.rows_written,
            "bytes_written": self.bytes_written,
            "api_calls": self.api_calls,
            "partitions_written": self.partitions_written,
            "duplicates_removed": self.duplicates_removed,
            "city_total": len(self.cities),
            "city_failed": len(self.failed_cities),
            "notes": self.notes,
        }


@dataclass
class PipelineReport:
    """一次管道运行的完整报告。"""

    run_id: str
    trigger: str
    scope: str
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    jobs: list[JobOutcome] = field(default_factory=list)

    @property
    def duration_ms(self) -> int | None:
        if self.finished_at is None:
            return None
        return int((self.finished_at - self.started_at).total_seconds() * 1000)

    @property
    def status(self) -> str:
        statuses = {job.status for job in self.jobs}
        if not statuses:
            return RunStatus.SKIPPED.value
        if statuses == {RunStatus.FAILED.value}:
            return RunStatus.FAILED.value
        if RunStatus.FAILED.value in statuses or RunStatus.PARTIAL.value in statuses:
            return RunStatus.PARTIAL.value
        if statuses == {RunStatus.SKIPPED.value}:
            return RunStatus.SKIPPED.value
        return RunStatus.SUCCESS.value

    @property
    def total_rows_written(self) -> int:
        return sum(job.rows_written for job in self.jobs)

    @property
    def total_api_calls(self) -> int:
        return sum(job.api_calls for job in self.jobs)

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "trigger": self.trigger,
            "scope": self.scope,
            "status": self.status,
            "started_at": iso_z(self.started_at),
            "finished_at": iso_z(self.finished_at),
            "duration_ms": self.duration_ms,
            "total_rows_written": self.total_rows_written,
            "total_api_calls": self.total_api_calls,
            "jobs": [job.to_payload() for job in self.jobs],
        }


# ==================================================================
# 作业上下文
# ==================================================================
@dataclass
class JobContext:
    """作业执行上下文。"""

    registry: AssetRegistry
    settings: Settings
    run_id: str
    trigger: str = TriggerType.MANUAL.value
    cities: list[Any] = field(default_factory=list)
    writer: LakeWriter = field(default_factory=LakeWriter)
    query: LakeQuery = field(default_factory=LakeQuery)

    def new_shared_run_id(self) -> str:
        return new_run_id("atmos")


# ==================================================================
# 采集作业
# ==================================================================
async def collect_geocoding(ctx: JobContext, client: OpenMeteoClient) -> JobOutcome:
    """采集城市地理编码参考数据。

    参考资产不按城市分区（落在 ``city=global`` 单一分区），因此运行记录
    也应当是**一条**：各城市的取数成败汇总进这条记录，避免重复计账。
    本作业不覆盖 ``cities.yaml`` 中经人工确认的城市主数据坐标，
    只用数据源返回的候选结果核对口径偏差。
    """
    outcome = JobOutcome(
        job="collect_geocoding",
        asset_key=ASSET_GEOCODING_PLACES,
        run_id=ctx.run_id,
        trigger=ctx.trigger,
    )
    asset = ctx.registry.asset(ASSET_GEOCODING_PLACES)
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    far_matches: list[dict[str, Any]] = []
    api_calls = 0

    for city in ctx.cities:
        try:
            result = await client.geocode(city.name_en or city.name_zh, count=1)
        except SourceError as exc:
            api_calls += 1
            logger.warning("城市 %s 地理编码失败: %s", city.slug, exc)
            failures.append({"city": city.slug, "error": f"{type(exc).__name__}: {exc}"})
            continue

        api_calls += result.api_calls
        match = _closest(result.rows, city.latitude, city.longitude)
        distance = _distance_km(city.latitude, city.longitude, match) if match else None
        if distance is not None and distance >= 100:
            far_matches.append({"city": city.slug, "distance_km": distance})

        rows.append(
            {
                "slug": city.slug,
                "name_zh": city.name_zh,
                "name_en": city.name_en,
                "country": city.country,
                "country_code": city.country_code,
                "admin1": city.admin1 or (match or {}).get("admin1"),
                "latitude": city.latitude,
                "longitude": city.longitude,
                "elevation": city.elevation
                if city.elevation is not None
                else (match or {}).get("elevation"),
                "timezone": city.timezone,
                "population": city.population
                if city.population is not None
                else (match or {}).get("population"),
                "climate_zone": city.climate_zone,
            }
        )

    report = ctx.writer.write(asset, "global", rows, run_id=ctx.run_id)
    batch = CityOutcome.from_report(report)
    batch.city_slug = "global"
    batch.api_calls = api_calls
    batch.detail = {
        "resolved_cities": len(rows),
        "failed_cities": failures,
        "far_matches": far_matches,
    }
    if failures:
        batch.status = (
            RunStatus.FAILED.value if len(failures) == len(ctx.cities) else RunStatus.PARTIAL.value
        )
    outcome.cities.append(batch)
    return outcome.finalize()


async def collect_weather_forecast(ctx: JobContext, client: OpenMeteoClient) -> JobOutcome:
    """采集逐小时天气（预报 + 近实时实况）。"""
    return await _collect_per_city(
        ctx,
        client,
        job="collect_weather_forecast",
        asset_key=ASSET_FORECAST_HOURLY,
        fetcher=lambda request: client.weather_forecast(request),
        decorator=_stamp_forecast_flag,
    )


async def collect_weather_daily(ctx: JobContext, client: OpenMeteoClient) -> JobOutcome:
    """采集逐日天气汇总。"""
    return await _collect_per_city(
        ctx,
        client,
        job="collect_weather_daily",
        asset_key=ASSET_FORECAST_DAILY,
        fetcher=lambda request: client.weather_daily(request),
        time_key="date",
    )


async def collect_air_quality(ctx: JobContext, client: OpenMeteoClient) -> JobOutcome:
    """采集逐小时空气质量。"""
    return await _collect_per_city(
        ctx,
        client,
        job="collect_air_quality",
        asset_key=ASSET_AIR_QUALITY_HOURLY,
        fetcher=lambda request: client.air_quality(request),
    )


async def collect_weather_archive(ctx: JobContext, client: OpenMeteoClient) -> JobOutcome:
    """采集历史再分析气象数据。"""
    lookback = ctx.settings.archive_lookback_days
    end_date = date.today() - timedelta(days=1)
    start_date = end_date - timedelta(days=lookback)

    def fetcher(request: FetchRequest) -> Any:
        return client.weather_archive(request, start_date=start_date, end_date=end_date)

    return await _collect_per_city(
        ctx,
        client,
        job="collect_weather_archive",
        asset_key=ASSET_ARCHIVE_HOURLY,
        fetcher=fetcher,
        notes=[f"回补区间 {start_date.isoformat()} → {end_date.isoformat()}"],
    )


# ---------- 采集作业通用骨架 ----------
async def _collect_per_city(
    ctx: JobContext,
    client: OpenMeteoClient,
    *,
    job: str,
    asset_key: str,
    fetcher: Callable[[FetchRequest], Any],
    decorator: Callable[[list[dict[str, Any]], Any], list[dict[str, Any]]] | None = None,
    time_key: str = "time",
    notes: list[str] | None = None,
) -> JobOutcome:
    """按城市采集 → 落湖 → 汇总，单城市失败隔离。"""
    asset = ctx.registry.asset(asset_key)
    outcome = JobOutcome(
        job=job, asset_key=asset_key, run_id=ctx.run_id, trigger=ctx.trigger, notes=list(notes or [])
    )

    for city in ctx.cities:
        request = make_request(city)
        try:
            result: FetchResult = await fetcher(request)
        except SourceError as exc:
            logger.warning("[%s] 城市 %s 采集失败: %s", job, city.slug, exc)
            outcome.cities.append(
                CityOutcome(
                    city_slug=city.slug,
                    status=RunStatus.FAILED.value,
                    api_calls=1,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )
            )
            continue

        rows = result.rows
        if decorator is not None:
            rows = decorator(rows, city)

        # 数据源统一用 time 字段，日粒度资产在字段字典中改名为 date
        if time_key != "time":
            for row in rows:
                if "time" in row and time_key not in row:
                    row[time_key] = row.pop("time")

        if not rows:
            outcome.cities.append(
                CityOutcome(
                    city_slug=city.slug,
                    status=RunStatus.SUCCESS.value,
                    api_calls=result.api_calls,
                    detail={"note": "数据源返回空结果"},
                )
            )
            continue

        report = ctx.writer.write(asset, city.slug, rows, run_id=ctx.run_id)
        city_outcome = CityOutcome.from_report(report)
        city_outcome.api_calls = result.api_calls
        _attach_time_bounds(city_outcome, rows, time_key)
        outcome.cities.append(city_outcome)

    return outcome.finalize(empty_is_skipped=True)


def _stamp_forecast_flag(rows: list[dict[str, Any]], city: Any) -> list[dict[str, Any]]:
    """标注每条记录是实况还是预报（按城市本地时间）。"""
    now_local = local_now(city.timezone)
    for row in rows:
        stamp = row.get("time")
        row["is_forecast"] = int(bool(stamp) and str(stamp) > now_local.strftime("%Y-%m-%dT%H:%M"))
    return rows


def _attach_time_bounds(outcome: CityOutcome, rows: list[dict[str, Any]], time_key: str) -> None:
    stamps = [row.get(time_key) for row in rows if row.get(time_key)]
    if not stamps:
        return
    try:
        outcome.latest_data_time = datetime.fromisoformat(str(max(stamps)))
    except ValueError:  # pragma: no cover
        outcome.latest_data_time = None


def _closest(
    candidates: list[dict[str, Any]], latitude: float, longitude: float
) -> dict[str, Any] | None:
    """挑选与目标坐标最近的候选地点。"""
    best: dict[str, Any] | None = None
    best_distance = float("inf")
    for item in candidates:
        distance = _distance_km(latitude, longitude, item)
        if distance is not None and distance < best_distance:
            best, best_distance = item, distance
    return best


def _distance_km(latitude: float, longitude: float, candidate: dict[str, Any] | None) -> float | None:
    """球面近似距离（km）。"""
    if not candidate:
        return None
    try:
        other_lat = float(candidate["latitude"])
        other_lon = float(candidate["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    from math import asin, cos, radians, sin, sqrt

    d_lat = radians(other_lat - latitude)
    d_lon = radians(other_lon - longitude)
    a = sin(d_lat / 2) ** 2 + cos(radians(latitude)) * cos(radians(other_lat)) * sin(d_lon / 2) ** 2
    return round(2 * 6371.0 * asin(sqrt(a)), 2)


# ==================================================================
# 派生作业
# ==================================================================
def build_city_environment_hourly(ctx: JobContext) -> JobOutcome:
    """把天气与空气质量融合为城市环境宽表，并派生业务指标。

    气象要素来自**两个上游**：
    * ``forecast.hourly`` —— 近实时实况 + 未来 7 天预报，带实况/预报标志；
    * ``archive.hourly``  —— ERA5 历史再分析，可回溯 180 天。

    空气质量能回溯 92 天，而预报接口只提供 9 天天气。若只用预报做融合，
    历史上 90% 以上的行会缺失风速/降水等要素，"气象—空气质量关系"分析
    就只剩极少数样本。因此按时间对齐后**以预报优先、归档兜底**合并气象列。
    """
    asset = ctx.registry.asset(ASSET_ENVIRONMENT_HOURLY)
    outcome = JobOutcome(
        job="build_city_environment_hourly",
        asset_key=ASSET_ENVIRONMENT_HOURLY,
        run_id=ctx.run_id,
        trigger=ctx.trigger,
    )

    weather_asset = ctx.registry.asset(ASSET_FORECAST_HOURLY)
    archive_asset = ctx.registry.asset(ASSET_ARCHIVE_HOURLY)
    air_asset = ctx.registry.asset(ASSET_AIR_QUALITY_HOURLY)

    # 只投影融合宽表真正声明的列：既显式表达设计意图，
    # 也避免把上游列带进落湖流程而被误报为 schema 漂移。
    declared = set(asset.column_names)
    forecast_columns = [c.name for c in weather_asset.data_columns if c.name in declared]
    archive_columns = [c.name for c in archive_asset.data_columns if c.name in declared]
    air_columns = [c.name for c in air_asset.data_columns if c.name in declared]

    for city in ctx.cities:
        forecast = ctx.query.fetch(
            ASSET_FORECAST_HOURLY,
            city_slug=city.slug,
            columns=forecast_columns,
            time_column="time",
        )
        archive = ctx.query.fetch(
            ASSET_ARCHIVE_HOURLY,
            city_slug=city.slug,
            columns=archive_columns,
            time_column="time",
        )
        air = ctx.query.fetch(
            ASSET_AIR_QUALITY_HOURLY,
            city_slug=city.slug,
            columns=air_columns,
            time_column="time",
        )
        if forecast.empty and archive.empty and air.empty:
            outcome.cities.append(
                CityOutcome(
                    city_slug=city.slug,
                    status=RunStatus.SUCCESS.value,
                    detail={"note": "上游无数据，跳过"},
                )
            )
            continue

        weather = _combine_weather(forecast, archive)
        merged = _merge_sources(weather, air)
        if merged.empty:
            outcome.cities.append(
                CityOutcome(city_slug=city.slug, status=RunStatus.SUCCESS.value)
            )
            continue

        merged = _derive_environment_columns(merged)
        rows = merged.to_dict("records")
        report = ctx.writer.write(asset, city.slug, rows, run_id=ctx.run_id)
        city_outcome = CityOutcome.from_report(report)
        city_outcome.detail = {
            "forecast_rows": int(len(forecast)),
            "archive_rows": int(len(archive)),
            "air_rows": int(len(air)),
            "weather_rows": int(len(weather)),
            "joined_rows": int(len(merged)),
        }
        outcome.cities.append(city_outcome)

    return outcome.finalize(empty_is_skipped=True)


def build_city_environment_daily(ctx: JobContext) -> JobOutcome:
    """把逐小时融合宽表聚合为城市环境日汇总。"""
    asset = ctx.registry.asset(ASSET_ENVIRONMENT_DAILY)
    outcome = JobOutcome(
        job="build_city_environment_daily",
        asset_key=ASSET_ENVIRONMENT_DAILY,
        run_id=ctx.run_id,
        trigger=ctx.trigger,
    )

    for city in ctx.cities:
        frame = ctx.query.fetch(ASSET_ENVIRONMENT_HOURLY, city_slug=city.slug, time_column="time")
        if frame.empty:
            outcome.cities.append(
                CityOutcome(
                    city_slug=city.slug,
                    status=RunStatus.SUCCESS.value,
                    detail={"note": "上游无数据，跳过"},
                )
            )
            continue

        aggregated = _aggregate_daily(frame)
        if aggregated.empty:
            outcome.cities.append(
                CityOutcome(city_slug=city.slug, status=RunStatus.SUCCESS.value)
            )
            continue

        rows = aggregated.to_dict("records")
        report = ctx.writer.write(asset, city.slug, rows, run_id=ctx.run_id)
        city_outcome = CityOutcome.from_report(report)
        city_outcome.detail = {"days": int(len(aggregated))}
        outcome.cities.append(city_outcome)

    return outcome.finalize(empty_is_skipped=True)


def _combine_weather(forecast: pd.DataFrame, archive: pd.DataFrame) -> pd.DataFrame:
    """合并两路气象来源：同一时刻以预报侧为准，缺测由归档侧补齐。

    不是简单拼接：预报表带 ``is_forecast`` 等归档没有的列，而归档覆盖更长历史。
    逐列 combine_first 才能既保留预报侧字段、又用归档填满历史时段。
    """
    if forecast.empty:
        if archive.empty:
            return archive
        # 归档独有：显式补上实况标志，避免融合宽表出现空值
        return archive.assign(is_forecast=0)
    if archive.empty:
        return forecast

    merged = archive.merge(
        forecast, on=["city_id", "time"], how="outer", suffixes=("_arc", "_fc")
    )
    forecast_only = [c for c in forecast.columns if c not in ("city_id", "time")]
    for column in forecast_only:
        left, right = f"{column}_arc", f"{column}_fc"
        if left in merged.columns and right in merged.columns:
            merged[column] = merged[right].combine_first(merged[left])
            merged = merged.drop(columns=[left, right])
        elif right in merged.columns:
            merged = merged.rename(columns={right: column})

    for column in [c for c in archive.columns if c not in ("city_id", "time")]:
        suffixed = f"{column}_arc"
        if suffixed in merged.columns:
            merged = merged.rename(columns={suffixed: column})

    # 归档时段没有预报标志，按实况处理
    if "is_forecast" in merged.columns:
        merged["is_forecast"] = pd.to_numeric(merged["is_forecast"], errors="coerce").fillna(0)
    return merged


def _merge_sources(weather: pd.DataFrame, air: pd.DataFrame) -> pd.DataFrame:
    """按 (city_id, time) 全外连接天气与空气质量。"""
    frames = [frame for frame in (weather, air) if not frame.empty]
    if not frames:
        return pd.DataFrame()
    if len(frames) == 1:
        return frames[0].copy()
    merged = frames[0].merge(
        frames[1],
        on=["city_id", "time"],
        how="outer",
        suffixes=("_weather", "_air"),
    )
    # 合并同名列（两侧都有的字段理论上不存在，除主键外；防御性处理）
    for column in list(merged.columns):
        if not column.endswith(("_weather", "_air")):
            continue
        base = column.rsplit("_", 1)[0]
        if base in merged.columns:
            continue
        counterpart = f"{base}_{'air' if column.endswith('_weather') else 'weather'}"
        if counterpart in merged.columns:
            merged[base] = merged[column].combine_first(merged[counterpart])
            merged = merged.drop(columns=[column, counterpart])
    return merged.sort_values("time", kind="stable")


def _derive_environment_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """派生空气质量等级、首要污染物、舒适度、通风、静稳与**国标 AQI**。

    国标 AQI（GB 3095-2012）对 PM 使用 24 小时滑动平均、对 O₃ 使用 8 小时滑动平均，
    因此必须先按城市做滑动窗口再算 IAQI，不能用瞬时小时浓度直接套断点。
    """
    frame = frame.sort_values(["city_id", "time"], kind="stable").copy()

    aqi = pd.to_numeric(frame.get("european_aqi"), errors="coerce")
    frame["aqi_level"] = aqi.map(lambda value: aqi_level(None if pd.isna(value) else float(value)))

    # ---- 国标 AQI：滑动平均 → IAQI ----
    grouped = frame.groupby("city_id", group_keys=False)
    rolling_inputs: dict[str, str] = {}
    for column, window in (("pm2_5", 24), ("pm10", 24)):
        if column in frame.columns:
            name = f"_roll{window}_{column}"
            frame[name] = grouped[column].transform(
                lambda series, size=window: pd.to_numeric(series, errors="coerce").rolling(
                    size, min_periods=size
                ).mean()
            )
            rolling_inputs[column] = name
    if "ozone" in frame.columns:
        frame["_roll8_ozone"] = grouped["ozone"].transform(
            lambda series: pd.to_numeric(series, errors="coerce").rolling(8, min_periods=8).mean()
        )
        rolling_inputs["ozone"] = "_roll8_ozone"
    for column in ("nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide"):
        if column in frame.columns:
            rolling_inputs[column] = column

    if rolling_inputs:
        china_rows = frame[list(rolling_inputs.values())].to_dict("records")
        computed = [
            china_aqi({key: _num(row.get(source)) for key, source in rolling_inputs.items()})
            for row in china_rows
        ]
        frame["china_aqi"] = [item[0] for item in computed]
        frame["china_aqi_level"] = [item[1] or None for item in computed]
        frame["china_primary_pollutant"] = [item[2] for item in computed]
        frame = frame.drop(columns=[name for name in rolling_inputs.values() if name.startswith("_")])

    # ---- 首要污染物（欧洲口径的相对负荷）----
    pollutant_columns = [
        "pm2_5",
        "pm10",
        "ozone",
        "nitrogen_dioxide",
        "sulphur_dioxide",
        "carbon_monoxide",
    ]
    available = [name for name in pollutant_columns if name in frame.columns]
    if available:
        frame["primary_pollutant"] = [
            primary_pollutant(
                {name: _num(row.get(name)) for name in available}
            )
            for row in frame[available].to_dict("records")
        ]

    frame["comfort_index"] = [
        comfort_index(
            _num(row.get("temperature_2m")),
            _num(row.get("relative_humidity_2m")),
            _num(row.get("wind_speed_10m")),
        )
        for row in frame.to_dict("records")
    ]
    frame["ventilation_index"] = [
        ventilation_index(_num(row.get("wind_speed_10m"))) for row in frame.to_dict("records")
    ]
    frame["is_stagnant"] = [
        is_stagnant(_num(row.get("wind_speed_10m")), _num(row.get("precipitation")))
        for row in frame.to_dict("records")
    ]
    return frame


def _aggregate_daily(frame: pd.DataFrame) -> pd.DataFrame:
    """按城市 × 自然日聚合，并计算**国标日 AQI**。

    国标日 AQI 由当日各污染物的 24 小时平均浓度计算（O₃ 取日最大 8 小时滑动平均），
    这正是 HJ 633-2012 的规定方法，因此日汇总上的 AQI 与官方考核口径可比。
    """
    frame = frame.copy()
    frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
    frame = frame.dropna(subset=["time"])
    if frame.empty:
        return pd.DataFrame()
    frame["date"] = frame["time"].dt.date

    aqi = pd.to_numeric(frame.get("european_aqi"), errors="coerce")
    frame["_aqi"] = aqi
    frame["_china_aqi"] = pd.to_numeric(frame.get("china_aqi"), errors="coerce")
    frame["_temp"] = pd.to_numeric(frame.get("temperature_2m"), errors="coerce")
    frame["_exceed"] = (aqi > GOOD_AQI_CEILING).astype("float64").where(aqi.notna())
    frame["_stagnant"] = pd.to_numeric(frame.get("is_stagnant"), errors="coerce")
    frame["_obs"] = frame["_temp"].notna().astype("float64")
    # 臭氧按国标取"日最大 8 小时滑动平均"，不能直接用小时值的日均
    if "ozone" in frame.columns:
        frame["_ozone_8h"] = (
            frame.sort_values(["city_id", "time"])
            .groupby("city_id")["ozone"]
            .transform(lambda series: pd.to_numeric(series, errors="coerce").rolling(8, min_periods=8).mean())
        )

    grouped = frame.groupby(["city_id", "date"], as_index=False)
    aggregations: dict[str, tuple[str, str]] = {
        "temp_avg": ("_temp", "mean"),
        "temp_max": ("_temp", "max"),
        "temp_min": ("_temp", "min"),
        "humidity_avg": ("relative_humidity_2m", "mean"),
        "precipitation_sum": ("precipitation", "sum"),
        "wind_speed_avg": ("wind_speed_10m", "mean"),
        "pressure_avg": ("pressure_msl", "mean"),
        "aqi_avg": ("_aqi", "mean"),
        "aqi_max": ("_aqi", "max"),
        "pm2_5_avg": ("pm2_5", "mean"),
        "pm10_avg": ("pm10", "mean"),
        "exceed_hours": ("_exceed", "sum"),
        "stagnant_hours": ("_stagnant", "sum"),
        "obs_hours": ("_obs", "sum"),
    }
    for extra in ("nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide"):
        if extra in frame.columns:
            aggregations[f"{extra}_avg"] = (extra, "mean")
    if "_ozone_8h" in frame.columns:
        aggregations["ozone_8h_max"] = ("_ozone_8h", "max")

    aggregated = grouped.agg(**{name: pd.NamedAgg(column=col, aggfunc=func) for name, (col, func) in aggregations.items()})

    # 国标日 AQI：由当日 24 小时平均浓度计算
    china_inputs = {
        "pm2_5": "pm2_5_avg",
        "pm10": "pm10_avg",
        "ozone": "ozone_8h_max",
        "nitrogen_dioxide": "nitrogen_dioxide_avg",
        "sulphur_dioxide": "sulphur_dioxide_avg",
        "carbon_monoxide": "carbon_monoxide_avg",
    }
    available = {key: col for key, col in china_inputs.items() if col in aggregated.columns}
    if available:
        computed = [
            china_aqi({key: _num(row.get(col)) for key, col in available.items()})
            for row in aggregated.to_dict("records")
        ]
        aggregated["china_aqi"] = [item[0] for item in computed]
        aggregated["china_aqi_level"] = [item[1] or None for item in computed]
        aggregated["china_primary_pollutant"] = [item[2] for item in computed]
        # 优良天按国标 AQI ≤ 100 判定（与官方考核口径一致）
        aggregated["good_air_day"] = aggregated["china_aqi"].map(
            lambda value: None if pd.isna(value) else int(float(value) <= CHINA_GOOD_CEILING)
        )
    else:
        aggregated["china_aqi"] = None
        aggregated["china_aqi_level"] = None
        aggregated["china_primary_pollutant"] = None
        aggregated["good_air_day"] = None

    aggregated["dominant_aqi_level"] = aggregated["aqi_max"].map(
        lambda value: aqi_level(None if pd.isna(value) else float(value))
    )
    for column in ("exceed_hours", "stagnant_hours", "obs_hours"):
        aggregated[column] = (
            pd.to_numeric(aggregated[column], errors="coerce").round().astype("Int64")
        )
    return aggregated.sort_values(["city_id", "date"], kind="stable")


def _num(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return None if numeric != numeric else numeric


# ==================================================================
# 作业清单（供 runner 按依赖顺序调度）
# ==================================================================
@dataclass(frozen=True)
class JobSpec:
    """作业注册项。"""

    name: str
    asset_key: str | None
    stage: str  # reference | collect | derive
    runner: str
    description: str = ""


JOB_SPECS: tuple[JobSpec, ...] = (
    JobSpec(
        "collect_geocoding",
        ASSET_GEOCODING_PLACES,
        "reference",
        "async",
        "采集城市地理编码参考数据，作为血缘最上游的维度资产。",
    ),
    JobSpec(
        "collect_weather_forecast",
        ASSET_FORECAST_HOURLY,
        "collect",
        "async",
        "采集逐小时天气（预报与近实时实况），标注实况/预报标志。",
    ),
    JobSpec(
        "collect_weather_daily",
        ASSET_FORECAST_DAILY,
        "collect",
        "async",
        "采集逐日天气汇总（极值、日照、辐射、蒸散）。",
    ),
    JobSpec(
        "collect_air_quality",
        ASSET_AIR_QUALITY_HOURLY,
        "collect",
        "async",
        "采集逐小时空气质量（污染物浓度与欧美双口径 AQI）。",
    ),
    JobSpec(
        "collect_weather_archive",
        ASSET_ARCHIVE_HOURLY,
        "collect",
        "async",
        "回补历史再分析气象数据。",
    ),
    JobSpec(
        "build_city_environment_hourly",
        ASSET_ENVIRONMENT_HOURLY,
        "derive",
        "sync",
        "融合天气与空气质量，派生等级、首要污染物、舒适度与静稳指标。",
    ),
    JobSpec(
        "build_city_environment_daily",
        ASSET_ENVIRONMENT_DAILY,
        "derive",
        "sync",
        "按城市 × 自然日聚合，产出考核口径的服务层资产。",
    ),
)

SYNC_JOB_RUNNERS: dict[str, Callable[[JobContext], JobOutcome]] = {
    "build_city_environment_hourly": build_city_environment_hourly,
    "build_city_environment_daily": build_city_environment_daily,
}

ASYNC_JOB_RUNNERS: dict[str, Callable[[JobContext, OpenMeteoClient], Any]] = {
    "collect_geocoding": collect_geocoding,
    "collect_weather_forecast": collect_weather_forecast,
    "collect_weather_daily": collect_weather_daily,
    "collect_air_quality": collect_air_quality,
    "collect_weather_archive": collect_weather_archive,
}


__all__ = [
    "CityOutcome",
    "JobOutcome",
    "PipelineReport",
    "JobContext",
    "JobSpec",
    "JOB_SPECS",
    "SYNC_JOB_RUNNERS",
    "ASYNC_JOB_RUNNERS",
]
