"""分析用的数值工具集。

为什么自己实现而不用 scikit-learn / scipy
----------------------------------------
本平台所需的统计量（分位数、相关系数、一元回归）与 KMeans 都是**教科书级算法**，
用 numpy 实现合计不到 300 行；而引入 scipy + scikit-learn 会让依赖体积增加约 60 MB、
拖慢冷启动，与"零配置一键启动"的项目目标冲突。自己实现还有一个好处：
算法参数与边界处理（空值、常量序列、样本不足）完全可控，便于测试与解释。

所有函数都只接受**已清洗的一维/二维 numpy 数组**，不负责空值过滤 ——
调用方（各分析器）统一在取数后用 ``pandas.dropna()`` 清洗，避免"清洗逻辑散落各处"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

# 样本不足时的下限：少于该数量则不输出统计结论
MIN_SAMPLES = 3


# ==================================================================
# 描述统计
# ==================================================================
def describe(values: np.ndarray) -> dict[str, Any]:
    """完整描述统计（含分位数与离散程度）。"""
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return {"count": 0}

    quartiles = np.percentile(array, [5, 25, 50, 75, 95])
    mean = float(np.mean(array))
    std = float(np.std(array, ddof=1)) if array.size > 1 else 0.0
    return {
        "count": int(array.size),
        "mean": round(mean, 4),
        "std": round(std, 4),
        "min": round(float(np.min(array)), 4),
        "max": round(float(np.max(array)), 4),
        "range": round(float(np.max(array) - np.min(array)), 4),
        "p5": round(float(quartiles[0]), 4),
        "p25": round(float(quartiles[1]), 4),
        "p50": round(float(quartiles[2]), 4),
        "p75": round(float(quartiles[3]), 4),
        "p95": round(float(quartiles[4]), 4),
        "iqr": round(float(quartiles[3] - quartiles[1]), 4),
        # 变异系数：标准差 / 均值，用于比较量纲不同的指标波动性
        "cv": round(std / mean, 4) if mean not in (0.0,) else None,
    }


@dataclass
class BoxplotStats:
    """箱线图五数概括与离群点。"""

    q1: float
    median: float
    q3: float
    iqr: float
    lower_fence: float
    upper_fence: float
    whisker_low: float
    whisker_high: float
    outliers: list[float] = field(default_factory=list)

    @property
    def outlier_count(self) -> int:
        """离群点数量。"""
        return len(self.outliers)

    def to_payload(self) -> dict[str, Any]:
        return {
            "q1": round(self.q1, 4),
            "median": round(self.median, 4),
            "q3": round(self.q3, 4),
            "iqr": round(self.iqr, 4),
            "lower_fence": round(self.lower_fence, 4),
            "upper_fence": round(self.upper_fence, 4),
            "whisker_low": round(self.whisker_low, 4),
            "whisker_high": round(self.whisker_high, 4),
            "outlier_count": self.outlier_count,
            "outliers": [round(item, 3) for item in self.outliers[:50]],
        }


def boxplot(values: np.ndarray, *, fence: float = 1.5) -> BoxplotStats | None:
    """Tukey 箱线图统计（默认 1.5 倍 IQR 围栏）。"""
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return None
    q1, median, q3 = (float(item) for item in np.percentile(array, [25, 50, 75]))
    iqr = q3 - q1
    lower = q1 - fence * iqr
    upper = q3 + fence * iqr
    inside = array[(array >= lower) & (array <= upper)]
    outliers = array[(array < lower) | (array > upper)]
    return BoxplotStats(
        q1=q1,
        median=median,
        q3=q3,
        iqr=iqr,
        lower_fence=lower,
        upper_fence=upper,
        whisker_low=float(np.min(inside)) if inside.size else q1,
        whisker_high=float(np.max(inside)) if inside.size else q3,
        outliers=[float(item) for item in outliers],
    )


def histogram(values: np.ndarray, *, bins: int = 20, value_range: tuple[float, float] | None = None) -> dict[str, Any]:
    """等宽直方图分箱。"""
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return {"bins": [], "counts": [], "edges": []}
    counts, edges = np.histogram(array, bins=bins, range=value_range)
    return {
        "edges": [round(float(item), 4) for item in edges],
        "counts": [int(item) for item in counts],
        "bins": [
            {
                "lower": round(float(edges[index]), 4),
                "upper": round(float(edges[index + 1]), 4),
                "count": int(counts[index]),
                "ratio": round(float(counts[index]) / array.size * 100, 3),
            }
            for index in range(len(counts))
        ],
    }


# ==================================================================
# 相关与回归
# ==================================================================
def pearson(x: np.ndarray, y: np.ndarray) -> float | None:
    """Pearson 线性相关系数；常量序列或样本不足时返回 None。"""
    left = np.asarray(x, dtype=float)
    right = np.asarray(y, dtype=float)
    if left.size < MIN_SAMPLES or left.size != right.size:
        return None
    if np.std(left) == 0 or np.std(right) == 0:
        return None
    return float(np.corrcoef(left, right)[0, 1])


def _rank(values: np.ndarray) -> np.ndarray:
    """平均秩（处理并列值），用于 Spearman。"""
    order = np.argsort(values, kind="stable")
    ranks = np.empty(len(values), dtype=float)
    ranks[order] = np.arange(1, len(values) + 1, dtype=float)
    # 并列值取平均秩
    sorted_values = values[order]
    index = 0
    while index < len(sorted_values):
        end = index
        while end + 1 < len(sorted_values) and sorted_values[end + 1] == sorted_values[index]:
            end += 1
        if end > index:
            ranks[order[index : end + 1]] = np.mean(ranks[order[index : end + 1]])
        index = end + 1
    return ranks


def spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    """Spearman 秩相关系数（对单调非线性关系敏感）。"""
    left = np.asarray(x, dtype=float)
    right = np.asarray(y, dtype=float)
    if left.size < MIN_SAMPLES or left.size != right.size:
        return None
    return pearson(_rank(left), _rank(right))


def linear_regression(x: np.ndarray, y: np.ndarray) -> dict[str, Any] | None:
    """一元最小二乘回归，返回斜率、截距、R² 与样本量。"""
    left = np.asarray(x, dtype=float)
    right = np.asarray(y, dtype=float)
    if left.size < MIN_SAMPLES or left.size != right.size:
        return None
    if np.std(left) == 0:
        return None
    slope, intercept = np.polyfit(left, right, 1)
    predicted = slope * left + intercept
    residual = float(np.sum((right - predicted) ** 2))
    total = float(np.sum((right - np.mean(right)) ** 2))
    r2 = 1.0 - residual / total if total > 0 else 0.0
    return {
        "slope": round(float(slope), 5),
        "intercept": round(float(intercept), 4),
        "r2": round(max(0.0, min(1.0, r2)), 4),
        "n": int(left.size),
    }


def correlation_label(value: float | None) -> str:
    """把相关系数翻译成中文强度描述（便于页面直接展示）。"""
    if value is None:
        return "样本不足"
    magnitude = abs(value)
    if magnitude >= 0.8:
        strength = "极强"
    elif magnitude >= 0.6:
        strength = "强"
    elif magnitude >= 0.4:
        strength = "中等"
    elif magnitude >= 0.2:
        strength = "弱"
    else:
        strength = "几乎无"
    if magnitude < 0.2:
        return strength
    return f"{strength}{'正' if value > 0 else '负'}相关"


def correlation_matrix(
    columns: dict[str, np.ndarray], *, method: str = "pearson"
) -> dict[str, Any]:
    """多指标相关矩阵（要求各列等长且已对齐）。"""
    names = list(columns)
    if len(names) < 2:
        return {"labels": names, "values": [], "method": method}

    func = spearman if method == "spearman" else pearson
    size = len(names)
    values: list[list[float | None]] = [[None] * size for _ in range(size)]
    for row in range(size):
        values[row][row] = 1.0
        for col in range(row + 1, size):
            coefficient = func(columns[names[row]], columns[names[col]])
            rounded = None if coefficient is None else round(coefficient, 4)
            values[row][col] = rounded
            values[col][row] = rounded
    return {"labels": names, "values": values, "method": method}


# ==================================================================
# 异常检测
# ==================================================================
def robust_zscore(values: np.ndarray) -> np.ndarray:
    """基于中位数与 MAD 的稳健 Z 分数（对离群点本身不敏感）。"""
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return array
    median = np.median(array)
    mad = np.median(np.abs(array - median))
    if mad == 0:
        return np.zeros_like(array)
    # 0.6745 使 MAD 在正态分布下与标准差一致
    return 0.6745 * (array - median) / mad


def zscore(values: np.ndarray) -> np.ndarray:
    """标准 Z 分数。"""
    array = np.asarray(values, dtype=float)
    std = np.std(array, ddof=1) if array.size > 1 else 0.0
    if std == 0:
        return np.zeros_like(array)
    return (array - np.mean(array)) / std


def outliers_by_zscore(values: np.ndarray, *, threshold: float = 3.0) -> np.ndarray:
    """按 |Z| > 阈值标记异常（返回布尔掩码）。"""
    return np.abs(zscore(values)) > threshold


def outliers_by_iqr(values: np.ndarray, *, fence: float = 1.5) -> np.ndarray:
    """按 Tukey 围栏标记异常（返回布尔掩码）。"""
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return np.zeros(0, dtype=bool)
    q1, q3 = np.percentile(array, [25, 75])
    iqr = q3 - q1
    return (array < q1 - fence * iqr) | (array > q3 + fence * iqr)


# ==================================================================
# 聚类
# ==================================================================
def standardize(matrix: np.ndarray) -> np.ndarray:
    """按列做 Z-score 标准化（聚类前必须做，否则量纲大的指标会主导距离）。"""
    array = np.asarray(matrix, dtype=float)
    if array.ndim != 2 or array.shape[0] == 0:
        return array
    mean = array.mean(axis=0)
    std = array.std(axis=0, ddof=0)
    std[std == 0] = 1.0
    return (array - mean) / std


def pairwise_distance(matrix: np.ndarray, *, metric: str = "euclidean") -> np.ndarray:
    """样本间距离矩阵。"""
    array = np.asarray(matrix, dtype=float)
    if metric == "manhattan":
        return np.abs(array[:, None, :] - array[None, :, :]).sum(axis=2)
    return np.sqrt(((array[:, None, :] - array[None, :, :]) ** 2).sum(axis=2))


def similarity_matrix(matrix: np.ndarray, *, metric: str = "euclidean") -> np.ndarray:
    """把距离矩阵转成 0-1 相似度（1 - 归一化距离），便于直接展示。"""
    distance = pairwise_distance(matrix, metric=metric)
    maximum = float(np.max(distance))
    if maximum == 0:
        return np.ones_like(distance)
    return 1.0 - distance / maximum


@dataclass
class ClusterResult:
    """KMeans 聚类结果。"""

    labels: list[int]
    centroids: list[list[float]]
    inertia: float
    iterations: int
    k: int
    silhouette: float | None = None
    sizes: dict[int, int] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "iterations": self.iterations,
            "inertia": round(self.inertia, 4),
            "silhouette": None if self.silhouette is None else round(self.silhouette, 4),
            "sizes": {str(key): value for key, value in sorted(self.sizes.items())},
            "centroids": [[round(value, 4) for value in row] for row in self.centroids],
            "labels": self.labels,
        }


def kmeans(
    matrix: np.ndarray,
    k: int,
    *,
    max_iterations: int = 300,
    tolerance: float = 1e-6,
    restarts: int = 10,
    seed: int = 20260917,
) -> ClusterResult | None:
    """KMeans++ 初始化 + Lloyd 迭代。

    多次随机重启取 inertia 最小者，规避局部最优 —— 这是 KMeans 结果不可复现
    与质量不稳的主要来源。使用固定随机种子，保证同一批数据结果可重复。
    """
    array = np.asarray(matrix, dtype=float)
    if array.ndim != 2 or array.shape[0] < k or k < 2:
        return None

    rng = np.random.default_rng(seed)
    best: tuple[float, np.ndarray, np.ndarray, int] | None = None

    for _ in range(max(1, restarts)):
        centroids = _kmeans_plus_plus(array, k, rng)
        labels = np.zeros(array.shape[0], dtype=int)
        iterations = 0
        for iterations in range(1, max_iterations + 1):
            distances = np.sqrt(((array[:, None, :] - centroids[None, :, :]) ** 2).sum(axis=2))
            new_labels = np.argmin(distances, axis=1)
            if np.array_equal(new_labels, labels) and iterations > 1:
                labels = new_labels
                break
            labels = new_labels
            for index in range(k):
                members = array[labels == index]
                if members.size:
                    centroids[index] = members.mean(axis=0)
                else:
                    # 空簇：重新播撒到离质心最远的样本，避免簇数塌缩
                    centroids[index] = array[np.argmax(np.min(distances, axis=1))]
            if float(np.max(np.abs(centroids))) < tolerance:
                break

        inertia = float(
            np.sum((array - centroids[labels]) ** 2)
        )
        if best is None or inertia < best[0]:
            best = (inertia, labels.copy(), centroids.copy(), iterations)

    if best is None:  # pragma: no cover - 上面的循环至少执行一次
        return None

    inertia, labels, centroids, iterations = best
    sizes = {index: int(np.sum(labels == index)) for index in range(k)}
    return ClusterResult(
        labels=[int(item) for item in labels],
        centroids=[[float(value) for value in row] for row in centroids],
        inertia=inertia,
        iterations=iterations,
        k=k,
        silhouette=silhouette_score(array, labels),
        sizes=sizes,
    )


def _kmeans_plus_plus(array: np.ndarray, k: int, rng: np.random.Generator) -> np.ndarray:
    """KMeans++ 初始化：让初始质心尽量彼此远离。"""
    count = array.shape[0]
    centroids = [array[rng.integers(count)]]
    for _ in range(1, k):
        squared = np.min(
            [((array - centroid) ** 2).sum(axis=1) for centroid in centroids], axis=0
        )
        total = float(squared.sum())
        if total <= 0:
            centroids.append(array[rng.integers(count)])
            continue
        probabilities = squared / total
        centroids.append(array[rng.choice(count, p=probabilities)])
    return np.array(centroids, dtype=float)


def silhouette_score(matrix: np.ndarray, labels: np.ndarray) -> float | None:
    """轮廓系数（-1 ~ 1，越高说明簇内越紧凑、簇间越分离）。"""
    array = np.asarray(matrix, dtype=float)
    unique = np.unique(labels)
    if len(unique) < 2 or array.shape[0] <= len(unique):
        return None
    distance = pairwise_distance(array)
    scores: list[float] = []
    for index in range(array.shape[0]):
        own = labels[index]
        same = distance[index][labels == own]
        if same.size <= 1:
            continue
        a = float(np.sum(same) / (same.size - 1))
        b = min(
            float(np.mean(distance[index][labels == other])) for other in unique if other != own
        )
        denominator = max(a, b)
        if denominator > 0:
            scores.append((b - a) / denominator)
    return float(np.mean(scores)) if scores else None


def choose_k(matrix: np.ndarray, *, k_min: int = 2, k_max: int = 6) -> list[dict[str, Any]]:
    """在给定范围内尝试多个 K，返回 inertia 与轮廓系数供选 K。"""
    results: list[dict[str, Any]] = []
    upper = min(k_max, max(k_min, matrix.shape[0] - 1))
    for k in range(k_min, upper + 1):
        result = kmeans(matrix, k)
        if result is None:
            continue
        results.append(
            {
                "k": k,
                "inertia": round(result.inertia, 4),
                "silhouette": None if result.silhouette is None else round(result.silhouette, 4),
                "sizes": {str(key): value for key, value in sorted(result.sizes.items())},
            }
        )
    return results


__all__ = [
    "MIN_SAMPLES",
    "describe",
    "BoxplotStats",
    "boxplot",
    "histogram",
    "pearson",
    "spearman",
    "linear_regression",
    "correlation_label",
    "correlation_matrix",
    "robust_zscore",
    "zscore",
    "outliers_by_zscore",
    "outliers_by_iqr",
    "standardize",
    "pairwise_distance",
    "similarity_matrix",
    "ClusterResult",
    "kmeans",
    "silhouette_score",
    "choose_k",
]
