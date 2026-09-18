"""两个平台共享的应用装配工具。

拆成两个服务后最容易失控的是"两边各写一套 FastAPI 配置"——
CORS、错误处理、静态资源挂载、生命周期这些横切关注点必须只有一份实现，
否则安全策略或错误格式会在两个服务间悄悄分叉。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from atmos import __version__
from atmos.catalog.bootstrap import bootstrap_catalog
from atmos.catalog.definitions import load_registry
from atmos.db import init_db, session_scope
from atmos.logging_setup import get_logger, setup_logging
from atmos.models import DataAsset
from atmos.settings import Settings, get_settings
from atmos.utils import iso_z, utcnow

logger = get_logger("apps.common")


@asynccontextmanager
async def portal_lifespan(app: FastAPI, portal: str):
    """两个平台共用的启动流程：日志 → 建表 → 幂等引导。"""
    settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("启动 %s（portal=%s, env=%s, v%s）", app.title, portal, settings.env, __version__)

    init_db(settings)
    try:
        with session_scope() as session:
            asset_count = session.scalar(select(func.count(DataAsset.id))) or 0
        if asset_count == 0:
            logger.info("资产目录为空，执行首次引导")
        # 引导幂等：既完成首次建册，也把配置变更（新增/移除城市、字段字典调整）
        # 投影到目录。两个平台都会执行，SQLite 的 busy_timeout 负责串行化。
        bootstrap_catalog(load_registry())
    except Exception as exc:  # pragma: no cover - 配置错误时仍允许启动以查看健康检查
        logger.error("目录引导失败: %s", exc)

    yield
    logger.info("%s 已停止", app.title)


def register_error_handlers(app: FastAPI) -> None:
    """统一错误响应结构，便于前端一致处理。"""

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:  # pragma: no cover
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": type(exc).__name__,
                "message": str(exc),
                "path": request.url.path,
                "generated_at": iso_z(utcnow()),
            },
        )


def register_frontend_cache(app: FastAPI) -> None:
    """给无构建步骤的前端资源加上"先校验、再使用"的缓存策略。

    前端文件名没有内容哈希，而 ``StaticFiles`` 只发 ETag / Last-Modified、
    不发 ``Cache-Control``。此时浏览器会按 RFC 9111 的启发式规则（以
    ``Last-Modified`` 推算新鲜期）**直接复用**缓存里的旧模块，于是出现
    "源码已经改好、页面仍报旧错误"的假象；动态 ``import()`` 又不遵循强制刷新，
    用户按 Ctrl+F5 也未必能拿到新模块。

    统一标成 ``no-cache``：仍然允许缓存，但每次使用前必须先带 ETag 校验，
    内容未变时只是一次 304，代价可忽略。
    （``/portal-config.js`` 是运行时注入的，因此用 ``no-store``，见下。）
    """

    @app.middleware("http")
    async def frontend_revalidate(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.endswith("/index.html") or path.startswith(("/shared/", "/static/")):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response


def register_frontend(app: FastAPI, settings: Settings, portal: str) -> None:
    """挂载共享资源、统一前端与运行时门户配置。

    两个服务托管**同一份**统一前端（``web/app``），差别只在注入的
    ``/portal-config.js``：它告诉前端自身默认展示哪个门户、以及两个后端的
    浏览器可达地址。这样治理部门收藏 8001 就落在治理页签，其他人用 8000
    落在分析页签，而前端代码只有一份。

    路径约定::

        /shared            -> web/shared       共享的 JS/CSS
        /portal-config.js  -> 运行时生成        门户标识与两个后端地址
        /static            -> web/app/static   统一前端模块
        /                  -> web/app          统一前端外壳
    """
    shared_dir = settings.web_dir / "shared"
    app_dir = settings.app_dir

    if shared_dir.exists():
        app.mount("/shared", StaticFiles(directory=str(shared_dir)), name="shared")

    @app.get("/portal-config.js", include_in_schema=False)
    async def portal_config() -> Response:
        """注入门户标识与两个后端地址（必须早于挂载 ``/`` 注册）。"""
        payload = {
            "current": portal,
            "endpoints": {
                "analysis": settings.analysis_url,
                "governance": settings.governance_url,
            },
            "environment": settings.env,
        }
        body = "window.__ATMOS_PORTALS__ = " + json.dumps(payload, ensure_ascii=False) + ";\n"
        return Response(
            content=body.encode("utf-8"),
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    if (app_dir / "index.html").exists():
        app.mount(
            "/static",
            StaticFiles(directory=str(app_dir / "static"), check_dir=False),
            name="static",
        )

        @app.get("/", include_in_schema=False)
        async def portal_index() -> FileResponse:
            return FileResponse(app_dir / "index.html")

        app.mount("/", StaticFiles(directory=str(app_dir), html=True), name="frontend")
        return

    @app.get("/", include_in_schema=False)
    async def placeholder() -> JSONResponse:  # pragma: no cover - 仅在裁剪部署时出现
        return JSONResponse(
            {
                "message": "前端资源未安装，请访问 /api/docs 查看接口文档",
                "expected_dir": str(app_dir),
            }
        )


def build_app(
    *,
    title: str,
    description: str,
    portal: str,
    routers: Iterator[APIRouter] | list[APIRouter],
    settings: Settings | None = None,
) -> FastAPI:
    """构造一个门户应用。

    所有门户共享同一套：文档路径、CORS 策略、错误处理、静态资源约定与启动流程。
    """
    settings = settings or get_settings()
    settings.ensure_directories()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with portal_lifespan(app, portal):
            yield

    app = FastAPI(
        title=title,
        description=description,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if settings.env == "dev" else [],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for router in routers:
        app.include_router(router, prefix="/api")

    _register_api_fallback(app)
    register_error_handlers(app)
    register_frontend_cache(app)
    register_frontend(app, settings, portal)
    return app


def _register_api_fallback(app: FastAPI) -> None:
    """未匹配的 ``/api/*`` 统一返回 404 JSON。

    没有这一层时，未注册的 API 路径会落到挂在 ``/`` 的静态文件处理器上，
    对 POST 返回 405 —— 对调用方来说"路径不存在"和"方法不允许"是完全不同的
    信号，容易误导排查方向。必须在挂载静态资源**之前**注册。
    """

    @app.api_route(
        "/api/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
        include_in_schema=False,
    )
    async def api_not_found(rest: str) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "ok": False,
                "error": "NotFound",
                "message": f"该服务不提供此接口: /api/{rest}",
                "hint": "两个平台的能力边界不同，请确认请求发往了正确的端口"
                "（分析平台 8000 / 治理平台 8001）",
                "generated_at": iso_z(utcnow()),
            },
        )


__all__ = [
    "build_app",
    "portal_lifespan",
    "register_error_handlers",
    "register_frontend",
    "register_frontend_cache",
]
