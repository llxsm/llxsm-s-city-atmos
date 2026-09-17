"""气象—空气质量关系分析。

回答"**为什么这次污染重**"这一类归因问题。做法是把气象要素与污染物浓度配对，
给出三样东西：

1. **散点数据** —— 直接看形态（线性？阈值？U 形？）；
2. **相关系数** —— Pearson（线性）+ Spearman（单调），两者差异能提示非线性；
3. **分箱均值曲线** —— 把连续变量分箱后取均值，比散点更能看清趋势方向。

需要注意：这是**相关性**分析，不是因果推断。真正的原因可能来自未观测的混杂因素
（如区域传输、排放源变化），页面上也会明确标注这一点。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from atmos.insights import numeric
from atmos.insights.loader import FrameLoader
from atmos.logging_setup import get_logger

logger = get_logger("insights.relationship")

# 预设的"气象要素 → 污染物"配对及其业务含义
DEFAULT_PAIRS: tuple[dict[str, Any], ...] = (
    {
        "x": "wind_speed_10m",
        "y": "pm2_5",
        "title": "风速 → PM2.5",
        "rationale": "风速越大，水平和垂直扩散越强，颗粒物越难累积。通常呈负相关。",
    },
    {
        "x": "relative_humidity_2m",
        "y": "pm2_5",
        "title": "相对湿度 → PM2.5",
        "rationale": "高湿促进二次气溶胶生成与吸湿增长，常呈正相关；但在降水时会被冲刷。",
    },
    {
        "x": "temperature_2m",
        "y": "ozone",
        "title": "气温 → 臭氧",
        "rationale": "高温强辐射下光化学反应加剧，臭氧生成量上升，通常呈正相关。",
    },
    {
        "x": "precipitation",
        "y": "pm2_5",
        "title": "降水量 → PM2.5",
        "rationale": "降水对颗粒物有湿沉降清除作用，但在小雨时可能先吸湿增长。",
    },
    {
        "x": "pressure_msl",
        "y": "pm2_5",
        "title": "海平面气压 → PM2.5",
        "rationale": "高压控制下易形成静稳天气，污染物不易扩散。",
    },
    {
        "x": "ventilation_index",
        "y": "pm2_5",
        "title": "通风指数 → PM2.5",
        "rationale": "通风指数综合了风速与边界层厚度，是扩散能力的近似度量。",
    },
)

SCATTER_SAMPLE_LIMIT = 1500
DEFAULT_BINS = 8


def list_pairs() -> list[dict[str, Any]]:
    """可分析的配对清单。"""
    return [dict(item) for item in DEFAULT_PAIRS]


def analyse_pair(
    loader: FrameLoader,
    *,
    x: str = "wind_speed_10m",
    y: str = "pm2_5",
    cities: list[str] | None = None,
    days: int = 30,
    bins: int = DEFAULT_BINS,
    by_city: bool = False,
) -> dict[str, Any]:
    """对一对指标做完整的关系分析。

    ``by_city=False`` 时把所有城市的数据合并分析（回答总体规律）；
    ``by_city=True`` 时逐城市给出相关系数（回答"这个规律在哪些城市成立"）。
    """
    frame = loader.environment_hourly(
        cities, days=days, columns=["city_id", "time", x, y], require=[x, y]
    )
    if frame.empty or x not in frame.columns or y not in frame.columns:
        return {
            "available": False,
            "message": "所选指标在窗口内无有效配对数据，请先执行采集或调整时段",
            "x": x,
            "y": y,
        }

    working = frame.assign(
        _x=pd.to_numeric(frame[x], errors="coerce"),
        _y=pd.to_numeric(frame[y], errors="coerce"),
    ).dropna(subset=["_x", "_y"])
    if working.empty:
        return {"available": False, "message": "配对数据为空", "x": x, "y": y}

    xs = working["_x"].to_numpy(dtype=float)
    ys = working["_y"].to_numpy(dtype=float)

    pearson = numeric.pearson(xs, ys)
    spearman = numeric.spearman(xs, ys)
    regression = numeric.linear_regression(xs, ys)

    payload: dict[str, Any] = {
        "available": True,
        "x": x,
        "y": y,
        "x_label": loader.metric_label(x),
        "y_label": loader.metric_label(y),
        "x_unit": loader.metric_meta(x).get("unit"),
        "y_unit": loader.metric_meta(y).get("unit"),
        "window_days": days,
        "samples": int(len(working)),
        "city_count": int(working["city_id"].nunique()),
        "pearson": None if pearson is None else round(pearson, 4),
        "spearman": None if spearman is None else round(spearman, 4),
        "pearson_label": numeric.correlation_label(pearson),
        "spearman_label": numeric.correlation_label(spearman),
        "regression": regression,
        "bins": _binned_curve(xs, ys, bins=bins),
        "scatter": _scatter(working),
        "interpretation": _interpret(pearson, spearman, x, y, loader),
        "caveat": "相关性不等于因果：区域传输、排放源变化等未观测因素同样会影响浓度。",
        "generated_at": datetime.utcnow().isoformat(),
    }

    if by_city:
        payload["by_city"] = _per_city(loader, working)
    return payload


def _scatter(working: pd.DataFrame) -> list[dict[str, Any]]:
    """抽样后的散点数据（避免把几万个点推给浏览器）。"""
    if len(working) > SCATTER_SAMPLE_LIMIT:
        step = max(1, len(working) // SCATTER_SAMPLE_LIMIT)
        sampled = working.iloc[::step]
    else:
        sampled = working
    return [
        {
            "x": round(float(row["_x"]), 3),
            "y": round(float(row["_y"]), 3),
            "city": str(row["city_id"]),
        }
        for _, row in sampled.iterrows()
    ]


def _binned_curve(xs: np.ndarray, ys: np.ndarray, *, bins: int) -> list[dict[str, Any]]:
    """按 x 分箱后取 y 的均值与中位数，刻画趋势方向。"""
    if xs.size < bins * 2:
        return []
    edges = np.linspace(float(np.min(xs)), float(np.max(xs)), bins + 1)
    index = np.clip(np.digitize(xs, edges) - 1, 0, bins - 1)
    curve: list[dict[str, Any]] = []
    for bucket in range(bins):
        mask = index == bucket
        if not mask.any():
            continue
        curve.append(
            {
                "lower": round(float(edges[bucket]), 3),
                "upper": round(float(edges[bucket + 1]), 3),
                "center": round(float(edges[bucket] + edges[bucket + 1]) / 2, 3),
                "mean": round(float(np.mean(ys[mask])), 3),
                "median": round(float(np.median(ys[mask])), 3),
                "count": int(mask.sum()),
            }
        )
    return curve


def _per_city(loader: FrameLoader, working: pd.DataFrame) -> list[dict[str, Any]]:
    """逐城市相关系数：检验规律是否普遍成立。"""
    rows: list[dict[str, Any]] = []
    for slug, group in working.groupby("city_id"):
        if len(group) < numeric.MIN_SAMPLES:
            continue
        coefficient = numeric.pearson(group["_x"].to_numpy(dtype=float), group["_y"].to_numpy(dtype=float))
        rank_coefficient = numeric.spearman(
            group["_x"].to_numpy(dtype=float), group["_y"].to_numpy(dtype=float)
        )
        rows.append(
            {
                "city_slug": str(slug),
                "city_name": loader.city_label(str(slug)),
                "samples": int(len(group)),
                "pearson": None if coefficient is None else round(coefficient, 4),
                "spearman": None if rank_coefficient is None else round(rank_coefficient, 4),
                "strength": numeric.correlation_label(coefficient),
            }
        )
    rows.sort(key=lambda item: (item["pearson"] is None, item["pearson"] or 0))
    return rows


def _interpret(
    pearson: float | None,
    spearman: float | None,
    x: str,
    y: str,
    loader: FrameLoader,
) -> str:
    """把统计结果翻译成一句可读的结论。"""
    if pearson is None:
        return "样本不足或数据无变化，无法给出相关性结论。"
    x_label = loader.metric_label(x)
    y_label = loader.metric_label(y)
    direction = "上升" if pearson > 0 else "下降"
    text = f"{x_label}增大时，{y_label}整体呈{direction}趋势（{numeric.correlation_label(pearson)}）。"
    if spearman is not None and abs(spearman) - abs(pearson) > 0.15:
        text += "秩相关明显强于线性相关，说明关系是单调但非线性的，存在边际效应递减或阈值效应。"
    if abs(pearson) < 0.2:
        text += "相关性很弱，说明在该时段内两者基本独立，影响浓度的主因可能在别处。"
    return text


__all__ = ["analyse_pair", "list_pairs", "DEFAULT_PAIRS"]
