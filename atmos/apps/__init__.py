"""平台应用层。

本包只做"装配"，不含业务逻辑：把共享核心（``atmos.*``）里的能力
组合成两个可独立部署的服务。

* :mod:`atmos.apps.analysis`   —— 数据分析平台（默认 8000 端口）
* :mod:`atmos.apps.governance` —— 数据治理平台（默认 8001 端口）
* :mod:`atmos.apps.common`     —— 两者共享的依赖注入、请求模型、任务登记与装配工具
"""

from __future__ import annotations

__all__ = ["analysis", "governance", "common"]
