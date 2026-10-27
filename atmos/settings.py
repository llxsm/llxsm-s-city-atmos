"""平台运行时配置。

全部配置项均可通过环境变量覆盖（前缀 ``ATMOS_``），也可写入项目根目录的
``.env``。所有项都有可用默认值，因此零配置即可启动。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录：atmos/settings.py -> atmos/ -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """平台全局设置。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="ATMOS_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------- 运行环境 ----------
    env: str = "dev"
    log_level: str = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000
    governance_port: int = 8001
    debug: bool = False

    # ---------- 前端访问地址 ----------
    # 统一前端需要在浏览器里同时访问两个服务，因此必须告诉它两个后端的可达地址。
    # 留空时按 host:port 自动推断；两个服务部署在不同机器时显式配置这两个值，
    # 并相应配置下面的 cors_origins。
    analysis_public_url: str = ""
    governance_public_url: str = ""

    # 允许的跨源来源（逗号分隔，如 ``https://a.example.com,https://b.example.com``）。
    # 留空时的行为随环境而变，两种都是有意的：
    #   * ``env=dev``  → 放开为 ``*``，方便前端在任意端口调试；
    #   * 其他环境      → 为空列表，即**不发** CORS 头。同源部署（反向代理按路径
    #     分流两个门户，推荐形态）下这正是需要的；两个服务分处不同域名/端口时
    #     必须显式填写，否则浏览器会拦下跨源请求。
    cors_origins: str = ""

    # ---------- 访问控制 ----------
    # 应用内 HTTP Basic 认证：两项**同时**配置才生效，留空则整个平台无鉴权。
    # 之所以放在应用层而不只依赖反向代理：服务监听地址可能被改成 0.0.0.0、
    # 也可能被换机器部署，代理配置不会跟着走，而这道门在代码里。
    auth_user: str = ""
    auth_password: str = ""

    # 启动服务后自动用默认浏览器打开看板。
    # 无图形界面的环境（服务器、CI、WSL 无 X）请设为 false，或用 ATMOS_OPEN_BROWSER=false。
    open_browser: bool = True

    # ---------- 路径 ----------
    lake_dir: Path = Path("data/lake")
    catalog_db: Path = Path("data/catalog.db")
    config_dir: Path = Path("config")
    export_dir: Path = Path("data/exports")

    # ---------- Open-Meteo 数据源 ----------
    openmeteo_api_key: str = ""
    openmeteo_forecast_host: str = "https://api.open-meteo.com"
    openmeteo_archive_host: str = "https://archive-api.open-meteo.com"
    openmeteo_air_host: str = "https://air-quality-api.open-meteo.com"
    openmeteo_geocoding_host: str = "https://geocoding-api.open-meteo.com"

    # ---------- HTTP 治理 ----------
    http_timeout_seconds: float = 30.0
    http_max_retries: int = 4
    http_min_interval_ms: int = 120
    http_backoff_base_seconds: float = 1.5
    http_user_agent: str = "city-atmos/0.1 (+data-asset-platform)"

    # ---------- 采集策略 ----------
    collect_max_cities: int = 20
    archive_lookback_days: int = 180
    # 空气质量 API 最多提供 92 天历史；这决定了融合宽表与日汇总能回溯多久，
    # 也直接决定"达标考核"能覆盖多长的周期。
    air_quality_history_days: int = 92
    forecast_horizon_days: int = 7
    forecast_past_days: int = 2

    # ---------- 调度 ----------
    # 由 `python -m atmos schedule` 这个常驻进程消费；不跑该命令就不会有任何
    # 自动任务。刻意不做成"Web 服务里内嵌定时器"：两个门户都会启动 Web 服务，
    # 内嵌定时器会让同一份数据被采集两遍。
    # 值留空表示**停用**该作业。
    schedule_collect_cron: str = "15 * * * *"   # 每小时第 15 分：增量采集（跳过归档）
    schedule_quality_cron: str = "30 * * * *"   # 每小时第 30 分：五维质量评测
    schedule_archive_cron: str = "20 4 * * *"   # 每天 04:20：回补 ERA5 归档（上游约 5 天延迟）
    schedule_prune_cron: str = "30 3 * * *"     # 每天 03:30：清理过期运行记录（不碰 parquet）

    # ---------- 保留策略 ----------
    # 只影响**元数据与导出文件**，数据湖（parquet）不在清理范围内：
    # 那是唯一删了要重新采集才能回来的东西，而运行记录、质量明细只是观测历史。
    # 默认 90 天足够回答"上个季度这条链路稳不稳"，又不至于让 SQLite 无限长大。
    retention_days: int = 90
    retention_export_days: int = 30

    # ---------- 校验 ----------
    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"非法日志级别: {value}")
        return level

    @field_validator("collect_max_cities", "http_max_retries")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value < 1:
            raise ValueError("该配置项必须为正整数")
        return value

    # ---------- 派生属性 ----------
    @property
    def auth_enabled(self) -> bool:
        """应用内认证是否启用（用户名与密码都配置了才算）。"""
        return bool(self.auth_user and self.auth_password)

    def cors_origin_list(self) -> list[str]:
        """解析 ``cors_origins``，去掉空白与尾斜杠（Origin 头从不带尾斜杠）。"""
        return [item.strip().rstrip("/") for item in self.cors_origins.split(",") if item.strip()]

    @property
    def lake_path(self) -> Path:
        """数据湖绝对路径。"""
        return self._absolute(self.lake_dir)

    @property
    def catalog_db_path(self) -> Path:
        """资产目录 SQLite 绝对路径。"""
        return self._absolute(self.catalog_db)

    @property
    def config_path(self) -> Path:
        """配置目录绝对路径。"""
        return self._absolute(self.config_dir)

    @property
    def export_path(self) -> Path:
        """导出目录绝对路径。"""
        return self._absolute(self.export_dir)

    @property
    def web_dir(self) -> Path:
        """前端静态资源目录。"""
        return PROJECT_ROOT / "web"

    @property
    def app_dir(self) -> Path:
        """统一前端的根目录（两个服务都托管同一份前端）。"""
        unified = PROJECT_ROOT / "web" / "app"
        return unified if unified.exists() else PROJECT_ROOT / "web"

    @property
    def analysis_url(self) -> str:
        """浏览器可达的分析平台地址。"""
        return self.analysis_public_url or self._browser_url(self.port)

    @property
    def governance_url(self) -> str:
        """浏览器可达的治理平台地址。"""
        return self.governance_public_url or self._browser_url(self.governance_port)

    def _browser_url(self, port: int) -> str:
        """把监听地址转成浏览器可用地址（0.0.0.0 对浏览器没有意义）。"""
        host = self.host
        if host in {"0.0.0.0", "::", ""}:
            host = "127.0.0.1"
        return f"http://{host}:{port}"

    def _absolute(self, path: Path) -> Path:
        return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()

    def ensure_directories(self) -> None:
        """创建运行所需目录（幂等）。"""
        for directory in (self.lake_path, self.catalog_db_path.parent, self.export_path):
            directory.mkdir(parents=True, exist_ok=True)

    def source_hosts(self) -> dict[str, str]:
        """按数据源产品返回 API host 映射。"""
        return {
            "forecast": self.openmeteo_forecast_host.rstrip("/"),
            "archive": self.openmeteo_archive_host.rstrip("/"),
            "air_quality": self.openmeteo_air_host.rstrip("/"),
            "geocoding": self.openmeteo_geocoding_host.rstrip("/"),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取单例配置对象（进程内缓存）。"""
    settings = Settings()
    settings.ensure_directories()
    return settings


def reload_settings() -> Settings:
    """清除缓存并重新加载配置（供测试或运行时重载使用）。"""
    get_settings.cache_clear()
    return get_settings()
