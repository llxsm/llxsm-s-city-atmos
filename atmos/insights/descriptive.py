"""描述性分析：统计分布与时段规律画像。

这一组回答的是"**数据长什么样**"：
* 分布 —— 集中趋势、离散程度、长尾与离群点；
* 相关 —— 指标之间是否同向变化；
* 时段规律 —— 一天之内、一周之内、一年之内的重复模式。

它们本身不给出因果结论，但是后续"污染过程""气象关系"分析的解释基础。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from atmos.insights import numeric
from atmos.insights.loader import FrameLoader, numeric_columns
from atmos.logging_setup import get_logger

logger = get_logger("insights.descriptive")

WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# 环境分析中默认关注的指标组合
DEFAULT_DISTRIBUTION_METRICS = ("temperature_2m", "relative_humidity_2m", "wind_speed_10m", "european_aqi", "pm2_5")
DEFAULT_CORRELATION_METRICS = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "precipitation",
    "pressure_msl",
    "pm2_5",
    "pm10",
    "ozone",
    "european_aqi",
)
DEFAULT_PROFILE_METRICS = ("temperature_2m", "european_aqi", "pm2_5", "wind_speed_10m")


# ==================================================================
# 统计与分布
# ==================================================================
def distribution(
    loader: FrameLoader,
    *,
    metrics: list[str] | None = None,
    cities: list[str] | None = None,
    days: int = 30,
    bins: int = 20,
) -> dict[str, Any]:
    """多指标描述统计 + 箱线图 + 直方图。

    同时给出**全域**与**逐城市**两个层级：全域用于把握整体水平，
    逐城市用于发现"哪个城市是异常源"。
    """
    names = [item for item in (metrics or list(DEFAULT_DISTRIBUTION_METRICS)) if item]
    frame = loader.environment_hourly(cities, days=days, columns=["city_id", "time", *names])
    if frame.empty:
        return _empty("尚无逐小时数据，请先执行采集")

    payload_metrics: list[dict[str, Any]] = []
    for name in names:
        if name not in frame.columns:
            continue
        values = pd.to_numeric(frame[name], errors="coerce").dropna().to_numpy(dtype=float)
        if values.size == 0:
            continue

        per_city: list[dict[str, Any]] = []
        for slug, group in frame.groupby("city_id"):
            series = pd.to_numeric(group[name], errors="coerce").dropna().to_numpy(dtype=float)
            if series.size == 0:
                continue
            stats = numeric.describe(series)
            box = numeric.boxplot(series)
            per_city.append(
                {
                    "city_slug": str(slug),
                    "city_name": loader.city_label(str(slug)),
                    **stats,
                    "boxplot": box.to_payload() if box else None,
                }
            )
        per_city.sort(key=lambda item: item.get("mean") or 0, reverse=True)

        box = numeric.boxplot(values)
        meta = loader.metric_meta(name)
        payload_metrics.append(
            {
                "metric": name,
                "label": meta.get("label"),
                "unit": meta.get("unit"),
                "description": meta.get("description"),
                "overall": numeric.describe(values),
                "boxplot": box.to_payload() if box else None,
                "histogram": numeric.histogram(values, bins=bins),
                "by_city": per_city,
            }
        )

    return {
        "window_days": days,
        "row_count": int(len(frame)),
        "city_count": int(frame["city_id"].nunique()),
        "city_names": [loader.city_label(str(slug)) for slug in sorted(frame["city_id"].unique())],
        "metrics": payload_metrics,
        "generated_at": datetime.utcnow().isoformat(),
    }


def correlation(
    frame_loader: FrameLoader,
    *,
    metrics: list[str] | None = None,
    cities: list[str] | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """多指标相关矩阵（同时给出 Pearson 与 Spearman）。

    两者一起看才有意义：Pearson 捕捉线性关系，Spearman 捕捉单调关系；
    若 Spearman 明显强于 Pearson，说明关系是单调但非线性的（例如
    "风速越大 PM2.5 越低，但边际效应递减"）。
    """
    names = [item for item in (metrics or list(DEFAULT_CORRELATION_METRICS)) if item]
    frame = frame_loader.environment_hourly(
        cities, days=days, columns=["city_id", "time", *names], require=names[:2]
    )
    if frame.empty:
        return _empty("尚无逐小时数据，请先执行采集")

    columns = numeric_columns(frame, names)
    if len(columns) < 2:
        return _empty("可用指标不足两个，无法计算相关性")

    pearson_matrix = numeric.correlation_matrix(columns, method="pearson")
    spearman_matrix = numeric.correlation_matrix(columns, method="spearman")

    labels = pearson_matrix["labels"]
    highlights: list[dict[str, Any]] = []
    for row in range(len(labels)):
        for col in range(row + 1, len(labels)):
            value = pearson_matrix["values"][row][col]
            if value is None:
                continue
            highlights.append(
                {
                    "left": labels[row],
                    "left_label": frame_loader.metric_label(labels[row]),
                    "right": labels[col],
                    "right_label": frame_loader.metric_label(labels[col]),
                    "pearson": value,
                    "spearman": spearman_matrix["values"][row][col],
                    "strength": numeric.correlation_label(value),
                }
            )
    highlights.sort(key=lambda item: -abs(item["pearson"]))

    return {
        "window_days": days,
        "row_count": int(len(frame)),
        "labels": labels,
        "labels_zh": [frame_loader.metric_label(name) for name in labels],
        "units": [frame_loader.metric_meta(name).get("unit") for name in labels],
        "pearson": pearson_matrix["values"],
        "spearman": spearman_matrix["values"],
        "highlights": highlights[:12],
        "cities": sorted(frame["city_id"].unique().tolist()),
    }


# ==================================================================
# 时段规律画像
# ==================================================================
def hourly_profile(
    loader: FrameLoader,
    *,
    metric: str = "european_aqi",
    cities: list[str] | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """日内小时画像：城市 × 小时的热力图数据。

    这是最直观的"规律"分析 —— 能一眼看出某城市是否在早晚高峰出现污染抬升。
    """
    frame = loader.environment_hourly(cities, days=days, columns=["city_id", "time", metric])
    if frame.empty or metric not in frame.columns:
        return _empty("尚无逐小时数据，请先执行采集")

    frame = frame.assign(hour=frame["time"].dt.hour, value=pd.to_numeric(frame[metric], errors="coerce"))
    frame = frame.dropna(subset=["value"])
    if frame.empty:
        return _empty("所选指标在窗口内无有效数据")

    pivot = frame.pivot_table(index="city_id", columns="hour", values="value", aggfunc="mean")
    pivot = pivot.reindex(columns=range(24))

    rows: list[dict[str, Any]] = []
    for slug in pivot.index:
        values = pivot.loc[slug].to_numpy(dtype=float)
        if np.all(np.isnan(values)):
            continue
        peak_hour = int(np.nanargmax(values))
        trough_hour = int(np.nanargmin(values))
        rows.append(
            {
                "city_slug": str(slug),
                "city_name": loader.city_label(str(slug)),
                "values": [None if np.isnan(item) else round(float(item), 3) for item in values],
                "peak_hour": peak_hour,
                "peak_value": round(float(np.nanmax(values)), 3),
                "trough_hour": trough_hour,
                "trough_value": round(float(np.nanmin(values)), 3),
                "amplitude": round(float(np.nanmax(values) - np.nanmin(values)), 3),
            }
        )
    rows.sort(key=lambda item: -(item["amplitude"] or 0))

    meta = loader.metric_meta(metric)
    return {
        "metric": metric,
        "label": meta.get("label"),
        "unit": meta.get("unit"),
        "window_days": days,
        "hours": list(range(24)),
        "rows": rows,
        "overall": [
            None if np.isnan(item) else round(float(item), 3)
            for item in pivot.to_numpy(dtype=float).mean(axis=0)
        ],
        "most_volatile": rows[0]["city_name"] if rows else None,
    }


def weekly_profile(
    loader: FrameLoader,
    *,
    metrics: list[str] | None = None,
    cities: list[str] | None = None,
    days: int = 90,
) -> dict[str, Any]:
    """周内规律：工作日 / 周末对比 + 周一至周日逐日水平。

    工作日与周末的差异能反映人为活动对空气质量的影响强度。
    """
    names = [item for item in (metrics or list(DEFAULT_PROFILE_METRICS)) if item]
    frame = loader.environment_daily(cities, days=days)
    if frame.empty:
        return _empty("尚无日汇总数据，请先执行采集")

    frame = frame.assign(weekday=frame["date"].dt.weekday, is_weekend=(frame["date"].dt.weekday >= 5))

    metrics_payload: list[dict[str, Any]] = []
    for name in names:
        column = loader.resolve_daily_column(frame, name)
        if column is None:
            continue
        values = pd.to_numeric(frame[column], errors="coerce")
        working = frame.assign(_v=values).dropna(subset=["_v"])
        if working.empty:
            continue

        # 三个值都按同一精度返回，且 delta 由**取整后**的两个均值相减得到：
        # 若各自独立取整，页面上就会出现 "周末均值 − 工作日均值 ≠ delta" 的
        # 0.001 级不一致，调用方按返回字段自行校验时会对不上。
        weekday_mean = round(float(working.loc[~working["is_weekend"], "_v"].mean()), 3)
        weekend_mean = round(float(working.loc[working["is_weekend"], "_v"].mean()), 3)
        delta = round(weekend_mean - weekday_mean, 3)
        by_weekday = [
            round(float(working.loc[working["weekday"] == index, "_v"].mean()), 3)
            if (working["weekday"] == index).any()
            else None
            for index in range(7)
        ]
        base = loader.daily_base_metric(column)
        meta = loader.metric_meta(base)
        metrics_payload.append(
            {
                "metric": column,
                "base_metric": base,
                "label": meta.get("label") or name,
                "unit": meta.get("unit"),
                "weekday_mean": weekday_mean,
                "weekend_mean": weekend_mean,
                "delta": delta,
                "delta_ratio": round(delta / weekday_mean * 100, 2) if weekday_mean else None,
                "by_weekday": by_weekday,
                "higher_on": "周末" if delta > 0 else "工作日",
            }
        )

    return {
        "window_days": days,
        "weekday_labels": list(WEEKDAY_LABELS),
        "day_count": int(len(frame)),
        "metrics": metrics_payload,
    }


def monthly_trend(
    loader: FrameLoader,
    *,
    metric: str = "european_aqi",
    cities: list[str] | None = None,
    days: int = 92,
) -> dict[str, Any]:
    """月度变化：用于观察季节性趋势与治理成效。"""
    frame = loader.environment_daily(cities, days=days)
    if frame.empty:
        return _empty("尚无日汇总数据，请先执行采集")

    column = loader.resolve_daily_column(frame, metric)
    if column is None:
        return _empty(f"指标 {metric} 在日汇总资产中不可用")

    working = frame.assign(
        month=frame["date"].dt.strftime("%Y-%m"),
        value=pd.to_numeric(frame[column], errors="coerce"),
    ).dropna(subset=["value"])
    if working.empty:
        return _empty("所选指标在窗口内无有效数据")

    grouped = working.groupby("month")["value"]
    months = [
        {
            "month": str(month),
            "count": int(series.size),
            "mean": round(float(series.mean()), 3),
            "max": round(float(series.max()), 3),
            "min": round(float(series.min()), 3),
        }
        for month, series in grouped
    ]
    months.sort(key=lambda item: item["month"])

    base = loader.daily_base_metric(column)
    meta = loader.metric_meta(base)
    return {
        "metric": column,
        "base_metric": base,
        "label": meta.get("label") or metric,
        "unit": meta.get("unit"),
        "window_days": days,
        "months": months,
        "city_count": int(working["city_id"].nunique()),
    }


def _empty(message: str) -> dict[str, Any]:
    """统一空状态：前端据此给出"先去采集"的引导。"""
    return {"available": False, "message": message, "metrics": [], "rows": []}


__all__ = [
    "distribution",
    "correlation",
    "hourly_profile",
    "weekly_profile",
    "monthly_trend",
    "DEFAULT_DISTRIBUTION_METRICS",
    "DEFAULT_CORRELATION_METRICS",
    "DEFAULT_PROFILE_METRICS",
    "WEEKDAY_LABELS",
]
