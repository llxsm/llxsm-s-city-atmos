"""深度分析引擎。

在"取数 + 展示"之上回答业务问题，六大类能力：

============  ==========================================================
描述性分析    统计与分布、指标相关性
时段规律      日内小时画像、周内工作日/周末画像、月度趋势
异常与过程    异常点检测、污染过程识别（起止/时长/峰值/成因）
归因分析      气象要素与污染物的散点、相关系数、分箱趋势、一元回归
考核与报表    优良天、超标统计、环比同比、目标完成度、CSV 导出
横向对比      城市聚类与相似度矩阵
============  ==========================================================

设计要点：
* 统计与聚类算法由 :mod:`atmos.insights.numeric` 用 numpy 自行实现，
  不引入 scikit-learn / scipy，保持"零配置一键启动"；
* 所有分析器只通过 :class:`~atmos.insights.loader.FrameLoader` 取数，
  口径与空值清洗集中在取数层；
* 数据不足时返回 ``{"available": False, "message": ...}`` 而不是抛错，
  前端据此给出"先去采集"的引导。
"""

from atmos.insights.loader import FrameLoader
from atmos.insights.service import INSIGHT_CATALOG, InsightService, InsightSpec

__all__ = ["InsightService", "InsightSpec", "INSIGHT_CATALOG", "FrameLoader"]
