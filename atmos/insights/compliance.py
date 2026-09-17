"""达标考核与报表。

把逐小时的原始观测转成**可考核的口径**：优良天、超标天/小时、等级天数分布、
环比与同比、目标完成度。这是分析平台里最容易被业务方直接使用的部分 ——
结论可以直接进通报和汇报材料。

**口径说明**：优良天依据**国标 AQI**（GB 3095-2012，AQI ≤ 100 为优良）判定，
与国内环境考核口径一致。欧洲 AQI（EAQI）另作国际对比之用，两者刻度差异极大
（EAQI 的"良"上界 40 大致对应 PM2.5 20 μg/m³，而国标 AQI 100 对应 75 μg/m³），
混用会让国内城市的优良率被严重低估。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from atmos.insights.loader import FrameLoader
from atmos.logging_setup import get_logger
from atmos.standards import CHINA_GOOD_CEILING, china_aqi_level
from atmos.models.enums import AQILevel

logger = get_logger("insights.compliance")

LEVEL_ORDER = [item.value for item in AQILevel]
# 默认考核目标：优良天数比例（%）
DEFAULT_GOOD_RATIO_TARGET = 80.0


def compliance_report(
    loader: FrameLoader,
    *,
    cities: list[str] | None = None,
    days: int = 90,
    good_ceiling: float = CHINA_GOOD_CEILING,
    target_ratio: float = DEFAULT_GOOD_RATIO_TARGET,
) -> dict[str, Any]:
    """城市空气质量考核报表（按城市 + 汇总），口径为国标 AQI。"""
    frame = loader.environment_daily(cities, days=days)
    if frame.empty:
        return {"available": False, "message": "尚无日汇总数据，请先执行采集", "cities": []}

    working = frame.assign(
        _aqi=pd.to_numeric(frame.get("china_aqi"), errors="coerce"),
        _aqi_euro=pd.to_numeric(frame.get("aqi_avg"), errors="coerce"),
        _exceed=pd.to_numeric(frame.get("exceed_hours"), errors="coerce"),
        _obs=pd.to_numeric(frame.get("obs_hours"), errors="coerce"),
    )
    working = working[working["_aqi"].notna()]
    if working.empty:
        return {"available": False, "message": "窗口内国标 AQI 数据全部缺失", "cities": []}

    working = working.assign(
        level=working["_aqi"].map(lambda value: china_aqi_level(float(value))),
        is_good=working["_aqi"].map(lambda value: bool(float(value) <= good_ceiling)),
    )

    city_rows: list[dict[str, Any]] = []
    for slug, group in working.groupby("city_id"):
        city_rows.append(
            _city_compliance(loader, str(slug), group, good_ceiling=good_ceiling, target_ratio=target_ratio)
        )
    city_rows.sort(key=lambda item: -(item["good_day_ratio"] or 0))

    totals = _aggregate(city_rows, working)
    return {
        "available": True,
        "aqi_standard": "国标 AQI（GB 3095-2012）",
        "window_days": days,
        "good_ceiling": good_ceiling,
        "good_ceiling_label": china_aqi_level(good_ceiling),
        "target_ratio": target_ratio,
        "cities": city_rows,
        "totals": totals,
        "period": {
            "start": working["date"].min().date().isoformat(),
            "end": working["date"].max().date().isoformat(),
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


def _city_compliance(
    loader: FrameLoader,
    slug: str,
    group: pd.DataFrame,
    *,
    good_ceiling: float,
    target_ratio: float,
) -> dict[str, Any]:
    """单城市考核指标（国标口径）。"""
    days = int(len(group))
    good_days = int(group["is_good"].fillna(False).sum())
    good_ratio = round(good_days / days * 100, 2) if days else None

    distribution = {level: 0 for level in LEVEL_ORDER}
    for level in group["level"].dropna():
        distribution[str(level)] = distribution.get(str(level), 0) + 1

    aqi = group["_aqi"].dropna()
    aqi_euro = group["_aqi_euro"].dropna()
    exceed_hours = float(group["_exceed"].fillna(0).sum())
    obs_hours = float(group["_obs"].fillna(0).sum())

    return {
        "city_slug": slug,
        "city_name": loader.city_label(slug),
        "days": days,
        "good_days": good_days,
        "exceed_days": days - good_days,
        "good_day_ratio": good_ratio,
        "target_ratio": target_ratio,
        "target_met": None if good_ratio is None else good_ratio >= target_ratio,
        "gap_to_target": None if good_ratio is None else round(good_ratio - target_ratio, 2),
        "aqi_avg": round(float(aqi.mean()), 2) if not aqi.empty else None,
        "aqi_peak": round(float(aqi.max()), 2) if not aqi.empty else None,
        "aqi_euro_avg": round(float(aqi_euro.mean()), 2) if not aqi_euro.empty else None,
        "exceed_hours": int(exceed_hours),
        "obs_hours": int(obs_hours),
        "exceed_hour_ratio": round(exceed_hours / obs_hours * 100, 2) if obs_hours else None,
        "level_distribution": distribution,
    }


def _aggregate(city_rows: list[dict[str, Any]], working: pd.DataFrame) -> dict[str, Any]:
    """全部城市汇总（按日均值统计，避免大城市天数多而主导）。"""
    total_days = int(len(working))
    good_days = int(working["is_good"].fillna(False).sum())
    distribution = {level: 0 for level in LEVEL_ORDER}
    for level in working["level"].dropna():
        distribution[str(level)] = distribution.get(str(level), 0) + 1
    aqi = working["_aqi"].dropna()

    met = [row for row in city_rows if row["target_met"]]
    return {
        "city_count": len(city_rows),
        "station_days": total_days,
        "good_days": good_days,
        "good_day_ratio": round(good_days / total_days * 100, 2) if total_days else None,
        "aqi_avg": round(float(aqi.mean()), 2) if not aqi.empty else None,
        "level_distribution": distribution,
        "cities_meeting_target": len(met),
        "best_city": city_rows[0]["city_name"] if city_rows else None,
        "worst_city": city_rows[-1]["city_name"] if city_rows else None,
    }


# ==================================================================
# 环比 / 同比
# ==================================================================
def period_comparison(
    loader: FrameLoader,
    *,
    cities: list[str] | None = None,
    days: int = 30,
    metric: str = "china_aqi",
) -> dict[str, Any]:
    """环比（与上一个等长周期比）与同比（与去年同期比）。

    同比依赖历史归档：天气类指标可回溯 ERA5，空气质量类指标受限于数据源
    只提供 92 天，因此数据不足时会明确说明而不是给出错误的对比值。
    """
    frame = loader.environment_daily(cities, days=days * 2 + 2)
    if frame.empty:
        return {"available": False, "message": "尚无日汇总数据，请先执行采集", "city": None}

    latest = frame["date"].max().date()
    current_start = latest - timedelta(days=days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)

    current = frame[(frame["date"].dt.date >= current_start)]
    previous = frame[
        (frame["date"].dt.date >= previous_start) & (frame["date"].dt.date <= previous_end)
    ]

    rows: list[dict[str, Any]] = []
    for slug in sorted(frame["city_id"].unique()):
        current_city = current[current["city_id"] == slug]
        previous_city = previous[previous["city_id"] == slug]
        if current_city.empty:
            continue
        rows.append(
            {
                "city_slug": str(slug),
                "city_name": loader.city_label(str(slug)),
                "current": _mean_metric(current_city, metric),
                "previous": _mean_metric(previous_city, metric) if not previous_city.empty else None,
                "current_days": int(len(current_city)),
                "previous_days": int(len(previous_city)),
            }
        )

    for row in rows:
        if row["current"] is None or row["previous"] is None:
            row["change"] = None
            row["change_ratio"] = None
            row["trend"] = "数据不足"
            continue
        delta = row["current"] - row["previous"]
        row["change"] = round(delta, 3)
        row["change_ratio"] = (
            round(delta / row["previous"] * 100, 2) if row["previous"] else None
        )
        row["trend"] = "改善" if delta < 0 else ("转差" if delta > 0 else "持平")

    rows.sort(key=lambda item: (item["change_ratio"] is None, item["change_ratio"] or 0))

    return {
        "available": True,
        "metric": metric,
        "metric_label": loader.metric_label(metric.replace("_avg", "")),
        "unit": loader.metric_meta(metric.replace("_avg", "")).get("unit"),
        "days": days,
        "current_period": {"start": current_start.isoformat(), "end": latest.isoformat()},
        "previous_period": {"start": previous_start.isoformat(), "end": previous_end.isoformat()},
        "cities": rows,
        "year_over_year": year_over_year(loader, cities=cities, days=days, metric=metric),
        "generated_at": datetime.utcnow().isoformat(),
    }


def year_over_year(
    loader: FrameLoader,
    *,
    cities: list[str] | None = None,
    days: int = 30,
    metric: str = "aqi_avg",
) -> dict[str, Any]:
    """同比：需要覆盖去年同期（约 400 天）的历史归档才可用。"""
    base_metric = metric.replace("_avg", "")
    # 归档资产只含气象要素，空气质量指标无法做同比
    archive_metric = base_metric if base_metric in {"temperature_2m", "precipitation", "wind_speed_10m"} else "temperature_2m"
    frame = loader.archive_hourly(cities, days=400, columns=["city_id", "time", archive_metric])
    if frame.empty:
        return {
            "available": False,
            "message": "历史归档数据不足，无法做同比。请把 ATMOS_ARCHIVE_LOOKBACK_DAYS 调至 400 后重新采集。",
        }

    frame = frame.assign(date=frame["time"].dt.date)
    daily = frame.groupby(["city_id", "date"], as_index=False)[archive_metric].mean()
    daily["year"] = pd.to_datetime(daily["date"]).dt.year

    latest = max(daily["date"])
    current_start = latest - timedelta(days=days - 1)
    previous_start = current_start.replace(year=current_start.year - 1)
    previous_end = latest.replace(year=latest.year - 1)

    current = daily[daily["date"] >= current_start]
    previous = daily[(daily["date"] >= previous_start) & (daily["date"] <= previous_end)]
    if previous.empty:
        return {
            "available": False,
            "message": f"缺少 {previous_start.year} 年同期归档数据，无法做同比。"
            "请把 ATMOS_ARCHIVE_LOOKBACK_DAYS 调至 400 后重新采集。",
        }

    rows: list[dict[str, Any]] = []
    for slug in sorted(daily["city_id"].unique()):
        current_values = current.loc[current["city_id"] == slug, archive_metric]
        previous_values = previous.loc[previous["city_id"] == slug, archive_metric]
        if current_values.empty or previous_values.empty:
            continue
        current_mean = float(current_values.mean())
        previous_mean = float(previous_values.mean())
        rows.append(
            {
                "city_slug": str(slug),
                "city_name": loader.city_label(str(slug)),
                "metric": archive_metric,
                "metric_label": loader.metric_label(archive_metric),
                "unit": loader.metric_meta(archive_metric).get("unit"),
                "current": round(current_mean, 3),
                "previous": round(previous_mean, 3),
                "change": round(current_mean - previous_mean, 3),
                "change_ratio": round((current_mean - previous_mean) / previous_mean * 100, 2)
                if previous_mean
                else None,
                "current_days": int(current_values.size),
                "previous_days": int(previous_values.size),
            }
        )

    return {
        "available": bool(rows),
        "metric": archive_metric,
        "metric_label": loader.metric_label(archive_metric),
        "unit": loader.metric_meta(archive_metric).get("unit"),
        "current_year": current_start.year,
        "previous_year": previous_start.year,
        "cities": rows,
        "note": "同比基于 ERA5 历史归档，且归档不含空气质量要素，因此使用气温等气象指标替代。",
    }


def _mean_metric(frame: pd.DataFrame, column: str) -> float | None:
    if column not in frame.columns:
        return None
    values = pd.to_numeric(frame[column], errors="coerce").dropna()
    if values.empty:
        return None
    return round(float(values.mean()), 3)


# ==================================================================
# 报表导出
# ==================================================================
def report_rows(report: dict[str, Any]) -> tuple[list[str], list[list[Any]]]:
    """把考核报表摊平成 CSV 的行列（供导出接口复用）。"""
    header = [
        "城市",
        "slug",
        "统计天数",
        "优良天",
        "超标天",
        "优良天比例(%)",
        "目标(%)",
        "是否达标",
        "差距(百分点)",
        "日均AQI",
        "AQI峰值",
        "超标小时数",
        "有效观测小时数",
        "超标小时占比(%)",
        *[f"{level}天数" for level in LEVEL_ORDER],
    ]
    rows: list[list[Any]] = []
    for item in report.get("cities", []):
        distribution = item.get("level_distribution") or {}
        rows.append(
            [
                item.get("city_name"),
                item.get("city_slug"),
                item.get("days"),
                item.get("good_days"),
                item.get("exceed_days"),
                item.get("good_day_ratio"),
                item.get("target_ratio"),
                "是" if item.get("target_met") else "否",
                item.get("gap_to_target"),
                item.get("aqi_avg"),
                item.get("aqi_peak"),
                item.get("exceed_hours"),
                item.get("obs_hours"),
                item.get("exceed_hour_ratio"),
                *[distribution.get(level, 0) for level in LEVEL_ORDER],
            ]
        )
    return header, rows


__all__ = [
    "compliance_report",
    "period_comparison",
    "year_over_year",
    "report_rows",
    "DEFAULT_GOOD_RATIO_TARGET",
    "LEVEL_ORDER",
]
