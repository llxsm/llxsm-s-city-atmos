"""两个平台共享的应用层组件。

包含依赖注入、请求模型、后台任务登记簿与 FastAPI 装配工具。
这里的任何改动都会同时影响分析与治理平台，因此保持最小且稳定。
"""

from atmos.apps.common.deps import (
    get_app_settings,
    get_environment_service,
    get_insight_service,
    get_quality_engine,
    get_registry,
    reset_caches,
)
from atmos.apps.common.schemas import (
    CityCreateRequest,
    CityUpdateRequest,
    CollectRequest,
    QualityRequest,
)
from atmos.apps.common.tasks import TaskRecord, TaskRegistry, get_task_registry
from atmos.apps.common.webapp import build_app

__all__ = [
    "build_app",
    "get_registry",
    "get_environment_service",
    "get_insight_service",
    "get_quality_engine",
    "get_app_settings",
    "reset_caches",
    "CollectRequest",
    "QualityRequest",
    "CityCreateRequest",
    "CityUpdateRequest",
    "TaskRecord",
    "TaskRegistry",
    "get_task_registry",
]
