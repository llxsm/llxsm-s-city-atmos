"""分析引擎测试：数值算法 + 六大分析能力。

分两层：
* **数值层**（``TestNumeric``）—— 纯函数，用已知答案的构造数据校验；
* **分析层** —— 先用 ``seeded`` 夹具把可复现的数据写进数据湖，
  再校验各分析器的结论是否符合预期（例如刻意造一次超标过程，
  验证污染过程识别的起止时间与峰值）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from atmos.insights import numeric
from atmos.insights.loader import FrameLoader

NOW = datetime.now().replace(minute=0, second=0, microsecond=0)


# ==================================================================
# 数值层
# ==================================================================
class TestNumeric:
    def test_describe_matches_numpy(self) -> None:
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 100.0])
        stats = numeric.describe(values)
        assert stats["count"] == 10
        assert stats["mean"] == round(float(np.mean(values)), 4)
        assert stats["min"] == 1.0 and stats["max"] == 100.0
        assert stats["p50"] == 5.5
        assert stats["iqr"] == round(7.75 - 3.25, 4)
        # 长尾数据上均值应显著高于中位数
        assert stats["mean"] > stats["p50"]

    def test_describe_handles_empty(self) -> None:
        assert numeric.describe(np.array([])) == {"count": 0}

    def test_boxplot_flags_outliers(self) -> None:
        values = np.concatenate([np.linspace(10, 20, 50), np.array([100.0, -50.0])])
        box = numeric.boxplot(values)
        assert box is not None
        assert box.outlier_count == 2
        assert 100.0 in box.outliers and -50.0 in box.outliers
        assert box.whisker_high < 100.0

    def test_histogram_bins_sum_to_sample(self) -> None:
        values = np.random.default_rng(7).normal(20, 5, 500)
        result = numeric.histogram(values, bins=10)
        assert len(result["bins"]) == 10
        assert sum(item["count"] for item in result["bins"]) == 500
        assert abs(sum(item["ratio"] for item in result["bins"]) - 100.0) < 0.01

    def test_pearson_detects_linear_relation(self) -> None:
        x = np.linspace(0, 10, 50)
        assert numeric.pearson(x, 2 * x + 3) == pytest.approx(1.0, abs=1e-9)
        assert numeric.pearson(x, -2 * x + 3) == pytest.approx(-1.0, abs=1e-9)

    def test_pearson_returns_none_for_constant_series(self) -> None:
        assert numeric.pearson(np.ones(10), np.arange(10)) is None
        assert numeric.pearson(np.array([1.0, 2.0]), np.array([1.0, 2.0])) is None

    def test_spearman_handles_monotonic_nonlinear(self) -> None:
        """单调但非线性的关系：Spearman 应接近 1，Pearson 明显偏低。"""
        x = np.linspace(0.1, 5, 60)
        y = np.exp(x)
        rho = numeric.spearman(x, y)
        r = numeric.pearson(x, y)
        assert rho == pytest.approx(1.0, abs=1e-9)
        assert r is not None and r < 0.95

    def test_spearman_handles_ties(self) -> None:
        x = np.array([1, 1, 2, 2, 3, 3], dtype=float)
        y = np.array([1, 2, 3, 4, 5, 6], dtype=float)
        rho = numeric.spearman(x, y)
        assert rho is not None and 0.8 < rho <= 1.0

    def test_linear_regression_recovers_parameters(self) -> None:
        rng = np.random.default_rng(11)
        x = np.linspace(0, 10, 200)
        y = 3.0 * x + 7.0 + rng.normal(0, 0.01, 200)
        result = numeric.linear_regression(x, y)
        assert result is not None
        assert result["slope"] == pytest.approx(3.0, abs=0.01)
        assert result["intercept"] == pytest.approx(7.0, abs=0.05)
        assert result["r2"] > 0.999

    def test_correlation_label_reads_naturally(self) -> None:
        assert numeric.correlation_label(-0.9) == "极强负相关"
        assert numeric.correlation_label(0.65) == "强正相关"
        assert numeric.correlation_label(0.1) == "几乎无"
        assert numeric.correlation_label(None) == "样本不足"

    def test_correlation_matrix_is_symmetric(self) -> None:
        rng = np.random.default_rng(3)
        base = rng.normal(0, 1, 100)
        columns = {
            "a": base,
            "b": base * 2 + rng.normal(0, 0.1, 100),
            "c": rng.normal(0, 1, 100),
        }
        result = numeric.correlation_matrix(columns)
        values = result["values"]
        assert values[0][0] == 1.0
        assert values[0][1] == values[1][0]
        assert values[0][1] > 0.9

    def test_outlier_masks(self) -> None:
        values = np.concatenate([np.random.default_rng(5).normal(0, 1, 200), np.array([50.0])])
        assert numeric.outliers_by_zscore(values, threshold=3.0).sum() == 1
        assert numeric.outliers_by_iqr(values).sum() == 1

    def test_robust_zscore_ignores_extreme(self) -> None:
        """稳健 Z 分数不应被单个极端值带偏（这是选它而非标准差的理由）。"""
        values = np.concatenate([np.random.default_rng(9).normal(0, 1, 200), np.array([1e6])])
        robust = numeric.robust_zscore(values)
        assert np.abs(robust[:-1]).max() < 6

    def test_standardize_gives_unit_variance(self) -> None:
        matrix = np.array([[1.0, 1000.0], [2.0, 2000.0], [3.0, 3000.0], [4.0, 4000.0]])
        scaled = numeric.standardize(matrix)
        assert np.allclose(scaled.mean(axis=0), 0, atol=1e-9)
        assert np.allclose(scaled.std(axis=0), 1, atol=1e-9)

    def test_kmeans_separates_obvious_clusters(self) -> None:
        rng = np.random.default_rng(17)
        left = rng.normal(-10, 0.4, (40, 2))
        right = rng.normal(10, 0.4, (40, 2))
        matrix = np.vstack([left, right])
        result = numeric.kmeans(matrix, 2)
        assert result is not None
        assert len(set(result.labels)) == 2
        # 前 40 个应同属一簇，后 40 个同属另一簇
        assert len(set(result.labels[:40])) == 1
        assert len(set(result.labels[40:])) == 1
        assert result.labels[0] != result.labels[-1]
        assert result.silhouette is not None and result.silhouette > 0.9

    def test_kmeans_is_reproducible(self) -> None:
        """固定随机种子：同一批数据两次结果必须一致。"""
        matrix = np.random.default_rng(23).normal(0, 1, (30, 3))
        first = numeric.kmeans(matrix, 3)
        second = numeric.kmeans(matrix, 3)
        assert first is not None and second is not None
        assert first.labels == second.labels
        assert first.inertia == second.inertia

    def test_kmeans_rejects_degenerate_input(self) -> None:
        assert numeric.kmeans(np.array([[1.0, 2.0]]), 3) is None
        assert numeric.kmeans(np.random.default_rng(1).normal(0, 1, (10, 2)), 1) is None

    def test_similarity_matrix_range(self) -> None:
        matrix = np.array([[0.0, 0.0], [1.0, 0.0], [0.0, 3.0]])
        similarity = numeric.similarity_matrix(matrix)
        assert np.allclose(np.diag(similarity), 1.0)
        assert similarity.min() >= 0.0 and similarity.max() <= 1.0

    def test_choose_k_returns_candidates(self) -> None:
        rng = np.random.default_rng(31)
        matrix = np.vstack([rng.normal(offset, 0.3, (25, 2)) for offset in (-8, 0, 8)])
        candidates = numeric.choose_k(matrix, k_min=2, k_max=5)
        assert [item["k"] for item in candidates] == [2, 3, 4, 5]
        assert any(item["silhouette"] is not None for item in candidates)


# ==================================================================
# 分析层：先播种可复现数据
# ==================================================================
def _seed_rows(count: int, start: datetime, *, spike_hours: range | None = None) -> list[dict]:
    """构造逐小时天气记录；``spike_hours`` 指定造高污染的小时下标区间。"""
    from tests.conftest import forecast_rows

    rows = forecast_rows(count, start=start.strftime("%Y-%m-%dT%H:%M"))
    for index, row in enumerate(rows):
        row["temperature_2m"] = round(18.0 + 6.0 * np.sin(index / 24 * 2 * np.pi), 2)
        row["relative_humidity_2m"] = round(60.0 + 15.0 * np.cos(index / 24 * 2 * np.pi), 2)
        # 静稳（低风速）与高污染同时出现，供"成因分析"验证负相关
        row["wind_speed_10m"] = 2.0 if spike_hours and index in spike_hours else round(
            6.0 + 4.0 * (index % 12) / 11, 2
        )
    return rows


def _seed_air(count: int, start: datetime, *, spike_hours: range | None = None) -> list[dict]:
    from tests.conftest import air_rows

    rows = air_rows(count, start=start.strftime("%Y-%m-%dT%H:%M"))
    for index, row in enumerate(rows):
        if spike_hours and index in spike_hours:
            aqi = 130.0
            pm25 = 180.0
            pm10 = 220.0
        else:
            aqi = round(25.0 + 8.0 * np.sin(index / 12 * np.pi), 2)
            pm25 = round(18.0 + 6.0 * np.sin(index / 12 * np.pi), 2)
            pm10 = round(pm25 * 1.6, 2)
        row["european_aqi"] = aqi
        row["pm2_5"] = pm25
        row["pm10"] = pm10
        row["us_aqi"] = aqi * 2
    return rows


@pytest.fixture()
def seeded(workspace, registry):
    """播种 10 天 × 2 城市的数据，并在其中造一次 6 小时的污染过程。"""
    import asyncio

    from atmos.collect.jobs import (
        JobContext,
        build_city_environment_daily,
        build_city_environment_hourly,
    )
    from atmos.collect.runner import Collector
    from atmos.lake.writer import LakeWriter
    from atmos.settings import get_settings
    from atmos.sources.specs import (
        ASSET_AIR_QUALITY_HOURLY,
        ASSET_FORECAST_HOURLY,
    )

    hours = 240
    start = NOW - timedelta(hours=hours - 1)
    # 污染过程必须持续足够长：国标 AQI 对 PM 用 24 小时滑动平均，
    # 只有几小时的尖峰会被平滑掉，不会构成一次"超标过程"。
    spike = range(100, 130)  # beijing 的第 100–129 小时超标（30 小时）
    writer = LakeWriter()

    writer.write(
        registry.asset(ASSET_FORECAST_HOURLY),
        "beijing",
        _seed_rows(hours, start, spike_hours=spike),
        run_id="seed",
    )
    writer.write(
        registry.asset(ASSET_AIR_QUALITY_HOURLY),
        "beijing",
        _seed_air(hours, start, spike_hours=spike),
        run_id="seed",
    )
    for slug in ("shanghai", "wuhan"):
        writer.write(
            registry.asset(ASSET_FORECAST_HOURLY),
            slug,
            _seed_rows(hours, start, spike_hours=None),
            run_id="seed",
        )
        writer.write(
            registry.asset(ASSET_AIR_QUALITY_HOURLY),
            slug,
            _seed_air(hours, start, spike_hours=None),
            run_id="seed",
        )

    cities = Collector(registry).resolve_cities(["beijing", "shanghai", "wuhan"])
    context = JobContext(registry=registry, settings=get_settings(), run_id="seed", cities=cities)
    build_city_environment_hourly(context)
    build_city_environment_daily(context)

    from atmos.insights.service import InsightService

    return InsightService(registry)


# ==================================================================
# 分析层
# ==================================================================
def test_loader_reports_availability(seeded) -> None:
    availability = seeded.availability()
    assert availability["has_environment"] is True
    assert availability["has_daily"] is True
    # 可用城市是全部启用城市，播种的 3 个应是其子集
    assert {"beijing", "shanghai", "wuhan"} <= set(availability["cities"])
    assert len(availability["cities"]) == 15


def test_distribution_reports_quantiles_and_outliers(seeded) -> None:
    payload = seeded.distribution(cities=["beijing"], metrics=["pm2_5"], days=10)
    assert payload["metrics"], payload
    metric = payload["metrics"][0]
    assert metric["overall"]["count"] > 100
    assert metric["overall"]["p50"] < metric["overall"]["max"]
    assert metric["histogram"]["bins"]
    assert metric["by_city"][0]["city_name"] == "北京"
    # 造了 6 小时高值，箱线图应把它们标成离群点
    assert metric["boxplot"]["outlier_count"] >= 1


def test_distribution_empty_state(seeded) -> None:
    payload = seeded.distribution(cities=["beijing"], metrics=["pm2_5"], days=1)
    assert "metrics" in payload
    assert payload.get("available") is not False or payload.get("message")


def test_correlation_finds_wind_pm25_negative(seeded) -> None:
    """构造数据里风速与 PM2.5 负相关，相关矩阵应能识别。"""
    payload = seeded.correlation(cities=["beijing"], metrics=["wind_speed_10m", "pm2_5"], days=10)
    assert payload["labels"] == ["wind_speed_10m", "pm2_5"]
    coefficient = payload["pearson"][0][1]
    assert coefficient is not None and coefficient < -0.2
    assert payload["highlights"][0]["left"] in {"wind_speed_10m", "pm2_5"}
    assert all(item["strength"] for item in payload["highlights"])


def test_hourly_profile_returns_24_hours(seeded) -> None:
    payload = seeded.hourly_profile(cities=["beijing", "shanghai"], metric="temperature_2m", days=10)
    assert payload["hours"] == list(range(24))
    assert len(payload["rows"]) == 2
    for row in payload["rows"]:
        assert len(row["values"]) == 24
        assert 0 <= row["peak_hour"] <= 23
        assert row["peak_value"] >= row["trough_value"]
        assert row["amplitude"] >= 0


def test_weekly_profile_splits_weekday_and_weekend(seeded) -> None:
    payload = seeded.weekly_profile(cities=["beijing"], metrics=["european_aqi"], days=10)
    assert payload["weekday_labels"][0] == "周一"
    assert payload["metrics"], payload
    metric = payload["metrics"][0]
    assert len(metric["by_weekday"]) == 7
    assert metric["higher_on"] in {"工作日", "周末"}
    assert metric["delta"] == pytest.approx(metric["weekend_mean"] - metric["weekday_mean"], abs=1e-6)


def test_monthly_trend(seeded) -> None:
    payload = seeded.monthly_trend(cities=["beijing"], metric="european_aqi", days=10)
    assert payload["months"], payload
    assert all(item["count"] > 0 for item in payload["months"])


def test_outliers_detects_spike(seeded) -> None:
    """IQR 方法应能识别出播种的高值时段。

    这里刻意用 IQR 而不是 Z 分数：30 小时的尖峰本身会显著抬高整体标准差，
    使 Z 分数降到 3 以下而检不出 —— 长尾污染数据用 Tukey 围栏更合适。
    """
    payload = seeded.outliers(cities=["beijing"], metric="european_aqi", days=10, method="iqr")
    assert payload["available"] is True
    assert payload["flagged"] >= 1
    assert payload["points"]
    top = payload["points"][0]
    assert top["value"] >= 100
    assert payload["by_city"][0]["city_slug"] == "beijing"
    assert payload["by_city"][0]["flagged_ratio"] > 0


def test_outliers_zscore_threshold_is_respected(seeded) -> None:
    """Z 分数阈值放宽后应能检出同一批尖峰，且标注的 |Z| 都不小于阈值。"""
    strict = seeded.outliers(cities=["beijing"], metric="european_aqi", days=10, method="zscore", threshold=3.0)
    loose = seeded.outliers(cities=["beijing"], metric="european_aqi", days=10, method="zscore", threshold=2.0)
    assert loose["flagged"] > strict["flagged"]
    assert loose["flagged"] >= 1
    for point in loose["points"]:
        assert abs(point["z"]) >= 2.0
    assert loose["method"] == "zscore"
    assert loose["threshold"] == 2.0


def test_episodes_identifies_the_seeded_pollution_event(seeded) -> None:
    """核心断言：识别出刻意造的污染过程，且峰值与国标 IAQI 一致。

    国标 AQI 对 PM2.5 使用 24 小时滑动平均，因此播种的 30 小时高浓度
    会形成一个持续约一天多的过程，峰值等于 PM2.5=180 μg/m³ 对应的 IAQI。
    """
    from atmos.standards import iaqi

    payload = seeded.episodes(cities=["beijing"], days=10, threshold=100.0, min_gap_hours=3, min_duration_hours=2)
    assert payload["available"] is True
    assert payload["aqi_standard"].startswith("国标")
    assert payload["episode_count"] >= 1

    episode = payload["episodes"][0]
    assert episode["city_slug"] == "beijing"
    assert episode["duration_hours"] >= 18
    assert episode["peak_value"] == pytest.approx(iaqi("pm2_5", 180.0), abs=1.0)
    assert episode["peak_level"] in {"中度污染", "重度污染", "严重污染"}
    assert episode["primary_pollutant"] == "PM2.5"
    assert episode["severity"] in {"轻微", "中等", "较重", "严重"}
    assert episode["chronic"] is False  # 30 小时远小于 10 天窗口的四分之一

    start = datetime.fromisoformat(episode["start"])
    expected_start = NOW - timedelta(hours=240 - 1 - 100)
    # 滑动平均使过程起点比原始尖峰晚若干小时，留出缓冲
    assert expected_start <= start <= expected_start + timedelta(hours=24)

    weather = episode["weather"]
    assert weather["wind_speed_avg"] is not None
    assert weather["stagnant_hours"] is not None


def test_episodes_respects_threshold(seeded) -> None:
    """把阈值抬到峰值之上时应识别不出任何过程。"""
    payload = seeded.episodes(cities=["beijing"], days=10, threshold=500.0)
    assert payload["available"] is True
    assert payload["episode_count"] == 0


def test_chronic_episode_is_flagged(seeded) -> None:
    """阈值低于常态水平时，长时段覆盖会被标记为持续性污染而不是单次事件。"""
    payload = seeded.episodes(cities=["beijing", "shanghai"], days=10, threshold=10.0)
    assert payload["available"] is True
    assert payload["chronic_episode_count"] >= 1
    assert payload["chronic_cities"]


def test_episodes_gap_merging(seeded) -> None:
    """允许的间隙越大，越倾向于把过程合并成更少的段。"""
    from atmos.insights.loader import FrameLoader

    loader = FrameLoader()
    tight = FrameLoader()
    assert loader is not None and tight is not None
    strict = seeded.episodes(cities=["beijing"], days=10, min_gap_hours=0)
    loose = seeded.episodes(cities=["beijing"], days=10, min_gap_hours=12)
    assert loose["episode_count"] <= strict["episode_count"]


def test_relationship_reports_correlation_and_regression(seeded) -> None:
    payload = seeded.relationship(x="wind_speed_10m", y="pm2_5", cities=["beijing"], days=10)
    assert payload["available"] is True
    assert payload["samples"] > 100
    assert payload["pearson"] is not None
    assert payload["regression"]["n"] > 100
    assert payload["bins"]
    assert payload["interpretation"]
    assert payload["caveat"]
    assert payload["scatter"]
    assert len(payload["scatter"]) <= 1500


def test_relationship_by_city(seeded) -> None:
    payload = seeded.relationship(
        x="wind_speed_10m", y="pm2_5", cities=["beijing", "shanghai"], days=10, by_city=True
    )
    assert "by_city" in payload
    assert {item["city_slug"] for item in payload["by_city"]} == {"beijing", "shanghai"}


def test_compliance_report(seeded) -> None:
    payload = seeded.compliance(cities=["beijing", "shanghai", "wuhan"], days=10, target_ratio=80.0)
    assert payload["available"] is True
    assert payload["aqi_standard"].startswith("国标")
    assert len(payload["cities"]) == 3
    for row in payload["cities"]:
        assert row["days"] > 0
        assert row["good_days"] + row["exceed_days"] == row["days"]
        assert 0 <= row["good_day_ratio"] <= 100
        assert sum(row["level_distribution"].values()) == row["days"]
        assert row["target_met"] is not None
    totals = payload["totals"]
    assert totals["city_count"] == 3
    assert totals["best_city"] and totals["worst_city"]
    assert totals["cities_meeting_target"] <= 3
    # 造了污染过程的北京，优良率必须低于未超标城市
    beijing = next(row for row in payload["cities"] if row["city_slug"] == "beijing")
    shanghai = next(row for row in payload["cities"] if row["city_slug"] == "shanghai")
    assert beijing["good_day_ratio"] < shanghai["good_day_ratio"]
    assert shanghai["good_day_ratio"] == 100.0


def test_compliance_export_rows(seeded) -> None:
    header, rows, title = seeded.export_rows("compliance", cities=["beijing"], days=10)
    assert "优良天比例(%)" in header
    assert len(rows) == 1
    assert len(rows[0]) == len(header)
    assert title == "达标考核报表"


def test_episode_export_rows(seeded) -> None:
    header, rows, title = seeded.export_rows("episodes", cities=["beijing"], days=10)
    assert "首要污染物" in header
    assert rows and all(len(row) == len(header) for row in rows)
    assert title == "污染过程清单"
    assert "持续性污染" in header


@pytest.mark.parametrize("analysis", ["compliance", "episodes", "distribution"])
def test_export_rows_are_rectangular(seeded, analysis: str) -> None:
    """导出表头与数据行列数必须一致，否则 CSV 会错位。"""
    header, rows, title = seeded.export_rows(analysis, cities=["beijing", "shanghai"], days=10)
    assert header and title
    assert rows
    for row in rows:
        assert len(row) == len(header), f"{analysis} 行列数不一致"


def test_export_rejects_unknown_analysis(seeded) -> None:
    with pytest.raises(ValueError, match="不支持导出"):
        seeded.export_rows("nonsense")


def test_period_comparison(seeded) -> None:
    payload = seeded.comparison(cities=["beijing", "shanghai"], days=2)
    assert payload["available"] is True
    assert len(payload["cities"]) == 2
    for row in payload["cities"]:
        assert row["current"] is not None
        assert row["trend"] in {"改善", "转差", "持平", "数据不足"}
    assert "year_over_year" in payload


def test_clustering_groups_cities(seeded) -> None:
    payload = seeded.clustering(cities=["beijing", "shanghai", "wuhan"], days=10)
    assert payload["available"] is True
    assert payload["city_count"] == 3
    assert payload["k"] >= 2
    assert payload["clusters"]
    members = [item["city_slug"] for cluster in payload["clusters"] for item in cluster["members"]]
    assert sorted(members) == ["beijing", "shanghai", "wuhan"]
    for cluster in payload["clusters"]:
        assert cluster["label"]
        assert cluster["size"] == len(cluster["members"])
        assert cluster["centroid"]
    assert payload["candidate_k"]


def test_clustering_requires_enough_cities(seeded) -> None:
    payload = seeded.clustering(cities=["beijing"], days=10)
    assert payload["available"] is False
    assert "不足" in (payload["message"] or "")


def test_digest_summarises(seeded) -> None:
    payload = seeded.digest(cities=["beijing", "shanghai", "wuhan"], days=10)
    assert payload["available"] is True
    assert payload["compliance"]["city_count"] == 3
    assert payload["episodes"]["count"] >= 1
    assert payload["profile"]["rows"]


def test_insight_catalog_lists_all_capabilities(seeded) -> None:
    payload = seeded.catalog()
    assert payload["total"] == 11
    keys = {item["key"] for group in payload["categories"] for item in group["items"]}
    assert {
        "distribution",
        "correlation",
        "hourly-profile",
        "weekly-profile",
        "monthly-trend",
        "outliers",
        "episodes",
        "relationship",
        "compliance",
        "comparison",
        "clustering",
    } == keys
    assert payload["metrics"]


def test_analysers_return_empty_state_without_data(workspace, registry) -> None:
    """无数据时每个分析器都应返回 available=False 与中文说明，而不是抛错。"""
    from atmos.insights.service import InsightService

    service = InsightService(registry)
    checks = [
        service.distribution(days=7),
        service.correlation(days=7),
        service.hourly_profile(days=7),
        service.weekly_profile(days=7),
        service.monthly_trend(days=7),
        service.outliers(days=7),
        service.episodes(days=7),
        service.relationship(days=7),
        service.compliance(days=7),
        service.comparison(days=7),
        service.clustering(days=7),
    ]
    for payload in checks:
        assert payload.get("available") is False, payload
        assert payload.get("message"), payload
