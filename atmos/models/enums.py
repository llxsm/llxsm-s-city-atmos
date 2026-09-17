"""平台领域枚举。

全部继承 ``str`` 以便直接序列化进 JSON / SQLite，无需额外转换。
"""

from __future__ import annotations

from enum import Enum


class StrEnum(str, Enum):
    """字符串枚举基类。"""

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return self.value

    @classmethod
    def values(cls) -> list[str]:
        return [member.value for member in cls]


class AssetLayer(StrEnum):
    """数据资产分层（数据湖分层建模）。"""

    REFERENCE = "reference"
    RAW = "raw"
    REFINED = "refined"
    SERVING = "serving"

    @property
    def label(self) -> str:
        return {
            "reference": "参考层",
            "raw": "原始层",
            "refined": "精炼层",
            "serving": "服务层",
        }[self.value]


class AssetDomain(StrEnum):
    """数据资产业务域。"""

    WEATHER = "weather"
    AIR_QUALITY = "air_quality"
    INTEGRATED = "integrated"
    REFERENCE = "reference"

    @property
    def label(self) -> str:
        return {
            "weather": "气象",
            "air_quality": "空气质量",
            "integrated": "环境融合",
            "reference": "参考数据",
        }[self.value]


class AssetStatus(StrEnum):
    """资产生命周期状态。"""

    ACTIVE = "active"
    DRAFT = "draft"
    DEPRECATED = "deprecated"
    RETIRED = "retired"

    @property
    def label(self) -> str:
        return {
            "active": "在用",
            "draft": "草稿",
            "deprecated": "已弃用",
            "retired": "已下线",
        }[self.value]


class Granularity(StrEnum):
    """数据时间粒度。"""

    HOURLY = "hourly"
    DAILY = "daily"
    STATIC = "static"

    @property
    def label(self) -> str:
        return {"hourly": "逐小时", "daily": "逐日", "static": "静态"}[self.value]

    @property
    def hours(self) -> float | None:
        """粒度对应小时数，静态资产返回 None。"""
        return {"hourly": 1.0, "daily": 24.0, "static": None}[self.value]


class UpdateFrequency(StrEnum):
    """资产更新频率。"""

    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    ON_DEMAND = "on_demand"

    @property
    def label(self) -> str:
        return {
            "hourly": "每小时",
            "daily": "每天",
            "weekly": "每周",
            "monthly": "每月",
            "on_demand": "按需",
        }[self.value]

    @property
    def seconds(self) -> int | None:
        return {
            "hourly": 3600,
            "daily": 86400,
            "weekly": 604800,
            "monthly": 2592000,
            "on_demand": None,
        }[self.value]


class Sensitivity(StrEnum):
    """数据敏感级别（数据安全分级）。"""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"

    @property
    def label(self) -> str:
        return {"public": "公开", "internal": "内部", "confidential": "机密"}[self.value]


class RunStatus(StrEnum):
    """采集运行状态。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def label(self) -> str:
        return {
            "pending": "等待中",
            "running": "运行中",
            "success": "成功",
            "partial": "部分成功",
            "failed": "失败",
            "skipped": "已跳过",
        }[self.value]


class TriggerType(StrEnum):
    """运行触发方式。"""

    MANUAL = "manual"
    SCHEDULED = "scheduled"
    API = "api"
    CLI = "cli"
    BOOTSTRAP = "bootstrap"


class QualityDimension(StrEnum):
    """数据质量五维模型。"""

    COMPLETENESS = "completeness"
    TIMELINESS = "timeliness"
    VALIDITY = "validity"
    CONSISTENCY = "consistency"
    UNIQUENESS = "uniqueness"

    @property
    def label(self) -> str:
        return {
            "completeness": "完整性",
            "timeliness": "时效性",
            "validity": "有效性",
            "consistency": "一致性",
            "uniqueness": "唯一性",
        }[self.value]


class CheckSeverity(StrEnum):
    """检查项严重级别。"""

    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"

    @property
    def label(self) -> str:
        return {"critical": "严重", "major": "重要", "minor": "次要"}[self.value]

    @property
    def weight(self) -> float:
        """严重级别在维度内的权重系数。"""
        return {"critical": 3.0, "major": 2.0, "minor": 1.0}[self.value]


class CheckStatus(StrEnum):
    """单次检查结果状态。"""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    ERROR = "error"
    SKIPPED = "skipped"

    @property
    def label(self) -> str:
        return {
            "pass": "通过",
            "warn": "预警",
            "fail": "不通过",
            "error": "执行异常",
            "skipped": "已跳过",
        }[self.value]


class Grade(StrEnum):
    """资产质量等级。"""

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E = "E"


class TransformType(StrEnum):
    """血缘转换类型。"""

    REFERENCE = "reference"
    DERIVE = "derive"
    JOIN = "join"
    AGGREGATE = "aggregate"
    ENRICH = "enrich"

    @property
    def label(self) -> str:
        return {
            "reference": "维度引用",
            "derive": "字段派生",
            "join": "关联融合",
            "aggregate": "聚合汇总",
            "enrich": "外部增强",
        }[self.value]


class AQILevel(StrEnum):
    """空气质量等级（沿用中国 GB 3095 表述，映射自欧洲 AQI 区间）。"""

    EXCELLENT = "优"
    GOOD = "良"
    LIGHT = "轻度污染"
    MODERATE = "中度污染"
    HEAVY = "重度污染"
    SEVERE = "严重污染"

    @property
    def color(self) -> str:
        return {
            "优": "#00e400",
            "良": "#ffff00",
            "轻度污染": "#ff7e00",
            "中度污染": "#ff0000",
            "重度污染": "#99004c",
            "严重污染": "#7e0023",
        }[self.value]
