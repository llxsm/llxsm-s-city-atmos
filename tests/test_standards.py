"""业务标准与指标体系测试。"""

from __future__ import annotations

import pytest

from atmos.standards import (
    GOOD_AQI_CEILING,
    aqi_level,
    comfort_index,
    evaluate_alerts,
    is_stagnant,
    pollutant_load,
    primary_pollutant,
    ventilation_index,
    weather_label,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.0, "优"),
        (20.0, "优"),
        (20.1, "良"),
        (40.0, "良"),
        (40.1, "轻度污染"),
        (60.0, "轻度污染"),
        (60.1, "中度污染"),
        (80.1, "重度污染"),
        (100.1, "严重污染"),
        (None, None),
    ],
)
def test_aqi_level_bands(value: float | None, expected: str | None) -> None:
    """AQI 分级边界必须闭合且无重叠。"""
    assert aqi_level(value) == expected


def test_good_air_ceiling_matches_level() -> None:
    """优良阈值应当正好等于"良"的上界，避免两处口径打架。"""
    assert aqi_level(GOOD_AQI_CEILING) == "良"
    assert aqi_level(GOOD_AQI_CEILING + 0.1) == "轻度污染"


def test_primary_pollutant_picks_dominant_species() -> None:
    """首要污染物应取相对限值负荷最大者。"""
    assert primary_pollutant({"pm2_5": 120.0, "pm10": 60.0, "ozone": 40.0}) == "PM2.5"
    assert primary_pollutant({"pm2_5": 10.0, "pm10": 300.0, "ozone": 40.0}) == "PM10"
    assert primary_pollutant({"pm2_5": 5.0, "pm10": 10.0, "ozone": 20.0}) == "无"
    assert primary_pollutant({}) == "无"


def test_pollutant_load_is_monotonic() -> None:
    """污染物负荷随浓度单调不减。"""
    from atmos.standards import POLLUTANT_STANDARDS

    standard = POLLUTANT_STANDARDS[0]
    loads = [pollutant_load(value, standard) for value in (0, 20, 35, 50, 100, 300)]
    assert all(item is not None for item in loads)
    assert loads == sorted(loads)  # type: ignore[type-var]
    assert pollutant_load(-1, standard) is None


def test_comfort_index_peaks_in_comfortable_range() -> None:
    """舒适度应在温和干爽条件下最高，在极端条件下显著下降。"""
    pleasant = comfort_index(24.0, 50.0, 8.0)
    hot = comfort_index(40.0, 20.0, 5.0)
    cold = comfort_index(-15.0, 60.0, 30.0)
    assert pleasant is not None and hot is not None and cold is not None
    assert pleasant > hot
    assert pleasant > cold
    assert 0 <= pleasant <= 100
    assert comfort_index(None, 50.0, 5.0) is None


def test_stagnant_requires_low_wind_and_no_rain() -> None:
    """静稳判据：低风速且无有效降水。"""
    assert is_stagnant(3.0, 0.0) == 1
    assert is_stagnant(3.0, 2.0) == 0
    assert is_stagnant(30.0, 0.0) == 0
    assert is_stagnant(None, 0.0) is None


def test_ventilation_index_scales_with_wind() -> None:
    """通风指数随风速单调递增，无风时为 0。"""
    assert ventilation_index(0.0) == 0.0
    assert ventilation_index(36.0) > ventilation_index(3.6)  # type: ignore[operator]
    assert ventilation_index(None) is None


@pytest.mark.parametrize(
    ("code", "expected"),
    [(0, "晴"), (3, "阴"), (61, "小雨"), (95, "雷阵雨"), (99, "雷阵雨伴强冰雹"), (999, "未知"), (None, "未知")],
)
def test_weather_label(code: int | None, expected: str) -> None:
    """WMO 天气代码字典覆盖与兜底。"""
    assert weather_label(code) == expected


def test_alerts_sorted_by_severity() -> None:
    """预警应按严重程度排序，最严重的排最前。"""
    row = {
        "european_aqi": 120.0,
        "temperature_2m": 36.0,
        "wind_speed_10m": 60.0,
        "precipitation": 10.0,
        "uv_index": 9.0,
    }
    alerts = evaluate_alerts(row)
    severities = [item["severity"] for item in alerts]
    assert severities == sorted(severities, key=lambda item: {"severe": 0, "warning": 1, "info": 2}[item])
    keys = {item["key"] for item in alerts}
    assert {"air.severe_pollution", "weather.heat_wave", "weather.gale", "weather.heavy_rain"} <= keys


def test_alerts_are_deduplicated_per_metric() -> None:
    """同一指标的分级规则只报最严重的一条，避免淹没关键信息。"""
    alerts = evaluate_alerts({"european_aqi": 124.0, "temperature_2m": 36.0})
    metrics = [item["metric"] for item in alerts]
    assert len(metrics) == len(set(metrics)), alerts
    assert {item["key"] for item in alerts} == {"air.severe_pollution", "weather.heat_wave"}

    # 恰好落在中间档时应报出该档而非更低档
    moderate = evaluate_alerts({"european_aqi": 65.0})
    assert [item["key"] for item in moderate] == ["air.moderate_pollution"]

    # 未达最低档则不报
    assert evaluate_alerts({"european_aqi": 30.0}) == []


def test_alerts_ignore_missing_metrics() -> None:
    """缺测指标不应产生误报。"""
    assert evaluate_alerts({}) == []
    assert evaluate_alerts({"temperature_2m": None}) == []
    assert [item["key"] for item in evaluate_alerts({"temperature_2m": 20.0})] == []
