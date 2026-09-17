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
    # 并相应调整 CORS 允许来源（见 atmos/apps/common/webapp.py）。
    analysis_public_url: str = ""
    governance_public_url: str = ""

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
    scheduler_enabled: bool = False
    schedule_collect_cron: str = "15 * * * *"
    schedule_quality_cron: str = "30 * * * *"

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
