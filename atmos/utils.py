"""通用工具函数。

集中放置跨层复用的纯函数，避免各处重复实现口径不一致。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypeVar

T = TypeVar("T")

_HOUR = timedelta(hours=1)


# ============================================================
# 时间
# ============================================================
def utcnow() -> datetime:
    """当前 UTC 时间（朴素，秒级截断）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def iso_z(value: datetime | date | None) -> str | None:
    """序列化为带 ``Z`` 的 ISO 8601 字符串；``date`` 保持日期形式。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        stamp = value.replace(tzinfo=None, microsecond=0)
        return stamp.isoformat() + "Z"
    return value.isoformat()


def floor_hour(moment: datetime) -> datetime:
    """截断到整点。"""
    return moment.replace(minute=0, second=0, microsecond=0)


def floor_day(moment: datetime) -> datetime:
    """截断到当日零点。"""
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def hours_between(start: datetime, end: datetime) -> int:
    """两个时间之间的小时数（向上取整，至少为 0）。"""
    delta = (end - start).total_seconds()
    return max(0, int(delta // 3600))


def date_range(start: date, end: date) -> Iterator[date]:
    """闭区间日期迭代。"""
    cursor = start
    while cursor <= end:
        yield cursor
        cursor += timedelta(days=1)


def parse_datetime(value: str | datetime | None) -> datetime | None:
    """宽松解析 ISO 8601 字符串为朴素 datetime。"""
    if value is None or isinstance(value, datetime):
        return value
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def parse_date(value: str | date | None) -> date | None:
    """宽松解析 ISO 8601 日期字符串。"""
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


# ============================================================
# 标识与哈希
# ============================================================
def new_run_id(prefix: str = "run") -> str:
    """生成采集批次 ID，形如 ``run-20260917T094500-ab12cd``。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


def slugify(text: str) -> str:
    """把任意文本规范为 URL / 路径安全的标识。"""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_text).strip("-").lower()
    return slug or "unnamed"


def sha256_of(value: Any) -> str:
    """对任意可 JSON 序列化对象求稳定 SHA-256（用于 schema 指纹）。"""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def short_hash(value: Any, length: int = 16) -> str:
    """短哈希，便于在 UI 展示。"""
    return sha256_of(value)[:length]


# ============================================================
# 集合
# ============================================================
def chunked(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """把序列按固定大小切块。"""
    if size < 1:
        raise ValueError("size 必须为正整数")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def unique_preserving_order(items: Iterable[T]) -> list[T]:
    """去重但保持首次出现顺序。"""
    seen: set[T] = set()
    result: list[T] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """递归合并字典，``override`` 优先。"""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


# ============================================================
# 数值与评分
# ============================================================
def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    """把数值限制在闭区间内。"""
    return max(low, min(high, value))


def safe_ratio(numerator: float, denominator: float, *, default: float = 0.0) -> float:
    """安全除法，分母为 0 或非法时返回默认值。"""
    if not denominator or denominator <= 0:
        return default
    try:
        result = numerator / denominator
    except (TypeError, ZeroDivisionError):
        return default
    if result != result:  # NaN
        return default
    return result


def round_score(value: float, digits: int = 2) -> float:
    """统一评分保留位数。"""
    return round(clamp(value), digits)


def linear_score(observed: float, *, warn: float, fail: float, invert: bool = False) -> float:
    """把观测值线性映射到 0-100 分。

    默认语义：``observed >= warn`` 得满分，``observed <= fail`` 得 0 分，
    中间线性插值。``invert=True`` 时语义反转（越小越好）。
    """
    if invert:
        observed, warn, fail = -observed, -warn, -fail
    if observed >= warn:
        return 100.0
    if observed <= fail:
        return 0.0
    span = warn - fail
    if span <= 0:
        return 100.0
    return round_score((observed - fail) / span * 100.0)


def describe_number(value: float | int | None, digits: int = 2) -> str:
    """人类可读的数值描述。"""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}"


def relative_time(moment: datetime | None, *, now: datetime | None = None) -> str:
    """相对时间描述（中文）。"""
    if moment is None:
        return "从未"
    reference = now or utcnow()
    seconds = (reference - moment.replace(tzinfo=None)).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)
    if seconds < 90:
        return "即将" if future else "刚刚"
    for unit, size in (("天", 86400), ("小时", 3600), ("分钟", 60)):
        if seconds >= size:
            count = int(seconds // size)
            return f"{count} {unit}{'后' if future else '前'}"
    return "刚刚"
