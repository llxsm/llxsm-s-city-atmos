"""质量检查项与评测引擎测试。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import pytest

from atmos.lake.writer import LakeWriter
from atmos.quality.checks import CheckContext, evaluator_for
from atmos.sources.specs import (
    ASSET_AIR_QUALITY_HOURLY,
    ASSET_ENVIRONMENT_DAILY,
    ASSET_ENVIRONMENT_HOURLY,
    ASSET_FORECAST_DAILY,
    ASSET_FORECAST_HOURLY,
)
from tests.conftest import air_rows, forecast_rows

NOW = datetime.now().replace(minute=0, second=0, microsecond=0)


def recent_rows(count: int, *, hours_back: int | None = None) -> list[dict]:
    """生成截至 ``NOW`` 的逐小时天气记录。"""
    span = hours_back if hours_back is not None else count - 1
    start = NOW - timedelta(hours=span)
    return forecast_rows(count, start=start.strftime("%Y-%m-%dT%H:%M"))


def recent_air(count: int = 10) -> list[dict]:
    """生成截至 ``NOW`` 的逐小时空气质量记录。"""
    start = NOW - timedelta(hours=count - 1)
    return air_rows(count, start=start.strftime("%Y-%m-%dT%H:%M"))


def recent_stamps(count: int) -> list[str]:
    """截至 ``NOW`` 的逐小时时间戳字符串。"""
    return [
        (NOW - timedelta(hours=count - 1 - index)).strftime("%Y-%m-%dT%H:%M")
        for index in range(count)
    ]


def recent_dates(count: int) -> list[str]:
    """截至 ``NOW`` 的逐日日期字符串。"""
    return [
        (NOW.date() - timedelta(days=count - 1 - index)).isoformat() for index in range(count)
    ]


def make_context(
    registry,
    asset_key: str,
    check_key: str,
    frame: pd.DataFrame,
    *,
    window_hours: int = 168,
    local_now: datetime | None = None,
    lake_schema: dict[str, str] | None = None,
    run_stats: dict[str, int] | None = None,
    params: dict | None = None,
) -> CheckContext:
    """构造检查上下文。

    ``params`` 用于在单个测试中覆盖规则参数（例如清空按资产配置的窗口偏移），
    从而把"检查机制本身"与"平台对某个资产的具体口径"分开测试。
    """
    import dataclasses

    asset = registry.asset(asset_key)
    check = next(item for item in registry.quality.checks if item.key == check_key)
    if params:
        check = dataclasses.replace(check, params={**check.params, **params})
    return CheckContext(
        asset=asset,
        check=check,
        frames={"beijing": frame} if frame is not None else {},
        city_local_now={"beijing": local_now or NOW},
        window_hours=window_hours,
        defaults=registry.quality.defaults,
        lake_schema=lake_schema or {},
        run_stats=run_stats or {},
    )


# 清空按资产配置的窗口偏移，用于纯机制测试
_NO_WINDOW_OVERRIDE = {"anchor_hours_by_asset": {}, "span_hours_by_asset": {}}


def run_check(registry, asset_key: str, check_key: str, frame, **kwargs):
    evaluator = evaluator_for(check_key)
    assert evaluator is not None, check_key
    return evaluator(make_context(registry, asset_key, check_key, frame, **kwargs))


# ==================================================================
# 完整性
# ==================================================================
def test_time_coverage_full_marks(registry) -> None:
    """窗口内数据齐全时时间覆盖应为满分（纯机制测试，不带资产级窗口偏移）。"""
    frame = pd.DataFrame(recent_rows(49))
    outcome = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "completeness.time_coverage",
        frame,
        window_hours=48,
        params=_NO_WINDOW_OVERRIDE,
    )
    assert outcome.status == "pass"
    assert outcome.score == pytest.approx(100.0, abs=0.5)


def test_time_coverage_half_marks(registry) -> None:
    """只覆盖一半窗口时应显著扣分并判为不通过。"""
    frame = pd.DataFrame(recent_rows(24))
    outcome = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "completeness.time_coverage",
        frame,
        window_hours=48,
        params=_NO_WINDOW_OVERRIDE,
    )
    assert outcome.score < 60
    assert outcome.status == "fail"
    assert outcome.detail["coverage_by_city"]["beijing"] == pytest.approx(50.0, abs=1.0)


def test_time_coverage_no_data(registry) -> None:
    """空窗口应得 0 分而不是抛错。"""
    outcome = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "completeness.time_coverage",
        pd.DataFrame(),
        window_hours=48,
        params=_NO_WINDOW_OVERRIDE,
    )
    assert outcome.score == 0.0
    assert outcome.status == "fail"


def test_time_coverage_accounts_for_source_publication_lag(registry) -> None:
    """归档资产的覆盖率窗口应扣除发布延迟，避免把"上游未发布"计为缺失。

    构造一段截止到 5 天前（即 ERA5 发布边界）的连续 48 小时数据：
    若不扣除延迟覆盖率应为 0%，扣除 120h 延迟后应为 100%。
    """
    from atmos.sources.specs import ASSET_ARCHIVE_HOURLY

    end = NOW - timedelta(hours=120)
    rows = forecast_rows(48, start=(end - timedelta(hours=47)).strftime("%Y-%m-%dT%H:%M"))
    frame = pd.DataFrame(rows)

    lagged = run_check(
        registry, ASSET_ARCHIVE_HOURLY, "completeness.time_coverage", frame, window_hours=48
    )
    assert lagged.detail["anchor_hours_applied"] == -120.0
    assert lagged.score == pytest.approx(100.0, abs=1.0)

    # 对照：清空窗口偏移后，同样的数据应判为 0% 覆盖
    unlagged = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "completeness.time_coverage",
        frame,
        window_hours=48,
        params=_NO_WINDOW_OVERRIDE,
    )
    assert unlagged.detail["anchor_hours_applied"] == 0.0
    assert unlagged.score == 0.0


def test_time_coverage_aligns_window_to_forecast_horizon(registry) -> None:
    """含预报时段的资产应把覆盖率窗口前移到未来，否则会误判为缺失。

    构造过去 48 小时 + 未来 168 小时的完整预报数据（共 216 小时）：
    默认窗口（过去 48h）只看得到 48/48，而资产声明的窗口是 [now-48h, now+168h]。
    """
    now = NOW
    start = now - timedelta(hours=48)
    rows = forecast_rows(216, start=start.strftime("%Y-%m-%dT%H:%M"))
    frame = pd.DataFrame(rows)

    outcome = run_check(
        registry, ASSET_FORECAST_HOURLY, "completeness.time_coverage", frame, window_hours=168
    )
    assert outcome.detail["anchor_hours_applied"] == 168.0
    assert outcome.detail["span_hours_applied"] == 216.0
    # 数据覆盖 [now-48h, now+168h]，与声明窗口完全对齐
    assert outcome.score == pytest.approx(100.0, abs=1.0)


def test_freshness_ignores_forecast_horizon(registry) -> None:
    """新鲜度不得被未来预报时段"刷成"负延迟。"""
    rows = recent_rows(24) + forecast_rows(
        48, start=(NOW + timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M")
    )
    outcome = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "timeliness.freshness",
        pd.DataFrame(rows),
        window_hours=168,
        local_now=NOW + timedelta(minutes=45),
    )
    assert outcome.observed == pytest.approx(45.0, abs=1.0)
    assert outcome.status == "pass"


def test_freshness_with_only_future_data_is_not_negative(registry) -> None:
    """仅有未来预报数据时延迟记为 0，而不是负数。"""
    rows = forecast_rows(12, start=(NOW + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M"))
    outcome = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "timeliness.freshness",
        pd.DataFrame(rows),
        window_hours=168,
        local_now=NOW,
    )
    assert outcome.observed == 0.0
    assert outcome.status == "pass"


def test_null_rate_penalises_missing_values(registry) -> None:
    """必需字段整列为空应被明确识别并显著扣分。"""
    rows = recent_rows(48)
    for row in rows:
        row["temperature_2m"] = None
    frame = pd.DataFrame(rows)
    outcome = run_check(registry, ASSET_FORECAST_HOURLY, "completeness.null_rate", frame, window_hours=48)

    assert outcome.detail["null_counts"]["temperature_2m"] == 48
    assert outcome.detail["per_column_null_rate"]["temperature_2m"] == 100.0
    assert outcome.detail["per_column_score"]["temperature_2m"] == 0.0
    assert outcome.score < 100.0
    assert "temperature_2m" in outcome.message or outcome.detail["columns_without_nulls"] < len(
        outcome.detail["critical_columns"]
    )


def test_null_rate_fails_when_many_required_columns_empty(registry) -> None:
    """多个必需字段为空应判为不通过（数据契约破损）。"""
    rows = recent_rows(48)
    broken = (
        "temperature_2m",
        "relative_humidity_2m",
        "dew_point_2m",
        "apparent_temperature",
        "cloud_cover",
        "pressure_msl",
    )
    for row in rows:
        for name in broken:
            row[name] = None
    outcome = run_check(
        registry, ASSET_FORECAST_HOURLY, "completeness.null_rate", pd.DataFrame(rows), window_hours=48
    )
    assert outcome.status == "fail"
    assert outcome.detail["columns_without_nulls"] <= len(outcome.detail["critical_columns"]) - len(broken) + 1


def test_null_rate_clean_data_passes(registry) -> None:
    frame = pd.DataFrame(recent_rows(48))
    outcome = run_check(registry, ASSET_FORECAST_HOURLY, "completeness.null_rate", frame, window_hours=48)
    assert outcome.status == "pass"
    assert outcome.records_failed == 0
    assert outcome.detail["columns_without_nulls"] == len(outcome.detail["critical_columns"])


def test_obs_hours_check_uses_daily_asset(registry) -> None:
    """日汇总资产的有效观测小时数检查。"""
    frame = pd.DataFrame({"date": recent_dates(2), "obs_hours": [24, 12]})
    outcome = run_check(
        registry, ASSET_ENVIRONMENT_DAILY, "completeness.obs_hours", frame, window_hours=48
    )
    assert outcome.observed == pytest.approx(18.0)
    assert outcome.status in {"warn", "fail"}


# ==================================================================
# 时效性
# ==================================================================
def test_freshness_within_sla(registry) -> None:
    """延迟小于 SLA 应满分。"""
    frame = pd.DataFrame(recent_rows(24))
    outcome = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "timeliness.freshness",
        frame,
        window_hours=48,
        local_now=NOW + timedelta(minutes=30),
    )
    assert outcome.status == "pass"
    assert outcome.observed == pytest.approx(30.0, abs=1.0)


def test_freshness_beyond_sla_and_zero(registry) -> None:
    """远超 SLA 时应趋近 0 分。"""
    frame = pd.DataFrame(recent_rows(48))
    outcome = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "timeliness.freshness",
        frame,
        window_hours=168,
        local_now=NOW + timedelta(hours=20),
    )
    assert outcome.status == "fail"
    assert outcome.score == 0.0


def test_run_success_rate_from_run_stats(registry) -> None:
    """运行成功率直接来自采集记录统计。"""
    frame = pd.DataFrame(recent_rows(10))
    good = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "timeliness.run_success_rate",
        frame,
        run_stats={"total": 4, "success": 4, "partial": 0, "failed": 0},
    )
    assert good.score == 100.0
    mixed = run_check(
        registry,
        ASSET_FORECAST_HOURLY,
        "timeliness.run_success_rate",
        frame,
        run_stats={"total": 4, "success": 2, "partial": 1, "failed": 1},
    )
    assert mixed.score == pytest.approx(62.5, abs=0.1)
    empty = run_check(
        registry, ASSET_FORECAST_HOURLY, "timeliness.run_success_rate", frame, run_stats={}
    )
    assert empty.status == "skipped"


# ==================================================================
# 有效性
# ==================================================================
def test_validity_range_detects_outliers(registry) -> None:
    """越界值应按比例扣分并给出样本。"""
    rows = recent_rows(48)
    rows[0]["temperature_2m"] = 999.0
    rows[1]["temperature_2m"] = -500.0
    frame = pd.DataFrame(rows)
    outcome = run_check(registry, ASSET_FORECAST_HOURLY, "validity.range", frame, window_hours=48)
    assert outcome.records_failed >= 2
    assert "temperature_2m" in outcome.detail["violations"]


def test_validity_range_clean(registry) -> None:
    frame = pd.DataFrame(recent_rows(48))
    outcome = run_check(registry, ASSET_FORECAST_HOURLY, "validity.range", frame, window_hours=48)
    assert outcome.score == 100.0


def test_validity_enum_flags_unknown_weather_code(registry) -> None:
    """非法 WMO 代码应被识别。"""
    rows = recent_rows(10)
    rows[0]["weather_code"] = 42
    frame = pd.DataFrame(rows)
    outcome = run_check(registry, ASSET_FORECAST_HOURLY, "validity.enum", frame, window_hours=48)
    assert outcome.records_failed == 1
    assert outcome.status == "fail"


def test_type_conformance_detects_drift(registry) -> None:
    """物理类型与字段字典不一致应被判定为有效性问题。"""
    frame = pd.DataFrame(recent_rows(5))
    schema = {name: "DOUBLE" for name in registry.asset(ASSET_FORECAST_HOURLY).column_names}
    outcome = run_check(
        registry, ASSET_FORECAST_HOURLY, "validity.type_conformance", frame, lake_schema=schema
    )
    assert outcome.records_failed > 0
    mismatched = {item["column"] for item in outcome.detail["mismatches"]}
    assert "time" in mismatched
    assert "temperature_2m" not in mismatched


# ==================================================================
# 一致性
# ==================================================================
def test_pm25_le_pm10_violation(registry) -> None:
    """PM2.5 高于 PM10 应触发一致性告警。"""
    rows = recent_air(10)
    rows[0]["pm2_5"] = 500.0
    rows[0]["pm10"] = 50.0
    frame = pd.DataFrame(rows)
    outcome = run_check(registry, ASSET_AIR_QUALITY_HOURLY, "consistency.pm25_le_pm10", frame, window_hours=48)
    assert outcome.records_failed >= 1
    assert outcome.detail["samples"]


def test_aqi_level_consistency(registry) -> None:
    """派生等级与 AQI 数值矛盾时应被检出。"""
    frame = pd.DataFrame(
        {
            "time": recent_stamps(4),
            "european_aqi": [10.0, 30.0, 90.0, 150.0],
            "aqi_level": ["优", "良", "优", "严重污染"],
        }
    )
    outcome = run_check(
        registry, ASSET_ENVIRONMENT_HOURLY, "consistency.aqi_matches_level", frame, window_hours=48
    )
    assert outcome.records_failed == 1
    assert outcome.detail["samples"][0]["aqi"] == 90.0
    assert outcome.detail["samples"][0]["expected"] == "重度污染"


def test_aqi_level_consistency_supports_serving_layer_columns(registry) -> None:
    """日汇总资产用 aqi_max + dominant_aqi_level 命名，同一检查也应生效。"""
    frame = pd.DataFrame(
        {
            "date": recent_dates(3),
            "aqi_max": [30.0, 90.0, 150.0],
            "dominant_aqi_level": ["良", "优", "严重污染"],
        }
    )
    outcome = run_check(
        registry, ASSET_ENVIRONMENT_DAILY, "consistency.aqi_matches_level", frame, window_hours=72
    )
    assert outcome.status != "skipped"
    assert outcome.detail["aqi_column"] == "aqi_max"
    assert outcome.records_failed == 1  # 90 应判为重度污染
    assert outcome.detail["samples"][0]["expected"] == "重度污染"


def test_daily_assets_are_compared_on_day_boundaries(registry) -> None:
    """逐日资产不得被小时精度的窗口/新鲜度误判。

    "今天"这一行的时间戳是当日零点，而当前时刻是当天任意时点；
    两者若直接按小时比较，当天数据会被算作未开始，新鲜度也会虚增近一天。
    """
    frame = pd.DataFrame(
        {"date": recent_dates(3), "obs_hours": [24, 24, 24], "temp_min": [1.0] * 3, "temp_max": [9.0] * 3}
    )
    late_evening = NOW.replace(hour=23, minute=0)

    coverage = run_check(
        registry,
        ASSET_ENVIRONMENT_DAILY,
        "completeness.time_coverage",
        frame,
        window_hours=72,
        local_now=late_evening,
        params=_NO_WINDOW_OVERRIDE,
    )
    assert coverage.score == pytest.approx(100.0, abs=1.0), coverage.message

    fresh = run_check(
        registry,
        ASSET_ENVIRONMENT_DAILY,
        "timeliness.freshness",
        frame,
        window_hours=72,
        local_now=late_evening,
    )
    # 当日零点到今天零点 = 0 天延迟，而不是 23 小时
    assert fresh.observed == pytest.approx(0.0, abs=1.0), fresh.message
    assert fresh.status == "pass"


def test_apparent_temp_deviation(registry) -> None:
    rows = recent_rows(10)
    rows[0]["apparent_temperature"] = 80.0
    frame = pd.DataFrame(rows)
    outcome = run_check(
        registry, ASSET_FORECAST_HOURLY, "consistency.apparent_temp_deviation", frame, window_hours=48
    )
    assert outcome.records_failed == 1


def test_rain_le_precipitation(registry) -> None:
    rows = recent_rows(10)
    rows[0]["rain"] = 5.0
    rows[0]["precipitation"] = 0.0
    frame = pd.DataFrame(rows)
    outcome = run_check(
        registry, ASSET_FORECAST_HOURLY, "consistency.rain_le_precipitation", frame, window_hours=48
    )
    assert outcome.records_failed == 1


def test_daily_extremes(registry) -> None:
    frame = pd.DataFrame(
        {
            "date": recent_dates(2),
            "temperature_2m_min": [10.0, 30.0],
            "temperature_2m_max": [20.0, 25.0],
            "apparent_temperature_min": [9.0, 28.0],
            "apparent_temperature_max": [19.0, 24.0],
        }
    )
    outcome = run_check(
        registry, ASSET_FORECAST_DAILY, "consistency.daily_extremes", frame, window_hours=48
    )
    assert outcome.records_failed == 2


def test_daily_extremes_supports_serving_layer_naming(registry) -> None:
    """服务层用 temp_min/temp_max 命名，同一检查也应生效。"""
    frame = pd.DataFrame(
        {
            "date": recent_dates(2),
            "temp_min": [10.0, 30.0],
            "temp_max": [20.0, 25.0],
        }
    )
    outcome = run_check(
        registry, ASSET_ENVIRONMENT_DAILY, "consistency.daily_extremes", frame, window_hours=48
    )
    assert outcome.status != "skipped"
    assert outcome.records_failed == 1


# ==================================================================
# 唯一性
# ==================================================================
def test_primary_key_uniqueness_detects_duplicates(registry) -> None:
    """主键重复率应被准确统计。"""
    rows = recent_rows(10)
    rows.append(dict(rows[0]))
    rows.append(dict(rows[1]))
    frame = pd.DataFrame(rows)
    outcome = run_check(
        registry, ASSET_FORECAST_HOURLY, "uniqueness.primary_key", frame, window_hours=48
    )
    assert outcome.records_failed == 2
    assert outcome.detail["duplicates_by_city"]["beijing"] == 2


def test_primary_key_uniqueness_clean(registry) -> None:
    frame = pd.DataFrame(recent_rows(10))
    outcome = run_check(
        registry, ASSET_FORECAST_HOURLY, "uniqueness.primary_key", frame, window_hours=48
    )
    assert outcome.score == 100.0


# ==================================================================
# 引擎端到端
# ==================================================================
def test_engine_scores_and_persists(workspace, registry) -> None:
    """完整走一遍：落湖 → 派生 → 评测 → 持久化。"""
    from atmos.collect.jobs import JobContext, build_city_environment_daily, build_city_environment_hourly
    from atmos.collect.runner import Collector
    from atmos.db import session_scope
    from atmos.models import AssetQualityScore, DataAsset, QualityResult
    from atmos.quality.engine import QualityEngine
    from atmos.settings import get_settings
    from sqlalchemy import func, select

    rows = recent_rows(50, hours_back=49)
    air_dataset = air_rows(50, start=(NOW - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M"))

    writer = LakeWriter()
    writer.write(registry.asset(ASSET_FORECAST_HOURLY), "beijing", rows, run_id="run-1")
    writer.write(registry.asset(ASSET_AIR_QUALITY_HOURLY), "beijing", air_dataset, run_id="run-1")

    cities = Collector(registry).resolve_cities(["beijing"])
    assert [city.slug for city in cities] == ["beijing"]

    context = JobContext(
        registry=registry, settings=get_settings(), run_id="run-1", cities=cities
    )
    hourly = build_city_environment_hourly(context)
    assert hourly.status == "success"
    assert hourly.rows_written == 50

    daily = build_city_environment_daily(context)
    assert daily.status == "success"
    assert daily.rows_written >= 2

    engine = QualityEngine(registry)
    with session_scope() as session:
        report = engine.evaluate_asset(session, ASSET_ENVIRONMENT_HOURLY, window_hours=48, run_id="run-1")

    assert report.checks_total > 0
    assert 0 <= report.overall <= 100
    assert report.grade in {"A", "B", "C", "D", "E"}
    for key in ("completeness", "timeliness", "validity", "consistency", "uniqueness"):
        dimension = report.dimensions[key]
        assert dimension.score is not None, key
        assert 0 <= dimension.score <= 100
    assert report.rows_evaluated == 50

    with session_scope() as session:
        results = session.scalar(select(func.count(QualityResult.id)))
        scores = session.scalar(select(func.count(AssetQualityScore.id)))
        asset = session.scalar(
            select(DataAsset).where(DataAsset.asset_key == ASSET_ENVIRONMENT_HOURLY)
        )
    assert results == report.checks_total
    assert scores == 1
    assert asset is not None
    assert asset.latest_grade == report.grade
    assert asset.latest_score == pytest.approx(round(report.overall, 2))


def test_engine_handles_asset_without_data(workspace, registry) -> None:
    """没有任何数据时评测不应崩溃，且各维度为 0 分。"""
    from atmos.db import session_scope
    from atmos.quality.engine import QualityEngine

    with session_scope() as session:
        report = QualityEngine(registry).evaluate_asset(session, ASSET_ENVIRONMENT_DAILY)
    assert report.rows_evaluated == 0
    assert report.overall == 0.0
    assert report.grade == "E"


def test_grade_mapping_is_monotonic(registry) -> None:
    """分数越高等级不应更差。"""
    model = registry.quality
    grades = [model.grade_for(score)["grade"] for score in range(0, 101, 5)]
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
    ranks = [order[grade] for grade in grades]
    assert ranks == sorted(ranks, reverse=True)
