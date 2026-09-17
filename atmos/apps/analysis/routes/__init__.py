"""分析平台路由。

面向**业务分析人员**，围绕"回答业务问题"组织，不含资产治理视图：

* ``meta``        平台健康、数据源元信息、数据覆盖总览与可用性
* ``environment`` 实时实况、逐小时/逐日时序、多城市对比、排名、预警、历史趋势
* ``insights``    六大类深度分析（统计分布、时段规律、污染过程、归因、考核、聚类）
* ``export``      数据集下载（CSV / JSON）
* ``ops``         数据刷新（触发采集）与后台任务进度
"""

from atmos.apps.analysis.routes import environment, export, insights, meta, ops

__all__ = ["meta", "environment", "insights", "export", "ops"]
