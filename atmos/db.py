"""数据库会话与初始化。

SQLite 作为资产目录存储，启用 WAL 与外键约束以保证并发读与引用完整性。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from atmos.logging_setup import get_logger
from atmos.models import Base
from atmos.settings import Settings, get_settings

logger = get_logger("db")

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def build_engine(settings: Settings | None = None) -> Engine:
    """构建 SQLAlchemy 引擎并挂载 SQLite 优化项。"""
    settings = settings or get_settings()
    settings.ensure_directories()
    url = f"sqlite+pysqlite:///{settings.catalog_db_path}"

    engine = create_engine(
        url,
        future=True,
        echo=False,
        # FastAPI 多线程访问同一 SQLite 文件
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    return engine


def get_engine(settings: Settings | None = None) -> Engine:
    """获取进程内单例引擎。"""
    global _engine
    if _engine is None:
        _engine = build_engine(settings)
    return _engine


def get_session_factory(settings: Settings | None = None) -> sessionmaker[Session]:
    """获取会话工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = sessionmaker(
            bind=get_engine(settings), autoflush=False, expire_on_commit=False, future=True
        )
    return _session_factory


def reset_engine() -> None:
    """销毁单例引擎（测试或切换数据库路径时使用）。"""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


def init_db(settings: Settings | None = None) -> Engine:
    """建表（幂等）并返回引擎。"""
    engine = get_engine(settings)
    Base.metadata.create_all(engine)
    logger.debug("资产目录已就绪: %s", engine.url.database)
    return engine


@contextmanager
def session_scope() -> Iterator[Session]:
    """事务性会话上下文：正常提交，异常回滚。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI 依赖注入用的会话生成器。"""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        session.close()


def healthcheck() -> dict[str, object]:
    """目录库连通性自检。"""
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "backend": "sqlite", "path": str(get_engine().url.database)}
    except Exception as exc:  # pragma: no cover - 仅在库损坏时触发
        return {"status": "error", "backend": "sqlite", "error": str(exc)}
