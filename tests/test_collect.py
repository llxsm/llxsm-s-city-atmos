"""采集管道测试。

用 ``httpx.MockTransport`` 替换真实网络：构造与 Open-Meteo 同构的响应，
从而在不触网的前提下端到端验证「取数 → 落湖 → 派生 → 运行记录」全链路，
并覆盖限流重试与契约破坏等异常路径。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from atmos.collect.runner import Collector
from atmos.db import session_scope
from atmos.models import CollectionRun, DataAsset
from atmos.sources.openmeteo import FetchRequest, OpenMeteoClient
from atmos.sources.specs import (
    AIR_QUALITY_HOURLY_VARIABLES,
    ARCHIVE_HOURLY_VARIABLES,
    ASSET_ENVIRONMENT_DAILY,
    ASSET_ENVIRONMENT_HOURLY,
    ASSET_FORECAST_HOURLY,
    WEATHER_DAILY_VARIABLES,
    WEATHER_HOURLY_VARIABLES,
)

BASE = (datetime.now() - timedelta(hours=6)).replace(minute=0, second=0, microsecond=0)

# 整型 / 枚举类变量：不施加漂移，保持离散取值合法
INTEGRAL_VARIABLES = frozenset({"weather_code", "is_day", "is_forecast"})

# 各变量的合理基准值：让模拟数据通过值域与枚举校验，
# 从而能断言"健康数据 → 高质量分"，而不只是"不崩溃"。
VALUE_BASE: dict[str, float] = {
    "temperature_2m": 20.0,
    "relative_humidity_2m": 55.0,
    "dew_point_2m": 10.0,
    "apparent_temperature": 21.0,
    "precipitation": 0.0,
    "rain": 0.0,
    "snowfall": 0.0,
    "precipitation_probability": 20.0,
    "weather_code": 1.0,
    "cloud_cover": 40.0,
    "pressure_msl": 1012.0,
    "surface_pressure": 1005.0,
    "wind_speed_10m": 12.0,
    "wind_direction_10m": 180.0,
    "wind_gusts_10m": 22.0,
    "is_day": 1.0,
    "visibility": 20000.0,
    "uv_index": 3.0,
    # 逐日气象
    "temperature_2m_max": 24.0,
    "temperature_2m_min": 12.0,
    "apparent_temperature_max": 25.0,
    "apparent_temperature_min": 11.0,
    "precipitation_sum": 0.0,
    "rain_sum": 0.0,
    "snowfall_sum": 0.0,
    "precipitation_hours": 0.0,
    "precipitation_probability_max": 20.0,
    "wind_speed_10m_max": 20.0,
    "wind_gusts_10m_max": 35.0,
    "wind_direction_10m_dominant": 200.0,
    "shortwave_radiation_sum": 20.0,
    "et0_fao_evapotranspiration": 3.0,
    "uv_index_max": 6.0,
    "sunshine_duration": 30000.0,
    "daylight_duration": 40000.0,
    # 历史归档
    "snow_depth": 0.0,
    "shortwave_radiation": 200.0,
    # 空气质量
    "pm2_5": 20.0,
    "pm10": 35.0,
    "carbon_monoxide": 300.0,
    "nitrogen_dioxide": 25.0,
    "sulphur_dioxide": 8.0,
    "ozone": 60.0,
    "ammonia": 4.0,
    "dust": 5.0,
    "aerosol_optical_depth": 0.2,
    "uv_index_clear_sky": 3.5,
    "european_aqi": 25.0,
    "european_aqi_pm2_5": 25.0,
    "european_aqi_pm10": 20.0,
    "european_aqi_nitrogen_dioxide": 15.0,
    "european_aqi_ozone": 18.0,
    "european_aqi_sulphur_dioxide": 5.0,
    "us_aqi": 60.0,
    "us_aqi_pm2_5": 60.0,
    "us_aqi_pm10": 40.0,
    "us_aqi_nitrogen_dioxide": 30.0,
    "us_aqi_ozone": 35.0,
    "us_aqi_sulphur_dioxide": 10.0,
    "us_aqi_carbon_monoxide": 12.0,
    "alder_pollen": 0.0,
    "birch_pollen": 0.0,
    "grass_pollen": 0.0,
    "mugwort_pollen": 0.0,
    "olive_pollen": 0.0,
    "ragweed_pollen": 0.0,
}


def _value_for(name: str, index: int, *, drift: float) -> float:
    """按基准值 + 轻微漂移生成模拟观测值。

    整型/枚举类字段不施加漂移，否则会产出非法枚举值（例如 weather_code=1.1）。
    """
    base = VALUE_BASE.get(name, 1.0)
    if name in INTEGRAL_VARIABLES:
        return float(int(base))
    return round(base + index * drift, 4)


def _hourly_payload(variables: tuple[str, ...], count: int) -> dict:
    times = [(BASE + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M") for index in range(count)]
    payload: dict = {"time": times}
    for name in variables:
        payload[name] = [
            _value_for(name, index, drift=0.0 if name in INTEGRAL_VARIABLES else 0.1)
            for index in range(count)
        ]
    return payload


def _daily_payload(variables: tuple[str, ...], count: int) -> dict:
    times = [(BASE + timedelta(days=index)).strftime("%Y-%m-%d") for index in range(count)]
    payload: dict = {"time": times}
    for name in variables:
        # 日粒度不引入漂移，保证 min ≤ max 恒成立
        payload[name] = [_value_for(name, 0, drift=0.0)] * count
    return payload


def base_handler(request: httpx.Request) -> httpx.Response:
    """Open-Meteo 同构响应的路由实现。"""
    path = request.url.path
    query = dict(request.url.params)
    if path.endswith("/v1/forecast") and "hourly" in query:
        body = _hourly_payload(WEATHER_HOURLY_VARIABLES, 4)
        units = {name: "u" for name in WEATHER_HOURLY_VARIABLES}
        return httpx.Response(
            200,
            json={
                "latitude": 39.9,
                "longitude": 116.4,
                "timezone": "Asia/Shanghai",
                "hourly": body,
                "hourly_units": units,
            },
        )
    if path.endswith("/v1/forecast") and "daily" in query:
        body = _daily_payload(WEATHER_DAILY_VARIABLES, 3)
        return httpx.Response(
            200,
            json={
                "timezone": "Asia/Shanghai",
                "daily": body,
                "daily_units": {name: "u" for name in WEATHER_DAILY_VARIABLES},
            },
        )
    if path.endswith("/v1/air-quality"):
        body = _hourly_payload(AIR_QUALITY_HOURLY_VARIABLES, 4)
        return httpx.Response(
            200,
            json={
                "timezone": "Asia/Shanghai",
                "hourly": body,
                "hourly_units": {name: "u" for name in AIR_QUALITY_HOURLY_VARIABLES},
            },
        )
    if path.endswith("/v1/archive"):
        body = _hourly_payload(ARCHIVE_HOURLY_VARIABLES, 4)
        return httpx.Response(
            200,
            json={
                "timezone": "Asia/Shanghai",
                "hourly": body,
                "hourly_units": {name: "u" for name in ARCHIVE_HOURLY_VARIABLES},
            },
        )
    if path.endswith("/v1/search"):
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "name": "Beijing",
                        "latitude": 39.9075,
                        "longitude": 116.397,
                        "elevation": 49.0,
                        "country": "China",
                        "country_code": "CN",
                        "admin1": "Beijing",
                        "timezone": "Asia/Shanghai",
                        "population": 11716620,
                    }
                ]
            },
        )
    return httpx.Response(404, json={"error": True, "reason": "not found"})


def make_transport(*, fail_times: int = 0, status: int = 200) -> httpx.MockTransport:
    """构造带可配置失败次数的模拟传输层。"""
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= fail_times:
            return httpx.Response(
                status_code=status,
                headers={"Retry-After": "0"} if status == 429 else {},
                json={"error": True, "reason": "模拟失败"},
            )
        return base_handler(request)

    return httpx.MockTransport(handler)


REQUEST = FetchRequest(
    city_slug="beijing", latitude=39.9042, longitude=116.4074, timezone="Asia/Shanghai"
)


# ==================================================================
# 客户端
# ==================================================================
@pytest.mark.asyncio
async def test_client_parses_hourly_response(settings) -> None:
    """列式响应应被正确转成行式记录。"""
    async with OpenMeteoClient(settings, transport=make_transport()) as client:
        result = await client.weather_forecast(REQUEST)

    assert len(result.rows) == 4
    assert result.variables
    assert "temperature_2m" in result.rows[0]
    assert result.rows[0]["time"] == BASE.strftime("%Y-%m-%dT%H:%M")
    assert result.meta["timezone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_client_retries_rate_limit(settings) -> None:
    """429 应触发退避重试并最终成功。"""
    async with OpenMeteoClient(settings, transport=make_transport(fail_times=2, status=429)) as client:
        result = await client.air_quality(REQUEST)
    assert len(result.rows) == 4
    assert client.api_calls >= 3


@pytest.mark.asyncio
async def test_client_retries_server_error(settings) -> None:
    async with OpenMeteoClient(settings, transport=make_transport(fail_times=1, status=503)) as client:
        result = await client.weather_daily(REQUEST)
    assert len(result.rows) == 3


@pytest.mark.asyncio
async def test_client_gives_up_after_max_retries(settings) -> None:
    """持续失败应在重试上限后抛错，而不是无限重试。"""
    from atmos.errors import SourceError

    async with OpenMeteoClient(
        settings, transport=make_transport(fail_times=99, status=503)
    ) as client:
        with pytest.raises(SourceError):
            await client.weather_forecast(REQUEST)


@pytest.mark.asyncio
async def test_client_reports_broken_contract(settings) -> None:
    """数据源返回空变量集应被视为契约破坏。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hourly": {"time": []}, "hourly_units": {}})

    from atmos.errors import SourceResponseError

    async with OpenMeteoClient(settings, transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceResponseError, match="契约"):
            await client.weather_forecast(REQUEST)


@pytest.mark.asyncio
async def test_client_skips_archive_inside_publication_lag(settings) -> None:
    """归档请求区间落在发布延迟内时应直接跳过，不消耗额度。"""
    async with OpenMeteoClient(settings, transport=make_transport()) as client:
        result = await client.weather_archive(
            REQUEST, start_date=datetime.now().date(), end_date=datetime.now().date()
        )
    assert result.rows == []
    assert result.api_calls == 0


@pytest.mark.asyncio
async def test_client_geocoding(settings) -> None:
    async with OpenMeteoClient(settings, transport=make_transport()) as client:
        result = await client.geocode("Beijing")
    assert result.rows[0]["country_code"] == "CN"
    assert result.rows[0]["timezone"] == "Asia/Shanghai"


# ==================================================================
# 管道
# ==================================================================
@pytest.mark.asyncio
async def test_full_pipeline_builds_all_assets(workspace, registry) -> None:
    """完整管道：参考 → 采集 → 派生，全部资产应落湖。"""
    collector = Collector(registry, transport=make_transport())
    report = await collector.run(scope="all", city_slugs=["beijing"], trigger="cli")

    assert report.status == "success", report.to_payload()
    assert len(report.jobs) == 7
    assert report.total_rows_written > 0
    assert report.total_api_calls == 5

    for key in (
        "openmeteo.geocoding.places",
        ASSET_FORECAST_HOURLY,
        "openmeteo.weather.forecast.daily",
        "openmeteo.weather.archive.hourly",
        "openmeteo.air_quality.hourly",
        ASSET_ENVIRONMENT_HOURLY,
        ASSET_ENVIRONMENT_DAILY,
    ):
        with session_scope() as session:
            asset = session.scalar(select(DataAsset).where(DataAsset.asset_key == key))
        assert asset is not None, key
        assert asset.row_count > 0, f"{key} 未落湖"
        assert asset.city_count >= 1, key
        if key != "openmeteo.geocoding.places":
            # 参考资产不含时间列，其 latest_data_time 理应为空
            assert asset.latest_data_time is not None, key


@pytest.mark.asyncio
async def test_pipeline_writes_run_records(workspace, registry) -> None:
    """每个城市每个作业都应产生一条运行记录。"""
    collector = Collector(registry, transport=make_transport())
    await collector.run(scope="all", city_slugs=["beijing"], trigger="cli")

    with session_scope() as session:
        total = session.scalar(select(func.count(CollectionRun.id)))
        successes = session.scalar(
            select(func.count(CollectionRun.id)).where(CollectionRun.status == "success")
        )
        jobs = {row[0] for row in session.execute(select(CollectionRun.job).distinct()).all()}

    assert total == 7
    assert successes == 7
    assert len(jobs) == 7


@pytest.mark.asyncio
async def test_pipeline_isolates_city_failures(workspace, registry) -> None:
    """单城市失败不应中断其他城市，整体状态记为 partial。"""
    state = {"triggered": False}

    def handler(request: httpx.Request) -> httpx.Response:
        query = dict(request.url.params)
        # 让上海的所有请求失败
        if abs(float(query.get("latitude", 0)) - 31.2304) < 0.01:
            state["triggered"] = True
            return httpx.Response(500, json={"error": True, "reason": "模拟故障"})
        return base_handler(request)

    collector = Collector(registry, transport=httpx.MockTransport(handler))
    report = await collector.run(scope="collect", city_slugs=["beijing", "shanghai"], trigger="cli")

    assert state["triggered"]
    assert report.status == "partial"
    failed_jobs = [job for job in report.jobs if job.status == "partial"]
    assert failed_jobs
    assert all(len(job.failed_cities) == 1 for job in failed_jobs)
    assert all(job.failed_cities[0].city_slug == "shanghai" for job in failed_jobs)


@pytest.mark.asyncio
async def test_pipeline_marks_forecast_flag(workspace, registry) -> None:
    """预报时段应被标注，实况时段不应被标注。"""
    from atmos.analytics.query import LakeQuery

    collector = Collector(registry, transport=make_transport())
    await collector.run(scope="collect", city_slugs=["beijing"], trigger="cli")

    frame = LakeQuery().fetch(ASSET_FORECAST_HOURLY, city_slug="beijing", time_column="time")
    assert "is_forecast" in frame.columns
    assert set(frame["is_forecast"].unique()) <= {0, 1}
    # 模拟数据固定在 2026-06-01，相对当前时间属于过去 → 全部为实况
    assert (frame["is_forecast"] == 0).all()


@pytest.mark.asyncio
async def test_derived_assets_contain_business_indicators(workspace, registry) -> None:
    """融合宽表应派生等级、首要污染物、舒适度等业务指标。"""
    from atmos.analytics.query import LakeQuery

    collector = Collector(registry, transport=make_transport())
    await collector.run(scope="all", city_slugs=["beijing"], trigger="cli")

    frame = LakeQuery().fetch(ASSET_ENVIRONMENT_HOURLY, city_slug="beijing", time_column="time")
    assert not frame.empty
    for column in ("aqi_level", "primary_pollutant", "comfort_index", "ventilation_index", "is_stagnant"):
        assert column in frame.columns, column
        assert frame[column].notna().any(), column

    daily = LakeQuery().fetch(ASSET_ENVIRONMENT_DAILY, city_slug="beijing", time_column="date")
    assert not daily.empty
    for column in ("temp_avg", "temp_max", "temp_min", "aqi_avg", "aqi_max", "obs_hours", "good_air_day"):
        assert column in daily.columns, column
    assert (daily["obs_hours"] <= 24).all()
    assert (daily["temp_min"] <= daily["temp_max"]).all()


@pytest.mark.asyncio
async def test_pipeline_is_idempotent(workspace, registry) -> None:
    """重复运行同一批次不应增加记录数。"""
    collector = Collector(registry, transport=make_transport())
    await collector.run(scope="all", city_slugs=["beijing"], trigger="cli")

    with session_scope() as session:
        before = session.scalar(
            select(DataAsset.row_count).where(DataAsset.asset_key == ASSET_ENVIRONMENT_HOURLY)
        )

    second = await collector.run(scope="all", city_slugs=["beijing"], trigger="cli")
    assert second.status == "success"
    assert second.jobs[5].duplicates_removed > 0

    with session_scope() as session:
        after = session.scalar(
            select(DataAsset.row_count).where(DataAsset.asset_key == ASSET_ENVIRONMENT_HOURLY)
        )
    assert before == after


@pytest.mark.asyncio
async def test_derive_scope_without_collected_data(workspace, registry) -> None:
    """仅派生范围内没有上游数据时应安全跳过。"""
    collector = Collector(registry, transport=make_transport())
    report = await collector.run(scope="derive", city_slugs=["beijing"], trigger="cli")
    assert report.status == "skipped"
    assert report.total_rows_written == 0


@pytest.mark.asyncio
async def test_collect_respects_city_limit(workspace, registry, monkeypatch) -> None:
    """城市上限应生效，保护数据源免费额度。"""
    monkeypatch.setenv("ATMOS_COLLECT_MAX_CITIES", "2")
    from atmos.settings import reload_settings

    reload_settings()
    collector = Collector(registry, transport=make_transport())
    cities = collector.resolve_cities()
    assert len(cities) == 2


@pytest.mark.asyncio
async def test_rate_limiter_enforces_minimum_interval() -> None:
    """限流器本身应保证相邻请求的间隔不低于配置值。

    直接对限流器做单元测试，避免把 httpx 首次请求的预热开销
    混进时序断言里造成偶发失败。
    """
    from atmos.sources.openmeteo import _RateLimiter

    limiter = _RateLimiter(80)
    started = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    elapsed = time.monotonic() - started
    # 3 次获取之间应发生 2 次 80ms 的间隔
    assert elapsed >= 0.15, elapsed


@pytest.mark.asyncio
async def test_rate_limiter_disabled_when_interval_zero() -> None:
    """间隔配置为 0 时不应引入任何等待。"""
    from atmos.sources.openmeteo import _RateLimiter

    limiter = _RateLimiter(0)
    started = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    assert time.monotonic() - started < 0.05


@pytest.mark.asyncio
async def test_quality_after_collection_produces_scores(workspace, registry) -> None:
    """采集后立即评测应产出五维评分。"""
    from atmos.db import session_scope
    from atmos.quality.engine import QualityEngine

    collector = Collector(registry, transport=make_transport())
    await collector.run(scope="all", city_slugs=["beijing"], trigger="cli")

    with session_scope() as session:
        reports = QualityEngine(registry).evaluate_all(session)

    assert len(reports) == 7
    environment_report = next(
        item for item in reports if item.asset_key == ASSET_ENVIRONMENT_HOURLY
    )
    assert environment_report.rows_evaluated > 0
    assert environment_report.dimensions["uniqueness"].score == 100.0
    assert environment_report.dimensions["validity"].score is not None


@pytest.mark.asyncio
async def test_export_after_collection(client_with_data, registry) -> None:
    """采集后两个平台的接口都应带出真实数据（分析侧看数据，治理侧看元数据）。"""
    analysis, governance = client_with_data

    # ---- 治理侧：资产目录与字段画像 ----
    detail = governance.get(f"/api/assets/{ASSET_FORECAST_HOURLY}").json()
    assert detail["row_count"] > 0
    assert detail["versions"]

    sample = governance.get(
        f"/api/assets/{ASSET_FORECAST_HOURLY}/sample", params={"city": "beijing"}
    ).json()
    assert sample["rows"]

    profiling = governance.get(
        f"/api/assets/{ASSET_FORECAST_HOURLY}/profiling", params={"city": "beijing"}
    ).json()
    assert profiling["row_count"] > 0
    temperature = next(item for item in profiling["columns"] if item["name"] == "temperature_2m")
    assert "mean" in temperature and "out_of_range" in temperature

    summary = governance.get("/api/quality/summary").json()
    assert summary["evaluated_assets"] == 7
    assert summary["average_overall"] is not None

    lineage = governance.get("/api/lineage/graph").json()
    forecast_node = next(
        node for node in lineage["nodes"] if node["key"] == ASSET_FORECAST_HOURLY
    )
    assert forecast_node["row_count"] > 0
    assert forecast_node["latest_grade"] in {"A", "B", "C", "D", "E"}

    # ---- 分析侧：数据下载与环境查询 ----
    csv_response = analysis.get(
        f"/api/export/{ASSET_FORECAST_HOURLY}.csv", params={"city": "beijing"}
    )
    assert int(csv_response.headers["x-row-count"]) > 0
    assert "temperature_2m" in csv_response.text.splitlines()[0]

    current = analysis.get("/api/env/current").json()
    assert current["has_data"] is True
    assert current["items"][0]["city_slug"] == "beijing"
    assert current["items"][0]["aqi_level"]

    hourly = analysis.get("/api/env/hourly", params={"city": "beijing", "hours": 24}).json()
    assert hourly["points"]
    assert "temperature_2m" in hourly["points"][0]

    daily = analysis.get("/api/env/daily", params={"city": "beijing", "days": 30}).json()
    assert daily["points"]
    assert daily["summary"]["days"] > 0


@pytest.fixture()
def client_with_data(workspace, registry):
    """跑完整管道 + 质量评测后，两个门户的 API 客户端。"""
    import asyncio

    from fastapi.testclient import TestClient

    from atmos.db import session_scope
    from atmos.quality.engine import QualityEngine

    asyncio.run(
        Collector(registry, transport=make_transport()).run(
            scope="all", city_slugs=["beijing"], trigger="cli"
        )
    )
    with session_scope() as session:
        QualityEngine(registry).evaluate_all(session)

    from atmos.apps.analysis.app import create_app as create_analysis
    from atmos.apps.governance.app import create_app as create_governance

    with TestClient(create_analysis()) as analysis, TestClient(create_governance()) as governance:
        yield analysis, governance


def test_mock_payload_shapes_are_json_serialisable() -> None:
    """模拟响应本身应可序列化，保证测试夹具可靠。"""
    payload = _hourly_payload(("a",), 1)
    assert json.loads(json.dumps(payload))["a"] == [1.0]


def test_mock_payload_passes_field_dictionary_validation() -> None:
    """模拟数据必须落在字段字典声明的值域与枚举内，否则测试会掩盖真实问题。"""
    from atmos.catalog.definitions import load_registry
    from atmos.sources.specs import ASSET_AIR_QUALITY_HOURLY, ASSET_FORECAST_DAILY

    registry = load_registry()
    samples = {
        ASSET_FORECAST_HOURLY: _hourly_payload(WEATHER_HOURLY_VARIABLES, 4),
        ASSET_FORECAST_DAILY: _daily_payload(WEATHER_DAILY_VARIABLES, 3),
        ASSET_AIR_QUALITY_HOURLY: _hourly_payload(AIR_QUALITY_HOURLY_VARIABLES, 4),
    }
    problems: list[str] = []
    for asset_key, payload in samples.items():
        asset = registry.asset(asset_key)
        for column in asset.columns:
            if column.name not in payload:
                continue
            for value in payload[column.name]:
                if column.value_range and not column.within_range(value):
                    problems.append(f"{asset_key}.{column.name}={value} 越界 {column.value_range}")
                if column.enum_values and value not in column.enum_values:
                    problems.append(f"{asset_key}.{column.name}={value} 非法枚举")
    assert problems == [], problems
