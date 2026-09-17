"""城市聚类与相似度分析。

用途：**横向管理**。15 个城市逐个盯不现实，先按环境特征分组，
就能对"同一类城市"用同一套治理策略，而不是一刀切。

方法：把每个城市压缩成一个特征向量（各指标的窗口均值），
标准化后做 KMeans 聚类，并输出相似度矩阵用于找"参考城市"。

聚类数 K 不写死：默认在 2–6 之间自动选轮廓系数最高的那个。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from atmos.insights import numeric
from atmos.insights.loader import FrameLoader
from atmos.logging_setup import get_logger

logger = get_logger("insights.clustering")

# 聚类使用的默认特征（覆盖气象 + 污染 + 扩散条件）
DEFAULT_FEATURES = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
    "pressure_msl",
    "precipitation",
    "pm2_5",
    "pm10",
    "ozone",
    "european_aqi",
)

# 判定"该特征在簇内偏高/偏低"的标准化阈值
DISTINGUISH_THRESHOLD = 0.55


def cluster_cities(
    loader: FrameLoader,
    *,
    cities: list[str] | None = None,
    days: int = 30,
    features: list[str] | None = None,
    k: int | None = None,
) -> dict[str, Any]:
    """按环境特征对城市聚类。"""
    names = [item for item in (features or list(DEFAULT_FEATURES)) if item]
    frame = loader.environment_hourly(cities, days=days, columns=["city_id", *names])
    if frame.empty:
        return {"available": False, "message": "尚无逐小时数据，请先执行采集", "cities": [], "clusters": []}

    available = [name for name in names if name in frame.columns]
    if len(available) < 2:
        return {"available": False, "message": "可用特征不足两个，无法聚类", "cities": [], "clusters": []}

    # 每个城市 → 一条特征向量（各指标均值）
    aggregated = frame.groupby("city_id")[available].mean(numeric_only=True)
    aggregated = aggregated.dropna(how="all")
    if len(aggregated) < 3:
        return {
            "available": False,
            "message": "有效城市不足 3 个，无法聚类",
            "cities": [],
            "clusters": [],
        }

    # 缺失特征用列均值补齐，避免整行被丢弃
    aggregated = aggregated.fillna(aggregated.mean(numeric_only=True)).fillna(0.0)
    slugs = [str(item) for item in aggregated.index]
    matrix = aggregated.to_numpy(dtype=float)

    # 标准化是必需的：气压 ~1000、风速 ~10、降水 ~0，
    # 不做标准化的话距离会被量纲最大的气压完全主导。
    scaled = numeric.standardize(matrix)

    candidates = numeric.choose_k(scaled, k_min=2, k_max=min(6, max(2, len(slugs) - 1)))
    chosen_k = k or _best_k(candidates) or 2
    result = numeric.kmeans(scaled, chosen_k)
    if result is None:
        return {"available": False, "message": "聚类失败（样本或 K 值不合法）", "cities": [], "clusters": []}

    labels = np.array(result.labels)
    feature_labels = [loader.metric_label(name) for name in available]
    units = [loader.metric_meta(name).get("unit") for name in available]

    # 相似度矩阵只算一次（放在成员循环里会退化成 O(n³)）
    similarity = numeric.similarity_matrix(scaled)

    clusters: list[dict[str, Any]] = []
    for index in range(chosen_k):
        member_positions = np.where(labels == index)[0]
        members = [slugs[position] for position in member_positions]
        if not members:
            continue
        centroid_scaled = result.centroids[index]
        centroid_raw = matrix[labels == index].mean(axis=0)
        # 成员与簇内其他成员的平均相似度：越高说明该簇越紧凑
        member_payload = []
        for position in member_positions:
            others = [item for item in member_positions if item != position]
            affinity = (
                float(np.mean([similarity[position][other] for other in others])) if others else 1.0
            )
            member_payload.append(
                {
                    "city_slug": slugs[position],
                    "city_name": loader.city_label(slugs[position]),
                    "similarity_to_cluster": round(affinity, 4),
                }
            )
        member_payload.sort(key=lambda item: -item["similarity_to_cluster"])
        clusters.append(
            {
                "cluster_id": index,
                "label": _describe_cluster(centroid_scaled, feature_labels),
                "size": len(members),
                "members": member_payload,
                "centroid": {
                    name: round(float(value), 3) for name, value in zip(available, centroid_raw, strict=False)
                },
                "distinguishing": _distinguishing(centroid_scaled, available, feature_labels),
            }
        )
    clusters.sort(key=lambda item: -item["size"])

    pairs: list[dict[str, Any]] = []
    for row in range(len(slugs)):
        for col in range(row + 1, len(slugs)):
            pairs.append(
                {
                    "left": slugs[row],
                    "left_name": loader.city_label(slugs[row]),
                    "right": slugs[col],
                    "right_name": loader.city_label(slugs[col]),
                    "similarity": round(float(similarity[row][col]), 4),
                    "same_cluster": bool(labels[row] == labels[col]),
                }
            )
    pairs.sort(key=lambda item: -item["similarity"])

    return {
        "available": True,
        "window_days": days,
        "features": available,
        "feature_labels": feature_labels,
        "units": units,
        "city_count": len(slugs),
        "k": chosen_k,
        "silhouette": None if result.silhouette is None else round(result.silhouette, 4),
        "inertia": round(result.inertia, 4),
        "candidate_k": candidates,
        "clusters": clusters,
        "similarity": {
            "labels": [loader.city_label(slug) for slug in slugs],
            "slugs": slugs,
            "values": [[round(float(value), 4) for value in row] for row in similarity],
        },
        "most_similar_pairs": pairs[:12],
        "feature_means": {
            name: {slug: round(float(aggregated.loc[slug, name]), 3) for slug in slugs}
            for name in available
        },
        "caveat": "聚类基于所选窗口的均值特征，窗口变化或数据缺失可能改变分组结果。",
        "generated_at": datetime.utcnow().isoformat(),
    }


def _best_k(candidates: list[dict[str, Any]]) -> int | None:
    """按轮廓系数选最优 K；全部缺失时退化为 inertia 拐点法的简单近似。"""
    scored = [item for item in candidates if item.get("silhouette") is not None]
    if scored:
        return int(max(scored, key=lambda item: item["silhouette"])["k"])
    if candidates:
        return int(candidates[0]["k"])
    return None


def _distinguishing(
    centroid_scaled: np.ndarray, features: list[str], labels: list[str]
) -> list[dict[str, Any]]:
    """簇内相对整体显著偏高/偏低的特征。"""
    items = [
        {
            "feature": feature,
            "label": label,
            "z": round(float(z_value), 3),
            "direction": "偏高" if z_value > 0 else "偏低",
        }
        for feature, label, z_value in zip(features, labels, centroid_scaled, strict=False)
        if abs(z_value) >= DISTINGUISH_THRESHOLD
    ]
    items.sort(key=lambda item: -abs(item["z"]))
    return items[:4]


def _describe_cluster(centroid_scaled: np.ndarray, labels: list[str]) -> str:
    """给簇起一个可读的名字，例如"气温偏低、湿度偏高型"。"""
    items = [
        (label, float(z_value))
        for label, z_value in zip(labels, centroid_scaled, strict=False)
        if abs(z_value) >= DISTINGUISH_THRESHOLD
    ]
    if not items:
        return "特征均衡型"
    items.sort(key=lambda item: -abs(item[1]))
    parts = [f"{label}{'偏高' if value > 0 else '偏低'}" for label, value in items[:2]]
    return "、".join(parts) + "型"


__all__ = ["cluster_cities", "DEFAULT_FEATURES", "DISTINGUISH_THRESHOLD"]
