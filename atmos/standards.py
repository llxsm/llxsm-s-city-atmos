"""平台业务标准与指标体系（口径单一来源）。

采集派生、质量一致性校验、看板展示三处都必须使用同一套口径，
因此 AQI 分级、首要污染物判定、舒适度、静稳判据、天气现象字典
统一收敛到本模块。任何口径调整只改这里。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from atmos.models.enums import AQILevel

# ==================================================================
# 空气质量分级（欧洲 AQI / EAQI 区间 → 中国 GB 3095 表述）
# ==================================================================
EAQI_BANDS: tuple[tuple[float, AQILevel], ...] = (
    (20.0, AQILevel.EXCELLENT),
    (40.0, AQILevel.GOOD),
    (60.0, AQILevel.LIGHT),
    (80.0, AQILevel.MODERATE),
    (100.0, AQILevel.HEAVY),
    (math.inf, AQILevel.SEVERE),
)

# 空气质量"优良"上界（优 + 良）
GOOD_AQI_CEILING = 40.0

AQI_HEALTH_ADVICE: dict[str, str] = {
    AQILevel.EXCELLENT.value: "空气质量令人满意，可正常户外活动。",
    AQILevel.GOOD.value: "空气质量可接受，极少数敏感人群应减少户外剧烈运动。",
    AQILevel.LIGHT.value: "易感人群症状轻度加剧，儿童、老年人及心肺疾病患者应减少长时间户外活动。",
    AQILevel.MODERATE.value: "进一步加剧易感人群症状，建议疾病患者避免长时间户外活动，一般人群适量减少户外运动。",
    AQILevel.HEAVY.value: "心脏病和肺病患者症状显著加剧，建议儿童、老年人和心脏病、肺病患者停留在室内，停止户外运动。",
    AQILevel.SEVERE.value: "健康人群运动耐受力降低，建议儿童、老年人和病人留在室内，避免体力消耗，一般人群应避免户外活动。",
}


def aqi_level(value: float | None) -> str | None:
    """把欧洲 AQI 数值映射为空气质量等级中文表述。"""
    if value is None or value != value:
        return None
    for upper, level in EAQI_BANDS:
        if value <= upper:
            return level.value
    return AQILevel.SEVERE.value


def aqi_level_index(value: float | None) -> int | None:
    """等级序号（1=优 … 6=严重污染），便于数值化比较。"""
    level = aqi_level(value)
    if level is None:
        return None
    order = [item.value for item in AQILevel]
    return order.index(level) + 1


def exceedance_hours_threshold() -> float:
    """超标判定阈值（高于"良"即为超标）。"""
    return GOOD_AQI_CEILING


# ==================================================================
# 首要污染物判定
# ==================================================================
@dataclass(frozen=True)
class PollutantStandard:
    """单一污染物的分级限值（参考 GB 3095-2012 日均限值，单位 μg/m³）。"""

    key: str
    label: str
    breakpoints: tuple[float, ...]


POLLUTANT_STANDARDS: tuple[PollutantStandard, ...] = (
    PollutantStandard("pm2_5", "PM2.5", (35.0, 75.0, 115.0, 150.0, 250.0)),
    PollutantStandard("pm10", "PM10", (50.0, 150.0, 250.0, 350.0, 420.0)),
    PollutantStandard("ozone", "O₃", (100.0, 160.0, 215.0, 265.0, 800.0)),
    PollutantStandard("nitrogen_dioxide", "NO₂", (40.0, 80.0, 180.0, 280.0, 565.0)),
    PollutantStandard("sulphur_dioxide", "SO₂", (50.0, 150.0, 475.0, 800.0, 1600.0)),
    PollutantStandard("carbon_monoxide", "CO", (2000.0, 4000.0, 14000.0, 24000.0, 36000.0)),
)


def pollutant_load(value: float | None, standard: PollutantStandard) -> float | None:
    """污染物负荷：浓度相对各级限值的归一化程度（>1 表示超标）。"""
    if value is None or value != value or value < 0:
        return None
    for index, breakpoint in enumerate(standard.breakpoints):
        if value <= breakpoint:
            lower = standard.breakpoints[index - 1] if index > 0 else 0.0
            span = breakpoint - lower
            if span <= 0:
                return float(index + 1)
            return index + (value - lower) / span
    return float(len(standard.breakpoints) + 1)


def primary_pollutant(row: dict[str, float | None]) -> str:
    """按污染物负荷最大值判定首要污染物；全部负荷很低时返回"无"。"""
    best_key: str | None = None
    best_load = 0.0
    for standard in POLLUTANT_STANDARDS:
        load = pollutant_load(row.get(standard.key), standard)
        if load is None:
            continue
        if load > best_load:
            best_load = load
            best_key = standard.label
    if best_key is None:
        return "无"
    # 负荷不超过"优"级限值的一半，视为无首要污染物
    return best_key if best_load > 0.5 else "无"


# ==================================================================
# 人体舒适度与扩散条件
# ==================================================================
def comfort_index(
    temperature: float | None,
    humidity: float | None,
    wind_speed_kmh: float | None,
) -> float | None:
    """人体舒适度指数（0-100，越高越舒适）。

    以温湿指数 (THI) 为主干，叠加风冷修正：
    经验上 22-26°C 且湿度 40-60% 时体感最佳。
    """
    if temperature is None or temperature != temperature:
        return None
    humidity = 55.0 if humidity is None or humidity != humidity else humidity
    wind = 0.0 if wind_speed_kmh is None or wind_speed_kmh != wind_speed_kmh else wind_speed_kmh

    # 温湿指数（华氏度形式），适宜区间中心约 70
    thi = temperature * 1.8 + 32 - (0.55 - 0.0055 * humidity) * (temperature * 1.8 - 26)
    comfort = 100.0 - abs(thi - 70.0) * 3.2
    # 风速修正：微风加分，强风减分
    if wind <= 20:
        comfort += 3.0
    elif wind > 40:
        comfort -= min(18.0, (wind - 40) * 0.45)
    return round(max(0.0, min(100.0, comfort)), 2)


def ventilation_index(
    wind_speed_kmh: float | None,
    boundary_layer_height_m: float | None = None,
) -> float | None:
    """通风扩散指数：风速（m/s）× 混合层高度（m）的近似。

    Open-Meteo 免费接口不提供混合层高度，此处以风速驱动并叠加一个
    经验性日变化因子（午后边界层更厚）作为近似，仅用于相对比较。
    """
    if wind_speed_kmh is None or wind_speed_kmh != wind_speed_kmh:
        return None
    wind_ms = max(0.0, wind_speed_kmh) / 3.6
    height = boundary_layer_height_m if boundary_layer_height_m else 600.0
    return round(wind_ms * height, 2)


def is_stagnant(
    wind_speed_kmh: float | None,
    precipitation: float | None,
    *,
    wind_threshold_kmh: float = 8.0,
    precipitation_threshold_mm: float = 0.1,
) -> int | None:
    """静稳天气判据：近地面风速小且无有效降水，污染物易累积。"""
    if wind_speed_kmh is None or wind_speed_kmh != wind_speed_kmh:
        return None
    rain = 0.0 if precipitation is None or precipitation != precipitation else precipitation
    return int(wind_speed_kmh < wind_threshold_kmh and rain < precipitation_threshold_mm)


# ==================================================================
# WMO 天气现象代码字典
# ==================================================================
WMO_WEATHER_CODES: dict[int, str] = {
    0: "晴",
    1: "晴间多云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "雾凇",
    51: "毛毛雨（弱）",
    53: "毛毛雨（中）",
    55: "毛毛雨（强）",
    56: "冻毛毛雨（弱）",
    57: "冻毛毛雨（强）",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨（弱）",
    67: "冻雨（强）",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨（弱）",
    81: "阵雨（中）",
    82: "阵雨（强）",
    85: "阵雪（弱）",
    86: "阵雪（强）",
    95: "雷阵雨",
    96: "雷阵雨伴小冰雹",
    99: "雷阵雨伴强冰雹",
}


def weather_label(code: int | float | None) -> str:
    """WMO 天气代码 → 中文描述。"""
    if code is None or code != code:
        return "未知"
    try:
        return WMO_WEATHER_CODES.get(int(code), "未知")
    except (TypeError, ValueError):
        return "未知"


WMO_CODE_VALUES: tuple[int, ...] = tuple(sorted(WMO_WEATHER_CODES))


# ==================================================================
# 国标 AQI（GB 3095-2012 / HJ 633-2012）
# ----------------------------------------------------------------
# 为什么平台需要两套 AQI：
# 欧洲 AQI（EAQI）与国标 AQI 的刻度差异极大 —— EAQI 的"良"上界 40 大致对应
# PM2.5 ≈ 20 μg/m³，而国标 AQI 100（"良"上界）对应 PM2.5 日均 75 μg/m³。
# 用 EAQI ≤ 40 统计"优良天"，国内城市会普遍得到个位数比例，与考核口径不可比。
# 因此：**合规考核用国标 AQI，国际对比用 EAQI**，两者并存且分别标注。
# ==================================================================

# IAQI 指数刻度
IAQI_SCALE: tuple[int, ...] = (0, 50, 100, 150, 200, 300, 400, 500)

# 各污染物的浓度限值断点（与 IAQI_SCALE 一一对应）
# 单位：PM2.5/PM10/O₃/NO₂/SO₂ 为 μg/m³，CO 为 mg/m³
IAQI_BREAKPOINTS: dict[str, tuple[float, ...]] = {
    # 24 小时平均
    "pm2_5": (0, 35, 75, 115, 150, 250, 350, 500),
    "pm10": (0, 50, 150, 250, 350, 420, 500, 600),
    "sulphur_dioxide": (0, 50, 150, 475, 800, 1600, 2100, 2620),
    "nitrogen_dioxide": (0, 40, 80, 180, 280, 565, 750, 940),
    "carbon_monoxide": (0, 2, 4, 14, 24, 36, 48, 60),
    # 8 小时滑动平均
    "ozone": (0, 100, 160, 215, 265, 800, 800, 800),
}

# 国标优良天上界：AQI ≤ 100 即"优"或"良"
CHINA_GOOD_CEILING = 100.0

# 国标 AQI 的 CO 以 mg/m³ 计，而平台存储为 μg/m³
_CO_UNIT_DIVISOR = 1000.0

_CHINA_LEVELS: tuple[tuple[float, str], ...] = (
    (50.0, "优"),
    (100.0, "良"),
    (150.0, "轻度污染"),
    (200.0, "中度污染"),
    (300.0, "重度污染"),
    (math.inf, "严重污染"),
)

CHINA_POLLUTANT_LABELS: dict[str, str] = {
    "pm2_5": "PM2.5",
    "pm10": "PM10",
    "ozone": "O₃",
    "nitrogen_dioxide": "NO₂",
    "sulphur_dioxide": "SO₂",
    "carbon_monoxide": "CO",
}


def iaqi(pollutant: str, concentration: float | None) -> float | None:
    """单项污染物空气质量分指数（分段线性插值，HJ 633-2012 方法）。"""
    if concentration is None or concentration != concentration or concentration < 0:
        return None
    table = IAQI_BREAKPOINTS.get(pollutant)
    if table is None:
        return None
    value = concentration / _CO_UNIT_DIVISOR if pollutant == "carbon_monoxide" else concentration
    if value >= table[-1]:
        return 500.0
    for index in range(1, len(table)):
        if value <= table[index]:
            low_c, high_c = table[index - 1], table[index]
            low_i, high_i = IAQI_SCALE[index - 1], IAQI_SCALE[index]
            span = high_c - low_c
            if span <= 0:
                return float(high_i)
            return round(low_i + (value - low_c) / span * (high_i - low_i), 2)
    return 500.0


def china_aqi(row: dict[str, float | None]) -> tuple[float | None, str, str]:
    """由污染物浓度计算国标 AQI。

    返回 ``(AQI, 等级, 首要污染物)``。AQI 取各污染物 IAQI 的最大值；
    首要污染物为 IAQI 最大且超过 50 的污染物，全部 ≤ 50 时记为"无"。
    """
    best_value: float | None = None
    best_key: str | None = None
    for pollutant in IAQI_BREAKPOINTS:
        value = iaqi(pollutant, row.get(pollutant))
        if value is None:
            continue
        if best_value is None or value > best_value:
            best_value, best_key = value, pollutant
    if best_value is None:
        return None, "", "无"
    label = CHINA_POLLUTANT_LABELS.get(best_key or "", "无")
    primary = label if best_value > 50 else "无"
    return best_value, china_aqi_level(best_value), primary


def china_aqi_level(value: float | None) -> str | None:
    """国标 AQI → 等级中文表述。"""
    if value is None or value != value:
        return None
    for upper, level in _CHINA_LEVELS:
        if value <= upper:
            return level
    return "严重污染"


CHINA_HEALTH_ADVICE: dict[str, str] = {
    "优": "空气质量令人满意，基本无空气污染，各类人群可正常活动。",
    "良": "空气质量可接受，极少数异常敏感人群应减少户外活动。",
    "轻度污染": "易感人群症状有轻度加剧，儿童、老年人及心脏病、呼吸系统疾病患者应减少长时间、高强度的户外锻炼。",
    "中度污染": "进一步加剧易感人群症状，建议疾病患者避免长时间、高强度的户外锻炼，一般人群适量减少户外运动。",
    "重度污染": "心脏病和肺病患者症状显著加剧，建议儿童、老年人和心脏病、肺病患者应停留在室内，停止户外运动。",
    "严重污染": "健康人群运动耐受力降低，有明显强烈症状，建议儿童、老年人和病人应当留在室内，避免体力消耗，一般人群应避免户外活动。",
}


# ==================================================================
# 预警阈值（看板与预警接口共用）
# ==================================================================
@dataclass(frozen=True)
class AlertRule:
    """单条环境预警规则。"""

    key: str
    name: str
    metric: str
    operator: str  # ">=" | "<=" | ">"
    threshold: float
    severity: str  # info | warning | severe
    advice: str
    unit: str | None = None


ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        key="air.severe_pollution",
        name="严重污染",
        metric="european_aqi",
        operator=">=",
        threshold=100.0,
        severity="severe",
        advice="AQI 达严重污染水平，建议停止户外活动并关闭门窗。",
        unit="EAQI",
    ),
    AlertRule(
        key="air.heavy_pollution",
        name="重度污染",
        metric="european_aqi",
        operator=">=",
        threshold=80.0,
        severity="severe",
        advice="AQI 达重度污染，儿童、老年人及心肺疾病患者应留在室内。",
        unit="EAQI",
    ),
    AlertRule(
        key="air.moderate_pollution",
        name="中度污染",
        metric="european_aqi",
        operator=">=",
        threshold=60.0,
        severity="warning",
        advice="AQI 达中度污染，敏感人群应减少户外活动。",
        unit="EAQI",
    ),
    AlertRule(
        key="air.light_pollution",
        name="轻度污染",
        metric="european_aqi",
        operator=">=",
        threshold=40.0,
        severity="info",
        advice="AQI 达轻度污染，敏感人群注意防护。",
        unit="EAQI",
    ),
    AlertRule(
        key="weather.heat_wave",
        name="高温",
        metric="temperature_2m",
        operator=">=",
        threshold=35.0,
        severity="severe",
        advice="气温超过 35°C，注意防暑降温，避免午后长时间户外作业。",
        unit="°C",
    ),
    AlertRule(
        key="weather.heat_warning",
        name="较高气温",
        metric="temperature_2m",
        operator=">=",
        threshold=32.0,
        severity="warning",
        advice="气温超过 32°C，注意补水与防晒。",
        unit="°C",
    ),
    AlertRule(
        key="weather.frost",
        name="低温冰冻",
        metric="temperature_2m",
        operator="<=",
        threshold=-10.0,
        severity="severe",
        advice="气温低于 -10°C，注意防寒防冻与管道保温。",
        unit="°C",
    ),
    AlertRule(
        key="weather.gale",
        name="大风",
        metric="wind_speed_10m",
        operator=">=",
        threshold=50.0,
        severity="warning",
        advice="风速达 50 km/h 以上，注意高空作业与临时构筑物安全。",
        unit="km/h",
    ),
    AlertRule(
        key="weather.strong_gust",
        name="强阵风",
        metric="wind_gusts_10m",
        operator=">=",
        threshold=75.0,
        severity="warning",
        advice="阵风超过 75 km/h，注意树木倒伏与行车安全。",
        unit="km/h",
    ),
    AlertRule(
        key="weather.heavy_rain",
        name="强降水",
        metric="precipitation",
        operator=">=",
        threshold=8.0,
        severity="severe",
        advice="小时降水量超过 8 mm，注意城市内涝与积水路段。",
        unit="mm",
    ),
    AlertRule(
        key="weather.low_visibility",
        name="低能见度",
        metric="visibility",
        operator="<=",
        threshold=1000.0,
        severity="warning",
        advice="能见度低于 1 km，注意行车安全。",
        unit="m",
    ),
    AlertRule(
        key="weather.high_uv",
        name="强紫外线",
        metric="uv_index",
        operator=">=",
        threshold=8.0,
        severity="info",
        advice="紫外线很强，外出请做好防晒。",
        unit="index",
    ),
)


def _alert_rank(rule: AlertRule) -> tuple[int, float]:
    """预警优选序：先比严重级别，同级再比阈值严苛程度（越小越优先）。

    对于 ">=" 类规则，阈值越高越严苛；对于 "<=" 类规则则相反。
    """
    severity_rank = {"severe": 0, "warning": 1, "info": 2}
    direction = -1.0 if rule.operator in (">=", ">") else 1.0
    return (severity_rank.get(rule.severity, 9), direction * rule.threshold)


def evaluate_alerts(row: dict[str, float | None]) -> list[dict[str, object]]:
    """对一条环境记录评估全部预警规则，返回触发的预警列表。

    同一指标上存在**分级规则**（例如 AQI 的轻度/中度/重度/严重污染），
    数值越界时会同时命中多条。对外只保留最严重的那一条，
    否则一条 AQI=124 的记录会报出三条污染预警，淹没真正需要关注的信息。
    """
    severity_rank = {"severe": 0, "warning": 1, "info": 2}
    best_per_metric: dict[str, tuple[tuple[int, float], dict[str, object]]] = {}

    for rule in ALERT_RULES:
        value = row.get(rule.metric)
        if value is None or value != value:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        hit = (
            (rule.operator == ">=" and numeric >= rule.threshold)
            or (rule.operator == "<=" and numeric <= rule.threshold)
            or (rule.operator == ">" and numeric > rule.threshold)
        )
        if not hit:
            continue

        payload: dict[str, object] = {
            "key": rule.key,
            "name": rule.name,
            "metric": rule.metric,
            "value": round(numeric, 2),
            "threshold": rule.threshold,
            "operator": rule.operator,
            "unit": rule.unit,
            "severity": rule.severity,
            "advice": rule.advice,
        }
        rank = _alert_rank(rule)
        current = best_per_metric.get(rule.metric)
        if current is None or rank < current[0]:
            best_per_metric[rule.metric] = (rank, payload)

    triggered = [item[1] for item in best_per_metric.values()]
    return sorted(triggered, key=lambda item: severity_rank.get(str(item["severity"]), 9))


__all__ = [
    "EAQI_BANDS",
    "GOOD_AQI_CEILING",
    "AQI_HEALTH_ADVICE",
    "aqi_level",
    "aqi_level_index",
    "exceedance_hours_threshold",
    "IAQI_SCALE",
    "IAQI_BREAKPOINTS",
    "CHINA_GOOD_CEILING",
    "CHINA_POLLUTANT_LABELS",
    "CHINA_HEALTH_ADVICE",
    "iaqi",
    "china_aqi",
    "china_aqi_level",
    "POLLUTANT_STANDARDS",
    "PollutantStandard",
    "pollutant_load",
    "primary_pollutant",
    "comfort_index",
    "ventilation_index",
    "is_stagnant",
    "WMO_WEATHER_CODES",
    "WMO_CODE_VALUES",
    "weather_label",
    "AlertRule",
    "ALERT_RULES",
    "evaluate_alerts",
]
