"""两个平台共享的依赖提供者。

服务对象均为无状态或只读，用 ``lru_cache`` 复用；
数据库会话按请求创建，保证事务边界清晰。
"""

from __future__ import annotations

from functools import lru_cache

from atmos.analytics.environment import EnvironmentService
from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.insights.service import InsightService
from atmos.quality.engine import QualityEngine
from atmos.settings import Settings, get_settings


@lru_cache(maxsize=1)
def get_registry() -> AssetRegistry:
    """平台元数据注册表。"""
    return load_registry()


@lru_cache(maxsize=1)
def get_environment_service() -> EnvironmentService:
    """环境数据读模型（实况、时序、对比、排名、预警、趋势）。"""
    return EnvironmentService(get_registry(), get_settings())


@lru_cache(maxsize=1)
def get_insight_service() -> InsightService:
    """深度分析引擎（统计分布、污染过程、归因、考核、聚类）。"""
    return InsightService(get_registry(), get_settings())


@lru_cache(maxsize=1)
def get_quality_engine() -> QualityEngine:
    """质量评测引擎（治理平台使用）。"""
    return QualityEngine(get_registry(), get_settings())


def get_app_settings() -> Settings:
    """运行时配置。"""
    return get_settings()


def reset_caches() -> None:
    """清除依赖缓存（配置热更新或测试使用）。"""
    get_registry.cache_clear()
    get_environment_service.cache_clear()
    get_insight_service.cache_clear()
    get_quality_engine.cache_clear()


__all__ = [
    "get_registry",
    "get_environment_service",
    "get_insight_service",
    "get_quality_engine",
    "get_app_settings",
    "reset_caches",
]
