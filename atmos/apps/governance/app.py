"""数据资产治理平台（门户应用）。

面向数据治理部门：把数据集当作受治理的**资产**来管理 ——
谁负责、口径是什么、字段什么含义、从哪来到哪去、质量好不好。

刻意精简为三块核心能力 + 运行审计，不包含数据刷新触发与环境数据查询，
使治理部门的职责边界清晰、不与环境分析工作重叠。

启动::

    python -m atmos serve --portal governance          # 默认 8001
    python -m atmos serve --portal both                # 同时起两个平台
"""

from __future__ import annotations

from fastapi import FastAPI

from atmos.apps.common.webapp import build_app
from atmos.apps.governance.routes import catalog, export, lineage, meta, ops, quality

DESCRIPTION = """
以 **Open-Meteo** 为数据源的城市环境**数据资产治理平台**。

平台把每个数据集注册为一项受治理的**数据资产**，提供三块核心能力：

**1. 资产目录**
* 治理元数据：责任人（owner）、数据管理员（steward）、SLA 新鲜度与完整度要求、
  更新频率、分层（参考层 / 原始层 / 精炼层 / 服务层）、敏感级别、来源与许可
* **字段字典**：161 个字段的类型、单位、业务含义、值域约束、枚举集合、
  是否主键、是否派生 —— 它同时是质量引擎的校验输入
* 版本快照：每次落湖的分区统计与体积变化
* 数据预览：样例数据与列级画像（空值率、唯一值数、极值、越界计数）

**2. 数据血缘**
* 从数据源原始端点，经采集作业与关联变换，到对外服务层数据集的完整有向图
* **影响分析**（这个资产坏了会影响谁）与**根因追溯**（这张表脏了问题出在哪）
* 血缘孤岛巡检：找出未接入血缘或缺少转换说明的资产

**3. 质量评分**
* 五维模型：完整性、时效性、有效性、一致性、唯一性
* 14 项检查，按严重级别加权合成维度得分与总分，映射 A–E 等级
* 逐项明细：观测值、阈值、违规样本、影响记录数

**运维审计**：作业清单、逐条运行记录（行数、字节、API 调用、去重数、
schema 漂移、错误信息）、元数据同步与统计刷新。
"""


def create_app() -> FastAPI:
    """构建治理平台应用。"""
    return build_app(
        title="数据资产治理平台",
        description=DESCRIPTION,
        portal="governance",
        routers=[
            meta.router,
            catalog.router,
            lineage.router,
            quality.router,
            export.router,
            ops.router,
        ],
    )


app = create_app()


__all__ = ["app", "create_app"]
