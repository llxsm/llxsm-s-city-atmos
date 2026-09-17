"""平台异常层级。

按失败归因分类，便于采集器决定"重试 / 跳过 / 中止"。
"""

from __future__ import annotations


class AtmosError(Exception):
    """平台异常基类。"""


class ConfigError(AtmosError):
    """配置或元数据定义非法，属于启动期硬失败。"""


class SourceError(AtmosError):
    """数据源访问失败。"""


class SourceUnavailableError(SourceError):
    """数据源暂时不可用（5xx / 超时），可重试。"""


class SourceRateLimitedError(SourceError):
    """触发数据源限流（429），应退避后重试。"""


class SourceResponseError(SourceError):
    """数据源返回结构或内容不符合预期，重试通常无效。"""


class LakeError(AtmosError):
    """数据湖读写失败。"""


class CatalogError(AtmosError):
    """资产目录读写失败。"""


class QualityError(AtmosError):
    """质量评测失败。"""
