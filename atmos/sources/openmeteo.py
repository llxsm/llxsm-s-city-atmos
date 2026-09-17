"""Open-Meteo 数据源客户端。

职责边界
--------
本模块只负责「把数据源的响应变成整洁的行集」，不含任何落湖或治理逻辑。
这样数据源替换（例如将来接入和风、AQICN）不会污染采集与质量层。

工程约束
--------
* **限流**：全局串行化最小请求间隔，避免触发 Open-Meteo 免费额度限制。
* **重试**：仅对可恢复错误（429 / 5xx / 网络超时）退避重试，
  对 4xx 与业务错误立即失败，防止把无效请求放大成额度消耗。
* **契约校验**：请求的变量若一个都没返回，视为数据源契约破坏并显式报错。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from atmos.errors import (
    SourceRateLimitedError,
    SourceResponseError,
    SourceUnavailableError,
)
from atmos.logging_setup import get_logger
from atmos.settings import Settings, get_settings
from atmos.sources.specs import (
    AIR_QUALITY_HOURLY_VARIABLES,
    ARCHIVE_HOURLY_VARIABLES,
    ARCHIVE_PUBLICATION_LAG_DAYS,
    WEATHER_DAILY_VARIABLES,
    WEATHER_HOURLY_VARIABLES,
)

logger = get_logger("sources.openmeteo")

_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class FetchResult:
    """一次数据源取数的结果（整洁行集 + 溯源信息）。"""

    rows: list[dict[str, Any]] = field(default_factory=list)
    units: dict[str, str] = field(default_factory=dict)
    variables: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)
    api_calls: int = 1
    row_count: int = 0

    def __post_init__(self) -> None:
        self.row_count = self.row_count or len(self.rows)


@dataclass
class FetchRequest:
    """城市级取数请求。"""

    city_slug: str
    latitude: float
    longitude: float
    timezone: str


class _RateLimiter:
    """跨协程共享的最小请求间隔限流器。"""

    def __init__(self, min_interval_ms: int) -> None:
        self._min_interval = max(0, min_interval_ms) / 1000.0
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def acquire(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            elapsed = time.monotonic() - self._last_call
            wait_for = self._min_interval - elapsed
            if wait_for > 0:
                await asyncio.sleep(wait_for)
            self._last_call = time.monotonic()


class OpenMeteoClient:
    """Open-Meteo 异步客户端。

    可作异步上下文管理器使用::

        async with OpenMeteoClient() as client:
            result = await client.weather_forecast(request)
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._hosts = self.settings.source_hosts()
        self._limiter = _RateLimiter(self.settings.http_min_interval_ms)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.settings.http_timeout_seconds),
            headers={"User-Agent": self.settings.http_user_agent, "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )
        self.api_calls = 0

    # ---------- 生命周期 ----------
    async def __aenter__(self) -> OpenMeteoClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------- 底层请求 ----------
    async def _get(self, host_key: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """带限流与退避重试的 GET，返回解析后的 JSON。"""
        host = self._hosts[host_key]
        url = f"{host}{path}"
        cleaned = {key: value for key, value in params.items() if value is not None}
        if self.settings.openmeteo_api_key:
            cleaned["apikey"] = self.settings.openmeteo_api_key

        attempts = self.settings.http_max_retries
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            await self._limiter.acquire()
            try:
                response = await self._client.get(url, params=cleaned)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = SourceUnavailableError(f"网络异常: {exc!r}")
                logger.warning("[%s] 网络异常（第 %d/%d 次）: %s", host_key, attempt, attempts, exc)
                await self._backoff(attempt, None)
                continue

            self.api_calls += 1

            if response.status_code in _RETRYABLE_STATUS:
                retry_after = self._parse_retry_after(response)
                reason = f"HTTP {response.status_code}"
                last_error = (
                    SourceRateLimitedError(reason)
                    if response.status_code == 429
                    else SourceUnavailableError(reason)
                )
                logger.warning(
                    "[%s] %s（第 %d/%d 次，退避 %.1fs）",
                    host_key,
                    reason,
                    attempt,
                    attempts,
                    retry_after or self._backoff_delay(attempt),
                )
                await self._backoff(attempt, retry_after)
                continue

            if response.status_code >= 400:
                raise SourceResponseError(
                    f"{host_key} 返回 HTTP {response.status_code}: {response.text[:300]}"
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceResponseError(f"{host_key} 返回非 JSON 响应: {exc}") from exc

            if isinstance(payload, dict) and payload.get("error"):
                raise SourceResponseError(
                    f"{host_key} 业务错误: {payload.get('reason', '未知原因')}"
                )
            return payload

        raise last_error or SourceUnavailableError(f"{host_key} 请求失败且无明确原因")

    def _backoff_delay(self, attempt: int) -> float:
        return self.settings.http_backoff_base_seconds ** attempt

    async def _backoff(self, attempt: int, retry_after: float | None) -> None:
        delay = retry_after if retry_after is not None else self._backoff_delay(attempt)
        await asyncio.sleep(min(delay, 30.0))

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    # ---------- 结果整形 ----------
    @staticmethod
    def _rows_from_section(
        payload: dict[str, Any],
        section_name: str,
        variables: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
        """把 Open-Meteo 的列式响应转成行式记录。"""
        section = payload.get(section_name)
        if not isinstance(section, dict):
            return [], ()
        times = section.get("time") or []
        available = tuple(var for var in variables if var in section)
        if not times or not available:
            return [], available
        length = len(times)
        columnar = {var: section.get(var) or [] for var in available}
        rows: list[dict[str, Any]] = []
        for index, stamp in enumerate(times):
            row: dict[str, Any] = {"time": stamp}
            for var in available:
                column = columnar[var]
                row[var] = column[index] if index < len(column) else None
            rows.append(row)
        return rows, available

    @staticmethod
    def _check_contract(
        available: tuple[str, ...], requested: tuple[str, ...], context: str
    ) -> None:
        """校验数据源是否按契约返回了变量。"""
        if not available:
            raise SourceResponseError(
                f"{context}: 数据源未返回任何请求变量，疑似接口契约变更 "
                f"(请求 {len(requested)} 个变量)"
            )
        missing = [var for var in requested if var not in available]
        if missing:
            logger.warning(
                "%s: 数据源缺失 %d 个变量（将继续采集其余变量）: %s",
                context,
                len(missing),
                ", ".join(missing[:8]) + (" ..." if len(missing) > 8 else ""),
            )

    # ========================================================
    # 业务接口
    # ========================================================
    async def geocode(self, name: str, *, count: int = 5, language: str = "zh") -> FetchResult:
        """地名检索，返回候选地点列表。"""
        payload = await self._get(
            "geocoding",
            "/v1/search",
            {"name": name, "count": count, "language": language, "format": "json"},
        )
        results = payload.get("results") or []
        rows: list[dict[str, Any]] = []
        for item in results:
            rows.append(
                {
                    "name": item.get("name"),
                    "latitude": item.get("latitude"),
                    "longitude": item.get("longitude"),
                    "elevation": item.get("elevation"),
                    "country": item.get("country"),
                    "country_code": item.get("country_code"),
                    "admin1": item.get("admin1"),
                    "timezone": item.get("timezone"),
                    "population": item.get("population"),
                    "feature_code": item.get("feature_code"),
                }
            )
        return FetchResult(
            rows=rows,
            variables=("name", "latitude", "longitude", "timezone"),
            meta={"query": name, "returned": len(rows)},
        )

    async def weather_forecast(self, request: FetchRequest) -> FetchResult:
        """逐小时天气预报与实况。"""
        payload = await self._get(
            "forecast",
            "/v1/forecast",
            {
                "latitude": request.latitude,
                "longitude": request.longitude,
                "hourly": ",".join(WEATHER_HOURLY_VARIABLES),
                "timezone": request.timezone,
                "past_days": self.settings.forecast_past_days,
                "forecast_days": self.settings.forecast_horizon_days,
                "wind_speed_unit": "kmh",
            },
        )
        rows, available = self._rows_from_section(payload, "hourly", WEATHER_HOURLY_VARIABLES)
        self._check_contract(available, WEATHER_HOURLY_VARIABLES, "weather_forecast")
        return self._envelope(payload, rows, available)

    async def weather_daily(self, request: FetchRequest) -> FetchResult:
        """逐日天气预报与实况汇总。"""
        payload = await self._get(
            "forecast",
            "/v1/forecast",
            {
                "latitude": request.latitude,
                "longitude": request.longitude,
                "daily": ",".join(WEATHER_DAILY_VARIABLES),
                "timezone": request.timezone,
                "past_days": self.settings.forecast_past_days,
                "forecast_days": self.settings.forecast_horizon_days,
                "wind_speed_unit": "kmh",
            },
        )
        rows, available = self._rows_from_section(payload, "daily", WEATHER_DAILY_VARIABLES)
        self._check_contract(available, WEATHER_DAILY_VARIABLES, "weather_daily")
        return self._envelope(payload, rows, available)

    async def weather_archive(
        self,
        request: FetchRequest,
        *,
        start_date: date,
        end_date: date,
    ) -> FetchResult:
        """历史再分析逐小时气象（ERA5）。

        自动把结束日期收窄到发布延迟之后，避免请求尚无数据的时段。
        """
        latest_available = date.today() - timedelta(days=ARCHIVE_PUBLICATION_LAG_DAYS)
        effective_end = min(end_date, latest_available)
        if effective_end < start_date:
            logger.info(
                "archive[%s]: 请求区间 %s~%s 全部落在发布延迟内，跳过",
                request.city_slug,
                start_date,
                end_date,
            )
            return FetchResult(rows=[], variables=ARCHIVE_HOURLY_VARIABLES, api_calls=0)

        payload = await self._get(
            "archive",
            "/v1/archive",
            {
                "latitude": request.latitude,
                "longitude": request.longitude,
                "start_date": start_date.isoformat(),
                "end_date": effective_end.isoformat(),
                "hourly": ",".join(ARCHIVE_HOURLY_VARIABLES),
                "timezone": request.timezone,
                "wind_speed_unit": "kmh",
            },
        )
        rows, available = self._rows_from_section(payload, "hourly", ARCHIVE_HOURLY_VARIABLES)
        self._check_contract(available, ARCHIVE_HOURLY_VARIABLES, "weather_archive")
        result = self._envelope(payload, rows, available)
        result.meta["effective_range"] = [start_date.isoformat(), effective_end.isoformat()]
        return result

    async def air_quality(self, request: FetchRequest) -> FetchResult:
        """逐小时空气质量（CAMS）。

        空气质量 API 支持最多 92 天历史，比预报接口的 ``past_days`` 宽松得多。
        这里按 ``air_quality_history_days`` 拉取历史，融合宽表因此能回溯数月，
        达标考核与污染过程分析才有多周期样本可用。
        """
        # 数据源上限 92 天，超出会被拒绝
        past_days = max(0, min(self.settings.air_quality_history_days, 92))
        payload = await self._get(
            "air_quality",
            "/v1/air-quality",
            {
                "latitude": request.latitude,
                "longitude": request.longitude,
                "hourly": ",".join(AIR_QUALITY_HOURLY_VARIABLES),
                "timezone": request.timezone,
                "past_days": past_days,
                "forecast_days": min(self.settings.forecast_horizon_days, 7),
            },
        )
        rows, available = self._rows_from_section(payload, "hourly", AIR_QUALITY_HOURLY_VARIABLES)
        self._check_contract(available, AIR_QUALITY_HOURLY_VARIABLES, "air_quality")
        return self._envelope(payload, rows, available)

    # ---------- 辅助 ----------
    @staticmethod
    def _envelope(
        payload: dict[str, Any],
        rows: list[dict[str, Any]],
        available: tuple[str, ...],
    ) -> FetchResult:
        units = payload.get("hourly_units") or payload.get("daily_units") or {}
        meta = {
            "latitude": payload.get("latitude"),
            "longitude": payload.get("longitude"),
            "elevation": payload.get("elevation"),
            "timezone": payload.get("timezone"),
            "timezone_abbreviation": payload.get("timezone_abbreviation"),
            "utc_offset_seconds": payload.get("utc_offset_seconds"),
            "generationtime_ms": payload.get("generationtime_ms"),
        }
        meta = {key: value for key, value in meta.items() if value is not None}
        return FetchResult(rows=rows, units=dict(units), variables=available, meta=meta)


def make_request(city: Any) -> FetchRequest:
    """从 City ORM 对象或字典构造取数请求。"""
    getter = city.get if isinstance(city, dict) else lambda key: getattr(city, key, None)
    return FetchRequest(
        city_slug=getter("slug"),
        latitude=float(getter("latitude")),
        longitude=float(getter("longitude")),
        timezone=getter("timezone") or "UTC",
    )


def local_now(timezone_name: str) -> datetime:
    """城市本地当前时间（朴素），用于判定实况 / 预报分界。"""
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(timezone_name)).replace(tzinfo=None, microsecond=0)
    except Exception:  # pragma: no cover - 非法时区回退到 UTC
        logger.warning("未知时区 %s，回退 UTC", timezone_name)
        return datetime.utcnow().replace(microsecond=0)


__all__ = ["OpenMeteoClient", "FetchRequest", "FetchResult", "make_request", "local_now"]
