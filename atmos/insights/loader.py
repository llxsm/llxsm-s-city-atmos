"""分析引擎的统一取数层。

所有分析器都通过 ``FrameLoader`` 拿数据，不直接写 SQL。这样做有三个好处：

1. **口径一致**：城市过滤、时间窗口、列投影只在这一处定义；
2. **空值清洗统一**：取数后立刻丢掉分析所需列全为空的行，避免每个分析器各写一套；
3. **替换存储实现时只改一处**：将来若把 DuckDB 换成 ClickHouse，分析器无需改动。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd

from atmos.analytics.environment import EnvironmentService
from atmos.analytics.query import LakeQuery
from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.logging_setup import get_logger
from atmos.settings import Settings, get_settings
from atmos.sources.specs import (
    ASSET_AIR_QUALITY_HOURLY,
    ASSET_ARCHIVE_HOURLY,
    ASSET_ENVIRONMENT_DAILY,
    ASSET_ENVIRONMENT_HOURLY,
)

logger = get_logger("insights.loader")

# 环境分析默认回溯天数（融合宽表按空气质量 92 天历史构建）
DEFAULT_HOURLY_DAYS = 30
DEFAULT_DAILY_DAYS = 90
DEFAULT_ARCHIVE_DAYS = 180


# 逐小时指标 → 日汇总资产中的对应列。
# 日汇总不是简单地把小时列改名（temperature_2m → temp_avg、precipitation → precipitation_sum），
# 因此需要一张显式映射表，否则"用小时指标名查日表"会静默查空。
DAILY_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "european_aqi": ("aqi_avg", "aqi_max"),
    "temperature_2m": ("temp_avg", "temp_max", "temp_min"),
    "relative_humidity_2m": ("humidity_avg",),
    "precipitation": ("precipitation_sum",),
    "wind_speed_10m": ("wind_speed_avg",),
    "pressure_msl": ("pressure_avg",),
    "pm2_5": ("pm2_5_avg",),
    "pm10": ("pm10_avg",),
}

# 日汇总列 → 用于展示的基础指标名（取单位与中文标签）
DAILY_BASE_METRICS: dict[str, str] = {
    "aqi_avg": "european_aqi",
    "aqi_max": "european_aqi",
    "temp_avg": "temperature_2m",
    "temp_max": "temperature_2m",
    "temp_min": "temperature_2m",
    "humidity_avg": "relative_humidity_2m",
    "precipitation_sum": "precipitation",
    "wind_speed_avg": "wind_speed_10m",
    "pressure_avg": "pressure_msl",
    "pm2_5_avg": "pm2_5",
    "pm10_avg": "pm10",
}


class FrameLoader:
    """分析用数据帧加载器。"""

    def __init__(
        self,
        registry: AssetRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry or load_registry()
        self.settings = settings or get_settings()
        self.query = LakeQuery(self.settings)
        self.environment = EnvironmentService(self.registry, self.settings)
        self._metric_cache: dict[str, dict[str, Any]] | None = None

    # ---------- 指标元数据 ----------
    def metric_catalog(self) -> dict[str, dict[str, Any]]:
        """指标名 → 元数据（中文标签、单位、值域），来自融合宽表的字段字典。"""
        if self._metric_cache is None:
            self._metric_cache = {
                item["name"]: item for item in self.environment.metric_catalog()
            }
        return self._metric_cache

    def metric_meta(self, name: str) -> dict[str, Any]:
        """单个指标的元数据；未登记时回退为列名本身。"""
        return self.metric_catalog().get(
            name, {"name": name, "label": name, "unit": None, "description": ""}
        )

    def metric_label(self, name: str) -> str:
        return str(self.metric_meta(name).get("label") or name)

    def resolve_daily_column(self, frame: pd.DataFrame, metric: str) -> str | None:
        """把逐小时指标名解析为日汇总资产中的实际列名。"""
        if metric in frame.columns:
            return metric
        for candidate in DAILY_COLUMN_ALIASES.get(metric, ()):
            if candidate in frame.columns:
                return candidate
        for suffix in ("_avg", "_sum", "_max"):
            candidate = f"{metric}{suffix}"
            if candidate in frame.columns:
                return candidate
        return None

    def daily_base_metric(self, column: str) -> str:
        """日汇总列名 → 基础指标名（用于取单位与中文标签）。"""
        return DAILY_BASE_METRICS.get(column, column)

    # ---------- 城市 ----------
    def city_index(self) -> dict[str, dict[str, Any]]:
        """启用中的城市 slug → 城市信息。"""
        return self.environment.city_index()

    def city_label(self, slug: str) -> str:
        return self.city_index().get(slug, {}).get("name_zh", slug)

    def known_cities(self) -> list[str]:
        return sorted(self.city_index())

    def resolve_cities(self, cities: list[str] | None) -> list[str]:
        """解析城市参数：为空表示全部启用城市；非法城市会被忽略并记录。"""
        known = self.known_cities()
        if not cities:
            return known
        requested = [item for item in cities if item in known]
        unknown = sorted(set(cities) - set(requested))
        if unknown:
            logger.warning("以下城市不可用，已忽略: %s", ", ".join(unknown))
        return requested

    # ---------- 数据帧 ----------
    def environment_hourly(
        self,
        cities: list[str] | None = None,
        *,
        days: int = DEFAULT_HOURLY_DAYS,
        columns: list[str] | None = None,
        require: list[str] | None = None,
    ) -> pd.DataFrame:
        """融合宽表逐小时数据。

        ``require`` 中声明的列若全为空，对应行会被剔除 —— 分析不允许
        "有行但没值"参与统计，否则均值会被大量空值稀释。
        """
        slugs = self.resolve_cities(cities)
        if not slugs:
            return pd.DataFrame()

        reference = datetime.utcnow()
        start = reference - timedelta(days=days)
        frame = self.query.environments(
            ASSET_ENVIRONMENT_HOURLY,
            city_slugs=slugs,
            columns=columns,
            start=start,
            order_by="time",
        )
        return self._clean(frame, require)

    def environment_daily(
        self,
        cities: list[str] | None = None,
        *,
        days: int = DEFAULT_DAILY_DAYS,
        require: list[str] | None = None,
    ) -> pd.DataFrame:
        """融合宽表逐日汇总。"""
        slugs = self.resolve_cities(cities)
        if not slugs:
            return pd.DataFrame()

        reference = date.today()
        frame = self.query.environments(
            ASSET_ENVIRONMENT_DAILY,
            city_slugs=slugs,
            start=datetime.combine(reference - timedelta(days=days), datetime.min.time()),
            end=datetime.combine(reference, datetime.min.time()),
            order_by="date",
        )
        return self._clean(frame, require)

    def archive_hourly(
        self,
        cities: list[str] | None = None,
        *,
        days: int = DEFAULT_ARCHIVE_DAYS,
        columns: list[str] | None = None,
        require: list[str] | None = None,
    ) -> pd.DataFrame:
        """ERA5 历史归档逐小时数据（用于长期趋势与同期对比）。"""
        slugs = self.resolve_cities(cities)
        if not slugs:
            return pd.DataFrame()

        end = date.today() - timedelta(days=6)
        start = end - timedelta(days=days)
        frame = self.query.environments(
            ASSET_ARCHIVE_HOURLY,
            city_slugs=slugs,
            columns=columns or ["city_id", "time", "temperature_2m", "precipitation", "wind_speed_10m"],
            start=datetime.combine(start, datetime.min.time()),
            end=datetime.combine(end, datetime.max.time()),
            order_by="time",
        )
        return self._clean(frame, require)

    def air_quality_hourly(
        self,
        cities: list[str] | None = None,
        *,
        days: int = 92,
        columns: list[str] | None = None,
        require: list[str] | None = None,
    ) -> pd.DataFrame:
        """空气质量逐小时数据（可回溯至 API 提供的 92 天上限）。"""
        slugs = self.resolve_cities(cities)
        if not slugs:
            return pd.DataFrame()

        reference = datetime.utcnow()
        frame = self.query.environments(
            ASSET_AIR_QUALITY_HOURLY,
            city_slugs=slugs,
            columns=columns
            or ["city_id", "time", "pm2_5", "pm10", "ozone", "european_aqi"],
            start=reference - timedelta(days=days),
            order_by="time",
        )
        return self._clean(frame, require)

    # ---------- 统计辅助 ----------
    @staticmethod
    def _clean(frame: pd.DataFrame, require: list[str] | None) -> pd.DataFrame:
        """丢弃必需列全空的行，并把时间列转成 datetime。"""
        if frame.empty:
            return frame
        result = frame.copy()
        if "time" in result.columns:
            result["time"] = pd.to_datetime(result["time"], errors="coerce")
            result = result.dropna(subset=["time"])
        if "date" in result.columns:
            result["date"] = pd.to_datetime(result["date"], errors="coerce")
            result = result.dropna(subset=["date"])
        if require:
            present = [name for name in require if name in result.columns]
            if present:
                result = result.dropna(subset=present)
        return result

    # ---------- 可用性 ----------
    def availability(self) -> dict[str, Any]:
        """各数据源当前可用的城市与时间范围，供前端提示"数据不足"。"""
        cities = self.known_cities()
        return {
            "cities": cities,
            "hourly_days": DEFAULT_HOURLY_DAYS,
            "daily_days": DEFAULT_DAILY_DAYS,
            "archive_days": DEFAULT_ARCHIVE_DAYS,
            "has_environment": self.query.has_data(ASSET_ENVIRONMENT_HOURLY),
            "has_daily": self.query.has_data(ASSET_ENVIRONMENT_DAILY),
            "has_archive": self.query.has_data(ASSET_ARCHIVE_HOURLY),
            "has_air_quality": self.query.has_data(ASSET_AIR_QUALITY_HOURLY),
        }


def numeric_columns(frame: pd.DataFrame, columns: list[str]) -> dict[str, Any]:
    """把若干列转成等长的 numpy 数组（先按行丢弃任一列为空的行）。

    相关矩阵与散点分析要求各列严格对齐，这里统一处理对齐逻辑。
    """
    import numpy as np

    present = [name for name in columns if name in frame.columns]
    if not present:
        return {}
    subset = frame[present].apply(pd.to_numeric, errors="coerce").dropna()
    if subset.empty:
        return {}
    return {name: subset[name].to_numpy(dtype=float) for name in present}


__all__ = [
    "FrameLoader",
    "numeric_columns",
    "DEFAULT_HOURLY_DAYS",
    "DEFAULT_DAILY_DAYS",
    "DEFAULT_ARCHIVE_DAYS",
]
