"""城市环境数据分析平台（门户应用）。

面向业务分析人员：把采集到的环境数据变成**结论**，
而不是罗列数据集与治理信息。

启动::

    python -m atmos serve --portal analysis            # 默认 8000
    python -m atmos serve --portal both                # 同时起两个平台
"""

from __future__ import annotations

from fastapi import FastAPI

from atmos.apps.analysis.routes import environment, export, insights, meta, ops
from atmos.apps.common.webapp import build_app

DESCRIPTION = """
以 **Open-Meteo** 为数据源的**城市环境数据分析平台**。

提供从实时实况到深度分析的两个层次：

**基础数据服务**
* 实时实况（含空气质量等级、首要污染物与健康提示）
* 逐小时 / 逐日时序，带实况与预报分界
* 多城市同指标对比、空气质量排名、阈值预警
* 基于 ERA5 历史归档的长期趋势

**六大类深度分析**
1. **统计与分布** —— 分位数、箱线图、直方图、多指标相关性矩阵（Pearson + Spearman）
2. **时段规律画像** —— 日内小时热力图、工作日与周末对比、月度趋势
3. **异常与污染过程** —— 异常点检测；把连续超标时段合并为"污染过程"，
   给出起止时间、持续时长、峰值、首要污染物与期间气象条件
4. **归因分析** —— 气象要素与污染物的散点、相关系数、分箱趋势与一元回归
5. **达标考核与报表** —— 优良天比例、等级天数分布、超标统计、环比同比、目标完成度、CSV 导出
6. **横向对比** —— 城市聚类与相似度矩阵

所有分析在数据不足时返回可读的中文说明而不是报错。
"""


def create_app() -> FastAPI:
    """构建分析平台应用。"""
    return build_app(
        title="城市环境数据分析平台",
        description=DESCRIPTION,
        portal="analysis",
        routers=[meta.router, environment.router, insights.router, export.router, ops.router],
    )


app = create_app()


__all__ = ["app", "create_app"]
