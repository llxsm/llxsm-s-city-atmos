"""异常检测与污染过程识别。

区别在于粒度：
* **异常点** —— 单个时刻的数值显著偏离常态（回答"哪个点不对"）；
* **污染过程** —— 把连续超标时段合并成一次事件，给出起止、持续时长、峰值与
  期间的气象条件（回答"这次污染是怎么发生的"）。

业务价值在于后者：考核通报、成因分析、应急响应都是按"过程"而不是按"小时"组织的。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from atmos.insights import numeric
from atmos.insights.loader import FrameLoader
from atmos.logging_setup import get_logger
from atmos.standards import CHINA_GOOD_CEILING, china_aqi_level, primary_pollutant

logger = get_logger("insights.episodes")

# 一次污染过程允许被合并的最大"清洁间隙"。
# 若不做合并，一次持续两天的污染过程会因为中间某个小时的短暂好转被切成十几段。
DEFAULT_MIN_GAP_HOURS = 3
# 短于此长度的过程不单独成案（抖动噪声）
DEFAULT_MIN_DURATION_HOURS = 2

POLLUTANT_COLUMNS = (
    "pm2_5",
    "pm10",
    "ozone",
    "nitrogen_dioxide",
    "sulphur_dioxide",
    "carbon_monoxide",
)


# ==================================================================
# 异常点检测
# ==================================================================
def detect_outliers(
    loader: FrameLoader,
    *,
    metric: str = "european_aqi",
    cities: list[str] | None = None,
    days: int = 30,
    method: str = "zscore",
    threshold: float = 3.0,
) -> dict[str, Any]:
    """基于统计方法标记异常点。

    ``zscore`` 适合近似正态的指标（气温、气压）；``iqr`` 适合长尾指标
    （PM2.5、AQI），因为均值和标准差本身会被极端污染值拉偏。
    """
    frame = loader.environment_hourly(cities, days=days, columns=["city_id", "time", metric])
    if frame.empty or metric not in frame.columns:
        return {"available": False, "message": "尚无逐小时数据，请先执行采集", "points": []}

    per_city: list[dict[str, Any]] = []
    all_points: list[dict[str, Any]] = []
    total = 0
    flagged = 0

    for slug, group in frame.groupby("city_id"):
        series = pd.to_numeric(group[metric], errors="coerce")
        working = group.assign(_value=series).dropna(subset=["_value"])
        if working.empty:
            continue
        values = working["_value"].to_numpy(dtype=float)
        if values.size < numeric.MIN_SAMPLES:
            continue

        if method == "iqr":
            mask = numeric.outliers_by_iqr(values, fence=threshold)
            scores = numeric.robust_zscore(values)
        else:
            mask = numeric.outliers_by_zscore(values, threshold=threshold)
            scores = numeric.zscore(values)

        z_values = working.assign(_z=scores)
        hits = z_values.loc[mask]
        total += int(values.size)
        flagged += int(mask.sum())

        points = [
            {
                "city_slug": str(slug),
                "city_name": loader.city_label(str(slug)),
                "time": row["time"].isoformat(),
                "value": round(float(row["_value"]), 3),
                "z": round(float(row["_z"]), 3),
            }
            for _, row in hits.sort_values("_z", key=lambda item: -item.abs()).head(60).iterrows()
        ]
        all_points.extend(points)
        per_city.append(
            {
                "city_slug": str(slug),
                "city_name": loader.city_label(str(slug)),
                "samples": int(values.size),
                "flagged": int(mask.sum()),
                "flagged_ratio": round(float(mask.sum()) / values.size * 100, 3),
                "mean": round(float(values.mean()), 3),
                "std": round(float(values.std(ddof=1)), 3) if values.size > 1 else 0.0,
                "max_z": round(float(np.max(np.abs(scores))), 3) if values.size else None,
                "top_points": points[:6],
            }
        )

    all_points.sort(key=lambda item: -abs(item["z"]))
    per_city.sort(key=lambda item: -item["flagged_ratio"])
    meta = loader.metric_meta(metric)

    return {
        "available": bool(per_city),
        "metric": metric,
        "label": meta.get("label") or metric,
        "unit": meta.get("unit"),
        "method": method,
        "threshold": threshold,
        "window_days": days,
        "samples": total,
        "flagged": flagged,
        "flagged_ratio": round(flagged / total * 100, 3) if total else 0.0,
        "by_city": per_city,
        "points": all_points[:200],
    }


# ==================================================================
# 污染过程识别
# ==================================================================
def detect_episodes(
    loader: FrameLoader,
    *,
    cities: list[str] | None = None,
    days: int = 92,
    threshold: float = CHINA_GOOD_CEILING,
    min_gap_hours: int = DEFAULT_MIN_GAP_HOURS,
    min_duration_hours: int = DEFAULT_MIN_DURATION_HOURS,
    limit: int = 30,
) -> dict[str, Any]:
    """识别连续超标时段并合并为污染过程。

    判定依据是**国标 AQI**（GB 3095-2012）：默认阈值 100 即国标"优良"上界，
    因此识别出的过程与国内考核口径一致。合并规则为：两次超标之间若间隔不超过
    ``min_gap_hours`` 个达标小时，视为同一次过程 —— 否则一次过程会被中间偶然的
    达标小时切碎，失去"过程"的业务意义。
    """
    columns = [
        "city_id",
        "time",
        "china_aqi",
        "european_aqi",
        "pm2_5",
        "pm10",
        "ozone",
        "nitrogen_dioxide",
        "wind_speed_10m",
        "relative_humidity_2m",
        "precipitation",
        "is_stagnant",
    ]
    frame = loader.environment_hourly(cities, days=days, columns=columns, require=["china_aqi"])
    if frame.empty:
        return {
            "available": False,
            "message": "尚无逐小时数据（或该窗口内国标 AQI 全部缺失），请先执行采集",
            "episodes": [],
        }

    episodes: list[dict[str, Any]] = []
    for slug, group in frame.groupby("city_id"):
        ordered = group.sort_values("time")
        values = pd.to_numeric(ordered["china_aqi"], errors="coerce").to_numpy(dtype=float)
        times = ordered["time"].to_numpy()
        episodes.extend(
            _extract(
                loader,
                str(slug),
                ordered,
                values,
                times,
                threshold,
                min_gap_hours,
                min_duration_hours,
                window_hours=len(ordered),
            )
        )

    episodes.sort(key=lambda item: (-item["peak_value"], -item["duration_hours"]))
    for index, item in enumerate(episodes, start=1):
        item["rank"] = index

    levels: dict[str, int] = {}
    for item in episodes:
        levels[item["peak_level"]] = levels.get(item["peak_level"], 0) + 1

    total_hours = int(sum(item["duration_hours"] for item in episodes))
    chronic = [item for item in episodes if item["chronic"]]
    return {
        "available": True,
        "aqi_standard": "国标 AQI（GB 3095-2012）",
        "threshold": threshold,
        "threshold_label": china_aqi_level(threshold),
        "min_gap_hours": min_gap_hours,
        "min_duration_hours": min_duration_hours,
        "window_days": days,
        "city_count": int(frame["city_id"].nunique()),
        "episode_count": len(episodes),
        "total_hours": total_hours,
        "peak_level_distribution": levels,
        "worst_city": _worst_city(episodes),
        "chronic_episode_count": len(chronic),
        "chronic_cities": sorted({item["city_name"] for item in chronic}),
        "episodes": episodes[:limit],
    }


def _extract(
    loader: FrameLoader,
    slug: str,
    ordered: pd.DataFrame,
    values: np.ndarray,
    times: np.ndarray,
    threshold: float,
    min_gap_hours: int,
    min_duration_hours: int,
    *,
    window_hours: int,
) -> list[dict[str, Any]]:
    """把单个城市的超标布尔序列切成污染过程。"""
    exceed = values > threshold
    if not exceed.any():
        return []

    # 找出所有连续超标区间（允许被 min_gap_hours 以内的达标时段桥接）
    segments: list[tuple[int, int]] = []
    start: int | None = None
    gap = 0
    for index, flag in enumerate(exceed):
        if flag:
            if start is None:
                start = index
            gap = 0
        elif start is not None:
            gap += 1
            if gap > min_gap_hours:
                segments.append((start, index - gap))
                start = None
                gap = 0
    if start is not None:
        segments.append((start, len(exceed) - 1 - gap))

    records = ordered.to_dict("records")
    episodes: list[dict[str, Any]] = []
    for begin, end in segments:
        if end < begin:
            continue
        duration = end - begin + 1
        if duration < min_duration_hours:
            continue
        window = values[begin : end + 1]
        valid = window[~np.isnan(window)]
        if valid.size == 0:
            continue

        peak_offset = int(np.nanargmax(window))
        peak_value = float(window[peak_offset])
        peak_time = pd.Timestamp(times[begin + peak_offset])

        exceed_slice = records[begin : end + 1]
        exceed_rows = [
            row for row in exceed_slice if row.get("european_aqi") is not None and row["european_aqi"] > threshold
        ]
        pollutants = {
            name: _mean_of(exceed_slice, name) for name in POLLUTANT_COLUMNS if name in ordered.columns
        }
        primary = primary_pollutant({key: value for key, value in pollutants.items() if value is not None})
        # 长过程（超过窗口四分之一）本质是"持续性污染"而不是一次事件，
        # 单独标记出来，避免把一座长期超标的城市描述成"发生了一次污染过程"。
        chronic = window_hours > 0 and duration / window_hours > 0.25

        episodes.append(
            {
                "city_slug": slug,
                "city_name": loader.city_label(slug),
                "start": pd.Timestamp(times[begin]).isoformat(),
                "end": pd.Timestamp(times[end]).isoformat(),
                "duration_hours": duration,
                "exceed_hours": len(exceed_rows),
                "peak_value": round(peak_value, 2),
                "peak_time": peak_time.isoformat(),
                "peak_level": china_aqi_level(peak_value),
                "mean_value": round(float(valid.mean()), 2),
                "threshold": threshold,
                "primary_pollutant": primary,
                "chronic": chronic,
                "window_share": round(duration / window_hours * 100, 1) if window_hours else None,
                "pollutants": {key: None if value is None else round(value, 2) for key, value in pollutants.items()},
                "weather": {
                    "wind_speed_avg": _mean_of(exceed_slice, "wind_speed_10m", digits=2),
                    "humidity_avg": _mean_of(exceed_slice, "relative_humidity_2m", digits=1),
                    "precipitation_sum": _sum_of(exceed_slice, "precipitation", digits=2),
                    "stagnant_hours": _sum_of(exceed_slice, "is_stagnant", digits=0),
                },
                "severity": _severity(peak_value, duration),
            }
        )
    return episodes


def _severity(peak: float, duration: int) -> str:
    """过程严重度：峰值等级与持续时长共同决定（按国标等级）。"""
    order = {"优": 0, "良": 1, "轻度污染": 2, "中度污染": 3, "重度污染": 4, "严重污染": 5}
    rank = order.get(china_aqi_level(peak) or "优", 0)
    score = rank * 10 + min(duration, 24)
    if score >= 50:
        return "严重"
    if score >= 35:
        return "较重"
    if score >= 22:
        return "中等"
    return "轻微"


def _mean_of(rows: list[dict[str, Any]], column: str, *, digits: int = 2) -> float | None:
    values = [
        float(row[column])
        for row in rows
        if row.get(column) is not None and not (isinstance(row[column], float) and row[column] != row[column])
    ]
    if not values:
        return None
    return round(sum(values) / len(values), digits)


def _sum_of(rows: list[dict[str, Any]], column: str, *, digits: int = 2) -> float | None:
    values = [
        float(row[column])
        for row in rows
        if row.get(column) is not None and not (isinstance(row[column], float) and row[column] != row[column])
    ]
    if not values:
        return None
    return round(sum(values), digits)


def _worst_city(episodes: list[dict[str, Any]]) -> dict[str, Any] | None:
    """累计超标时长最长的城市。"""
    if not episodes:
        return None
    totals: dict[str, dict[str, Any]] = {}
    for item in episodes:
        bucket = totals.setdefault(
            item["city_slug"], {"city_slug": item["city_slug"], "city_name": item["city_name"], "hours": 0, "count": 0, "peak": 0.0}
        )
        bucket["hours"] += item["duration_hours"]
        bucket["count"] += 1
        bucket["peak"] = max(bucket["peak"], item["peak_value"])
    worst = max(totals.values(), key=lambda item: item["hours"])
    return {**worst, "peak": round(worst["peak"], 2)}


__all__ = ["detect_outliers", "detect_episodes", "DEFAULT_MIN_GAP_HOURS", "DEFAULT_MIN_DURATION_HOURS"]
