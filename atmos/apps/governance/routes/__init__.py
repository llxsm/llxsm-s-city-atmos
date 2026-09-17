"""治理平台路由。

面向**数据治理部门**，只保留核心三块能力与审计：

* ``meta``     平台健康、枚举与质量模型字典、资产目录总览
* ``catalog``  数据资产目录（含字段字典）与城市主数据
* ``lineage``  数据血缘图谱与影响分析
* ``quality``  五维质量评分与检查明细
* ``export``   目录元数据导出
* ``ops``      作业清单、运行记录审计、元数据同步与统计刷新

**不包含**：治理体检视图（已并入总览与血缘巡检）、数据刷新触发
（属于分析平台）、环境数据查询（不属于治理范畴）。
"""

from atmos.apps.governance.routes import catalog, export, lineage, meta, ops, quality

__all__ = ["meta", "catalog", "lineage", "quality", "export", "ops"]
