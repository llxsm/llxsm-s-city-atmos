"""应用内 HTTP Basic 认证（可选，默认关闭）。

**为什么不只靠反向代理做认证。** 代理层认证（Nginx ``auth_basic``）有一个隐性
假设：所有流量都真的经过代理。而这个项目允许 ``--host 0.0.0.0``、允许两个服务
分处不同机器、也允许别人直接开 8000 端口看接口文档 —— 一旦代理被绕过、
配置被复制漏了行、或者换机器部署，代理上的门就不存在了。把门放在应用里，
它跟着代码走。

**为什么不默认开启。** 默认开启会让本地开发、``verify_platform.py`` 与全部
集成测试都必须先配凭据，门槛与收益不成比例。两项都配置即生效，是这个项目
"零配置可跑、配置了就按配置来"的一致做法。

**只配一半视为未启用并告警。** ``ATMOS_AUTH_USER`` 配了而密码忘了配，
几乎总是配置写漏，静默放行是最坏的结果。
"""

from __future__ import annotations

import base64
import secrets

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from atmos.logging_setup import get_logger
from atmos.settings import Settings

logger = get_logger("apps.auth")

REALM = "atmos"

#: 免认证路径。健康检查是"进程活着吗"的探针，被 systemd、容器编排与外部监控
#: 反复调用，要求它带凭据会让每个探针都要单独维护一份密码。它只返回状态与
#: 版本号，不泄露任何业务数据。
EXEMPT_PATHS = frozenset({"/api/health"})


def _unauthorized() -> JSONResponse:
    """401 必须带 ``WWW-Authenticate``，否则浏览器不会弹登录框。"""
    return JSONResponse(
        status_code=401,
        content={
            "ok": False,
            "error": "Unauthorized",
            "message": "需要认证：请在浏览器弹出的登录框中输入账号密码",
        },
        headers={
            "WWW-Authenticate": f'Basic realm="{REALM}", charset="UTF-8"',
            "Cache-Control": "no-store",
        },
    )


def register_basic_auth(app: FastAPI, settings: Settings) -> None:
    """按配置挂载认证中间件（未配置时什么都不做）。

    比较用 ``secrets.compare_digest`` 而不是 ``==``：后者会在首个不同字节处
    提前返回，逐字节的响应时间差可被用来猜密码。
    """
    if not settings.auth_enabled:
        if settings.auth_user or settings.auth_password:
            logger.warning(
                "认证配置不完整（ATMOS_AUTH_USER 与 ATMOS_AUTH_PASSWORD 需同时设置），"
                "已按**未启用**处理 —— 平台当前无访问控制"
            )
        return

    expected = base64.b64encode(
        f"{settings.auth_user}:{settings.auth_password}".encode()
    ).decode("ascii")

    @app.middleware("http")
    async def basic_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
        # 预检请求按规范不带凭据，拦下它会让跨源部署直接失效
        if request.method == "OPTIONS" or request.url.path in EXEMPT_PATHS:
            return await call_next(request)

        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "basic" and secrets.compare_digest(token.strip(), expected):
            return await call_next(request)
        return _unauthorized()

    logger.info("已启用 HTTP Basic 认证（用户 %s）", settings.auth_user)


__all__ = ["EXEMPT_PATHS", "REALM", "register_basic_auth"]
