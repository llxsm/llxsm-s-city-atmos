"""质量检查项实现集。

每一项检查都是纯函数：``CheckContext → CheckOutcome``，
通过 ``@register`` 注册到按 ``check_key`` 索引的调度表中。
规则参数全部来自 ``quality_rules.yaml``，代码不含魔法数字。

新增一项检查只需三步：
1. 在 ``quality_rules.yaml`` 的 ``checks`` 中声明（含 applies_to 与 params）；
2. 在本模块实现同名函数并 ``@register("<check_key>")``；
3. 若涉及新维度，在 ``dimensions`` 中登记权重。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from atmos.catalog.definitions import AssetDefinition, CheckDefinition
from atmos.logging_setup import get_logger
from atmos.models.enums import CheckStatus
from atmos.standards import aqi_level
from atmos.utils import clamp, safe_ratio

logger = get_logger("quality.checks")

# 平台自产列不参与数据质量判定
_AUDIT_COLUMNS = {"ingested_at", "source_run_id", "city_id"}

# DuckDB 物理类型 → 字段字典声明类型的等价集合
_TYPE_EQUIVALENTS: dict[str, set[str]] = {
    "double": {"DOUBLE", "FLOAT", "REAL", "DECIMAL"},
    "integer": {"BIGINT", "INTEGER", "INT", "SMALLINT", "HUGEINT", "UBIGINT"},
    "string": {"VARCHAR", "TEXT", "STRING"},
    "timestamp": {"TIMESTAMP", "TIMESTAMP WITH TIME ZONE", "TIMESTAMP_NS", "TIMESTAMP_MS"},
    "date": {"DATE"},
    "boolean": {"BOOLEAN", "BOOL"},
}

# Pass / Warn 分界
_PASS_SCORE = 90.0
_WARN_SCORE = 70.0


@dataclass
class CheckOutcome:
    """单项检查结果。"""

    status: str = CheckStatus.PASS.value
    score: float = 100.0
    observed: float | None = None
    expected: float | None = None
    records_checked: int = 0
    records_failed: int = 0
    message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": round(self.score, 2),
            "observed": self.observed,
            "expected": self.expected,
            "records_checked": self.records_checked,
            "records_failed": self.records_failed,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass
class CheckContext:
    """检查执行上下文。"""

    asset: AssetDefinition
    check: CheckDefinition
    frames: dict[str, pd.DataFrame]
    city_local_now: dict[str, datetime]
    window_hours: int
    defaults: dict[str, Any]
    lake_schema: dict[str, str] = field(default_factory=dict)
    run_stats: dict[str, int] = field(default_factory=dict)

    # ---------- 参数与便捷访问 ----------
    def param(self, name: str, fallback: Any = None) -> Any:
        return self.check.params.get(name, fallback)

    def default(self, name: str, fallback: Any) -> Any:
        return self.defaults.get(name, fallback)

    @property
    def time_column(self) -> str | None:
        return self.asset.time_column

    @property
    def granularity_hours(self) -> float:
        if self.asset.granularity == "daily":
            return 24.0
        return 1.0

    @property
    def is_daily(self) -> bool:
        return self.granularity_hours >= 24.0

    def boundary_now(self, city_slug: str) -> datetime | None:
        """评测窗口的本地时间基准。

        逐日资产的时间戳落在当日零点，而"现在"是当天任意时刻；
        若直接用小时精度比较，今天这一行会被算作"尚未开始"而排除，
        新鲜度也会被虚报为 21 小时延迟。因此日粒度资产统一对齐到当日零点。
        """
        local_now = self.city_local_now.get(city_slug)
        if local_now is None:
            return None
        if self.is_daily:
            return local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_now

    def window(self, city_slug: str) -> pd.DataFrame:
        """该城市在评测窗口内的数据（按城市本地时间裁窗）。"""
        return self._slice(city_slug, self.window_hours, lag_hours=0.0)

    def coverage_window(self, city_slug: str) -> pd.DataFrame:
        """覆盖度评测窗口：整体前移数据源的发布延迟。

        ERA5 归档等数据源存在数天的发布延迟，若直接以"当前时刻"为窗口右界，
        会把上游尚未发布的数据误判为平台缺失。
        """
        return self._slice(city_slug, self.window_hours, lag_hours=self.lag_hours)

    @property
    def lag_hours(self) -> float:
        """兼容旧参数名：本资产应扣除的发布延迟（小时）。"""
        return self.anchor_hours

    @property
    def anchor_hours(self) -> float:
        """覆盖率窗口相对"当前时刻"的锚点偏移（小时）。

        * ``0``    —— 默认，窗口右界为当前时刻；
        * ``< 0``  —— 数据源存在发布延迟（如 ERA5 滞后 5 天），窗口整体前移；
        * ``> 0``  —— 资产包含预报时段（如逐小时预报覆盖未来 7 天），窗口整体前移
          到"未来"，从而对齐该资产实际应当持有的时段。
        """
        table = self.check.params.get("anchor_hours_by_asset") or {}
        legacy = self.check.params.get("lag_hours_by_asset") or {}
        try:
            raw = table.get(self.asset.asset_key, legacy.get(self.asset.asset_key, 0.0))
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    @property
    def span_hours(self) -> float:
        """覆盖率窗口的长度；默认等于评测窗口。"""
        table = self.check.params.get("span_hours_by_asset") or {}
        try:
            return float(table.get(self.asset.asset_key, self.window_hours))
        except (TypeError, ValueError):
            return float(self.window_hours)

    def coverage_window(self, city_slug: str) -> pd.DataFrame:
        """覆盖度评测窗口：按资产的数据时段预期整体平移。"""
        return self._slice(
            city_slug, int(self.span_hours), lag_hours=-self.anchor_hours
        )

    def _slice(self, city_slug: str, window_hours: int, *, lag_hours: float) -> pd.DataFrame:
        """按城市本地时间切出 ``[now-lag-window, now-lag]`` 区间。

        ``lag_hours`` 为负时窗口前移（用于预报类资产）。
        若数据帧缺少时间列，则退回整帧 —— 单条检查不应因为列缺失而崩溃，
        列缺失本身由有效性维度负责报告。
        """
        frame = self.frames.get(city_slug)
        if frame is None or frame.empty or not self.time_column:
            return pd.DataFrame()
        if self.time_column not in frame.columns:
            return frame
        local_now = self.boundary_now(city_slug)
        if local_now is None:
            return frame
        stamps = pd.to_datetime(frame[self.time_column], errors="coerce")
        end = local_now - pd.Timedelta(hours=lag_hours)
        # 右界放宽 1 小时以吸收整点取整误差
        start = end - pd.Timedelta(hours=window_hours)
        return frame.loc[(stamps >= start) & (stamps <= end + pd.Timedelta(hours=1))]

    def windowed_frames(self) -> dict[str, pd.DataFrame]:
        return {slug: self.window(slug) for slug in self.frames}

    def non_nullable_columns(self) -> list[str]:
        return [
            column.name
            for column in self.asset.columns
            if not column.is_nullable and column.name not in _AUDIT_COLUMNS
        ]

    def has_columns(self, *names: str) -> bool:
        if not self.frames:
            return False
        sample = next(iter(self.frames.values()))
        return all(name in sample.columns for name in names)


# ==================================================================
# 注册表
# ==================================================================
Evaluator = Callable[[CheckContext], CheckOutcome]
_REGISTRY: dict[str, Evaluator] = {}


def register(key: str) -> Callable[[Evaluator], Evaluator]:
    """把检查实现注册到调度表。"""

    def decorator(func: Evaluator) -> Evaluator:
        if key in _REGISTRY:
            raise RuntimeError(f"检查项 {key} 重复注册")
        _REGISTRY[key] = func
        return func

    return decorator


def evaluator_for(key: str) -> Evaluator | None:
    return _REGISTRY.get(key)


def registered_keys() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


# ==================================================================
# 通用工具
# ==================================================================
def _status_for(score: float) -> str:
    if score >= _PASS_SCORE:
        return CheckStatus.PASS.value
    if score >= _WARN_SCORE:
        return CheckStatus.WARN.value
    return CheckStatus.FAIL.value


def _score_from_bad_rate(rate: float, ctx: CheckContext) -> float:
    """把"违规比例"转为 0-100 分：违规率越高分越低。"""
    warn = float(ctx.default("inconsistency_warn", 0.02))
    fail = float(ctx.default("inconsistency_fail", 0.10))
    from atmos.utils import linear_score

    return linear_score(1.0 - rate, warn=1.0 - warn, fail=1.0 - fail)


def _skip(message: str, detail: dict[str, Any] | None = None) -> CheckOutcome:
    return CheckOutcome(
        status=CheckStatus.SKIPPED.value,
        score=0.0,
        message=message,
        detail=detail or {},
    )


def _total_rows(frames: dict[str, pd.DataFrame]) -> int:
    return int(sum(len(frame) for frame in frames.values()))


# ==================================================================
# 完整性
# ==================================================================
@register("completeness.time_coverage")
def time_coverage(ctx: CheckContext) -> CheckOutcome:
    """实际记录数 / 应有记录数。"""
    if not ctx.time_column:
        return _skip("资产未声明时间列，无法评估时间覆盖")

    expected_per_city = max(1.0, ctx.span_hours / ctx.granularity_hours)
    ratios: dict[str, float] = {}
    expected_total = 0
    actual_total = 0

    for city_slug in ctx.frames:
        window = ctx.coverage_window(city_slug)
        if window.empty:
            ratios[city_slug] = 0.0
            expected_total += int(expected_per_city)
            continue
        distinct = int(pd.to_datetime(window[ctx.time_column], errors="coerce").nunique())
        # 同一时刻的重复由唯一性维度负责，此处只关心"有没有"
        actual = min(distinct, expected_per_city)
        ratios[city_slug] = min(1.0, actual / expected_per_city)
        expected_total += int(expected_per_city)
        actual_total += int(actual)

    average_ratio = safe_ratio(sum(ratios.values()), len(ratios), default=0.0) if ratios else 0.0
    score = clamp(average_ratio * 100.0)
    weakest = sorted(ratios.items(), key=lambda item: item[1])[:5]
    anchor = ctx.anchor_hours
    span = ctx.span_hours

    anchor_note = ""
    if anchor < 0:
        anchor_note = f"，已扣除数据源发布延迟 {abs(anchor):g}h"
    elif anchor > 0:
        anchor_note = f"，窗口已前移至预报时段（+{anchor:g}h）"

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round(average_ratio * 100, 2),
        expected=100.0,
        records_checked=expected_total,
        records_failed=max(0, expected_total - actual_total),
        message=(
            f"{span:g}h 窗口内平均时间覆盖 {average_ratio * 100:.1f}%"
            f"（{len(ctx.frames)} 个城市{anchor_note}）"
        ),
        detail={
            "coverage_by_city": {key: round(value * 100, 2) for key, value in sorted(ratios.items())},
            "weakest_cities": [{"city": key, "coverage": round(value * 100, 2)} for key, value in weakest],
            "anchor_hours_applied": anchor,
            "span_hours_applied": span,
        },
    )


@register("completeness.null_rate")
def null_rate(ctx: CheckContext) -> CheckOutcome:
    """关键字段（字段字典中声明 non-nullable）的空值率。

    评分口径：**逐列**计算非空率并映射为分数，再对所有必需字段取平均。
    这样"某个必需字段整列为空"会显著拉低得分，而不是被大量健康字段稀释掉 ——
    这正是数据契约最需要捕获的失败模式。
    """
    critical = ctx.non_nullable_columns()
    if not critical:
        return _skip("资产未声明非空字段")

    warn = float(ctx.default("null_rate_warn", 0.05))
    fail = float(ctx.default("null_rate_fail", 0.20))
    from atmos.utils import linear_score

    column_scores: dict[str, float] = {}
    column_rates: dict[str, float] = {}
    null_counts: dict[str, int] = {}
    checked = 0
    failed = 0

    for name in critical:
        field_total = 0
        field_nulls = 0
        for city_slug in ctx.frames:
            window = ctx.window(city_slug)
            if window.empty or name not in window.columns:
                continue
            field_total += len(window)
            field_nulls += int(window[name].isna().sum())
        if field_total == 0:
            continue
        rate = field_nulls / field_total
        column_rates[name] = rate
        null_counts[name] = field_nulls
        column_scores[name] = linear_score(1.0 - rate, warn=1.0 - warn, fail=1.0 - fail)
        checked += field_total
        failed += field_nulls

    if not column_scores:
        return _skip("窗口内无数据，无法评估空值率")

    score = sum(column_scores.values()) / len(column_scores)
    healthy = sum(1 for value in column_scores.values() if value >= 100.0)
    worst = sorted(column_rates.items(), key=lambda item: -item[1])[:5]

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round(healthy / len(column_scores) * 100, 2),
        expected=100.0,
        records_checked=checked,
        records_failed=failed,
        message=(
            f"{healthy}/{len(column_scores)} 个必需字段无缺失值"
            + (f"，最差字段空值率 {worst[0][1] * 100:.1f}%" if worst and worst[0][1] > 0 else "")
        ),
        detail={
            "critical_columns": critical,
            "columns_without_nulls": healthy,
            "null_counts": {key: null_counts[key] for key, _ in worst},
            "per_column_null_rate": {key: round(value * 100, 3) for key, value in worst},
            "per_column_score": {
                key: round(column_scores[key], 2) for key, _ in worst
            },
        },
    )


@register("completeness.obs_hours")
def obs_hours(ctx: CheckContext) -> CheckOutcome:
    """日汇总资产的有效观测小时数。"""
    if not ctx.has_columns("obs_hours"):
        return _skip("资产不含 obs_hours 字段")

    ideal = float(ctx.param("ideal_hours", 24.0))
    minimum = float(ctx.param("min_acceptable_hours", 20.0))
    samples: list[float] = []
    for city_slug in ctx.frames:
        window = ctx.window(city_slug)
        if window.empty:
            continue
        values = pd.to_numeric(window["obs_hours"], errors="coerce").dropna()
        samples.extend(values.tolist())

    if not samples:
        return _skip("窗口内无日汇总样本")

    average = sum(samples) / len(samples)
    ratio = min(1.0, average / ideal)
    from atmos.utils import linear_score

    score = linear_score(ratio, warn=1.0, fail=minimum / ideal)
    below = sum(1 for value in samples if value < minimum)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round(average, 2),
        expected=ideal,
        records_checked=len(samples),
        records_failed=below,
        message=f"平均有效观测 {average:.1f} 小时（样本 {len(samples)} 天，低于 {minimum:.0f} 小时 {below} 天）",
        detail={"min_acceptable_hours": minimum, "below_threshold_days": below},
    )


# ==================================================================
# 时效性
# ==================================================================
@register("timeliness.freshness")
def freshness(ctx: CheckContext) -> CheckOutcome:
    """数据新鲜度对标 SLA。"""
    if not ctx.time_column:
        return _skip("资产未声明时间列")

    sla = ctx.asset.sla_freshness_minutes
    if not sla:
        return _skip("资产未声明 SLA 新鲜度要求")

    zero_factor = float(ctx.param("zero_score_factor", 3.0))
    delays: dict[str, float] = {}
    scores: list[float] = []

    for city_slug, frame in ctx.frames.items():
        if frame.empty:
            delays[city_slug] = float("inf")
            scores.append(0.0)
            continue
        stamps = pd.to_datetime(frame[ctx.time_column], errors="coerce").dropna()
        if stamps.empty:
            delays[city_slug] = float("inf")
            scores.append(0.0)
            continue
        local_now = ctx.boundary_now(city_slug)
        if local_now is None:
            continue
        # 预报类资产包含未来时段，新鲜度只看"本应已到达"的部分，
        # 否则会出现"延迟 -8757 分钟"这类无意义的负值。
        arrived = stamps[stamps <= local_now + pd.Timedelta(hours=1)]
        if arrived.empty:
            # 全部是尚未到来的预报时段，视为零延迟
            delays[city_slug] = 0.0
            scores.append(100.0)
            continue
        latest = arrived.max().to_pydatetime()
        delay = (local_now - latest).total_seconds() / 60.0
        delays[city_slug] = delay
        if delay <= sla:
            scores.append(100.0)
        else:
            denominator = max(1.0, (zero_factor - 1.0) * sla)
            scores.append(clamp(100.0 * (1.0 - (delay - sla) / denominator)))

    if not scores:
        return _skip("无法计算新鲜度（缺少城市本地时间基准）")

    score = sum(scores) / len(scores)
    finite_delays = [value for value in delays.values() if value != float("inf")]
    worst_delay = max(finite_delays) if finite_delays else None

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round(worst_delay, 1) if worst_delay is not None else None,
        expected=float(sla),
        records_checked=len(delays),
        records_failed=sum(1 for value in delays.values() if value > sla),
        message=(
            f"最大延迟 {worst_delay:.0f} 分钟 / SLA {sla} 分钟"
            if worst_delay is not None
            else "窗口内无数据，全部城市判定为超期"
        ),
        detail={
            "sla_minutes": sla,
            "delay_by_city": {
                key: (None if value == float("inf") else round(value, 1))
                for key, value in sorted(delays.items())
            },
        },
    )


@register("timeliness.run_success_rate")
def run_success_rate(ctx: CheckContext) -> CheckOutcome:
    """近窗口内采集运行成功率。"""
    total = int(ctx.run_stats.get("total", 0))
    if total == 0:
        return _skip("窗口内无采集运行记录", {"total": 0})

    success = int(ctx.run_stats.get("success", 0))
    partial = int(ctx.run_stats.get("partial", 0))
    failed = int(ctx.run_stats.get("failed", 0))
    # 部分成功按半分计入
    effective = success + partial * 0.5
    score = clamp(effective / total * 100.0)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round(score, 2),
        expected=100.0,
        records_checked=total,
        records_failed=failed,
        message=f"近 {ctx.window_hours}h 共 {total} 次运行，成功 {success}、部分 {partial}、失败 {failed}",
        detail={"total": total, "success": success, "partial": partial, "failed": failed},
    )


# ==================================================================
# 有效性
# ==================================================================
@register("validity.range")
def validity_range(ctx: CheckContext) -> CheckOutcome:
    """按字段字典声明的值域检测越界。"""
    ranged = [column for column in ctx.asset.columns if column.value_range]
    if not ranged:
        return _skip("字段字典未声明任何值域约束")

    checked = 0
    outliers = 0
    per_column: dict[str, dict[str, Any]] = {}

    for city_slug in ctx.frames:
        window = ctx.window(city_slug)
        if window.empty:
            continue
        for column in ranged:
            if column.name not in window.columns:
                continue
            values = pd.to_numeric(window[column.name], errors="coerce").dropna()
            if values.empty:
                continue
            low, high = column.value_range or (None, None)
            mask = (values < low) | (values > high)
            bad = int(mask.sum())
            checked += len(values)
            outliers += bad
            if bad:
                entry = per_column.setdefault(
                    column.name,
                    {"count": 0, "low": low, "high": high, "samples": []},
                )
                entry["count"] += bad
                if len(entry["samples"]) < 3:
                    entry["samples"].extend(
                        [round(float(item), 3) for item in values[mask].head(3).tolist()]
                    )

    if checked == 0:
        return _skip("窗口内无可用数值，无法评估值域")

    rate = outliers / checked
    warn = float(ctx.default("outlier_warn", 0.01))
    fail = float(ctx.default("outlier_fail", 0.05))
    from atmos.utils import linear_score

    score = linear_score(1.0 - rate, warn=1.0 - warn, fail=1.0 - fail)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round((1.0 - rate) * 100, 3),
        expected=100.0,
        records_checked=checked,
        records_failed=outliers,
        message=(
            f"{len(ranged)} 个受限字段整体合规率 {(1 - rate) * 100:.3f}%（越界 {outliers} 个值）"
            if outliers
            else f"{len(ranged)} 个受限字段全部落在值域内"
        ),
        detail={
            "constrained_columns": len(ranged),
            "violations": {
                key: {"count": value["count"], "range": [value["low"], value["high"]], "samples": value["samples"]}
                for key, value in sorted(per_column.items(), key=lambda item: -item[1]["count"])[:8]
            },
        },
    )


@register("validity.enum")
def validity_enum(ctx: CheckContext) -> CheckOutcome:
    """按字段字典声明的枚举集合检测非法离散值。"""
    enum_columns = [column for column in ctx.asset.columns if column.enum_values]
    if not enum_columns:
        return _skip("字段字典未声明任何枚举约束")

    checked = 0
    invalid = 0
    per_column: dict[str, dict[str, Any]] = {}

    for city_slug in ctx.frames:
        window = ctx.window(city_slug)
        if window.empty:
            continue
        for column in enum_columns:
            if column.name not in window.columns:
                continue
            series = window[column.name].dropna()
            if series.empty:
                continue
            allowed = set(column.enum_values or ())
            # 数值枚举统一按 float 比较，规避 int/float 差异
            try:
                normalized = {float(item) for item in allowed}
                mask = ~series.map(lambda value: float(value) in normalized)
            except (TypeError, ValueError):
                mask = ~series.isin(allowed)
            bad = int(mask.sum())
            checked += len(series)
            invalid += bad
            if bad:
                entry = per_column.setdefault(column.name, {"count": 0, "samples": []})
                entry["count"] += bad
                if len(entry["samples"]) < 5:
                    entry["samples"].extend(
                        [str(item) for item in series[mask].unique().tolist()[:5]]
                    )

    if checked == 0:
        return _skip("窗口内无可用离散值")

    rate = invalid / checked
    warn = float(ctx.default("outlier_warn", 0.01))
    fail = float(ctx.default("outlier_fail", 0.05))
    from atmos.utils import linear_score

    score = linear_score(1.0 - rate, warn=1.0 - warn, fail=1.0 - fail)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round((1.0 - rate) * 100, 3),
        expected=100.0,
        records_checked=checked,
        records_failed=invalid,
        message=(
            f"{len(enum_columns)} 个枚举字段合法率 {(1 - rate) * 100:.3f}%（非法 {invalid} 个值）"
            if invalid
            else f"{len(enum_columns)} 个枚举字段取值全部合法"
        ),
        detail={"violations": per_column},
    )


@register("validity.type_conformance")
def type_conformance(ctx: CheckContext) -> CheckOutcome:
    """落湖物理类型与字段字典声明是否一致（上游改版的早期预警）。"""
    if not ctx.lake_schema:
        return _skip("尚未落湖，无物理类型可校验")

    mismatches: list[dict[str, Any]] = []
    matched = 0
    for column in ctx.asset.columns:
        actual = ctx.lake_schema.get(column.name)
        if actual is None:
            mismatches.append({"column": column.name, "declared": column.data_type, "actual": None})
            continue
        equivalents = _TYPE_EQUIVALENTS.get(column.data_type, set())
        if actual.upper() in equivalents:
            matched += 1
        else:
            mismatches.append(
                {"column": column.name, "declared": column.data_type, "actual": actual}
            )

    total = len(ctx.asset.columns)
    score = clamp(safe_ratio(matched, total) * 100.0)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round(score, 2),
        expected=100.0,
        records_checked=total,
        records_failed=len(mismatches),
        message=(
            f"{matched}/{total} 个字段物理类型与字段字典一致"
            + (f"，{len(mismatches)} 个不一致" if mismatches else "")
        ),
        detail={"mismatches": mismatches[:20]},
    )


# ==================================================================
# 一致性
# ==================================================================
def _pairwise_violation(
    ctx: CheckContext,
    left: str,
    right: str,
    predicate: Callable[[pd.Series, pd.Series], pd.Series],
    *,
    message: str,
) -> CheckOutcome:
    """通用二元一致性校验：统计不满足 predicate 的比例。"""
    if not ctx.has_columns(left, right):
        return _skip(f"缺少字段 {left} 或 {right}")

    checked = 0
    violations = 0
    samples: list[dict[str, Any]] = []

    for city_slug in ctx.frames:
        window = ctx.window(city_slug)
        if window.empty:
            continue
        left_series = pd.to_numeric(window[left], errors="coerce")
        right_series = pd.to_numeric(window[right], errors="coerce")
        valid = left_series.notna() & right_series.notna()
        if not valid.any():
            continue
        ok = predicate(left_series[valid], right_series[valid])
        bad = int((~ok).sum())
        checked += int(valid.sum())
        violations += bad
        if bad and len(samples) < 5:
            offenders = window.loc[valid][~ok].head(5 - len(samples))
            for _, row in offenders.iterrows():
                samples.append(
                    {
                        "city": city_slug,
                        "time": str(row.get(ctx.time_column or "time")),
                        left: row.get(left),
                        right: row.get(right),
                    }
                )

    if checked == 0:
        return _skip(f"{left} 与 {right} 均无有效配对值")

    rate = violations / checked
    score = _score_from_bad_rate(rate, ctx)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round((1.0 - rate) * 100, 3),
        expected=100.0,
        records_checked=checked,
        records_failed=violations,
        message=f"{message}：{checked - violations}/{checked} 条满足约束（违反率 {rate * 100:.3f}%）",
        detail={"samples": samples, "violation_rate": round(rate, 6)},
    )


@register("consistency.pm25_le_pm10")
def pm25_le_pm10(ctx: CheckContext) -> CheckOutcome:
    tolerance = float(ctx.param("tolerance_ratio", 1.05))
    return _pairwise_violation(
        ctx,
        "pm2_5",
        "pm10",
        lambda left, right: left <= right * tolerance,
        message=f"PM2.5 应不超过 PM10 的 {tolerance:g} 倍",
    )


@register("consistency.rain_le_precipitation")
def rain_le_precipitation(ctx: CheckContext) -> CheckOutcome:
    tolerance = float(ctx.param("tolerance_mm", 0.1))
    return _pairwise_violation(
        ctx,
        "rain",
        "precipitation",
        lambda left, right: left <= right + tolerance,
        message=f"降雨量应不超过总降水量（容差 {tolerance:g} mm）",
    )


@register("consistency.apparent_temp_deviation")
def apparent_temp_deviation(ctx: CheckContext) -> CheckOutcome:
    max_deviation = float(ctx.param("max_deviation_c", 25.0))
    return _pairwise_violation(
        ctx,
        "apparent_temperature",
        "temperature_2m",
        lambda left, right: (left - right).abs() <= max_deviation,
        message=f"体感温度与气温偏差应不超过 {max_deviation:g}°C",
    )


@register("consistency.daily_extremes")
def daily_extremes(ctx: CheckContext) -> CheckOutcome:
    """日极值序关系：min 不得大于 max。

    不同资产对日极值的命名不同（原始层 ``temperature_2m_min/max``，
    服务层 ``temp_min/max``），此处逐一尝试，命中即校验。
    """
    candidate_pairs = (
        ("temperature_2m_min", "temperature_2m_max"),
        ("apparent_temperature_min", "apparent_temperature_max"),
        ("temp_min", "temp_max"),
    )
    available = [(low, high) for low, high in candidate_pairs if ctx.has_columns(low, high)]
    if not available:
        return _skip("资产不含可校验的日极值字段对")

    checked = 0
    violations = 0
    details: dict[str, int] = {}

    for city_slug in ctx.frames:
        window = ctx.window(city_slug)
        if window.empty:
            continue
        for low_name, high_name in available:
            low = pd.to_numeric(window[low_name], errors="coerce")
            high = pd.to_numeric(window[high_name], errors="coerce")
            valid = low.notna() & high.notna()
            if not valid.any():
                continue
            bad = int((low[valid] > high[valid]).sum())
            checked += int(valid.sum())
            violations += bad
            if bad:
                details[f"{low_name}>{high_name}"] = details.get(
                    f"{low_name}>{high_name}", 0
                ) + bad

    if checked == 0:
        return _skip("日极值字段均无有效值")

    rate = violations / checked
    score = _score_from_bad_rate(rate, ctx)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round((1.0 - rate) * 100, 3),
        expected=100.0,
        records_checked=checked,
        records_failed=violations,
        message=f"日极值序关系：{checked - violations}/{checked} 条满足 min ≤ max",
        detail={"violations": details},
    )


@register("consistency.aqi_matches_level")
def aqi_matches_level(ctx: CheckContext) -> CheckOutcome:
    """派生等级必须与 AQI 数值落在同一区间。

    不同资产的命名不同：逐小时资产用 ``european_aqi`` + ``aqi_level``，
    日汇总资产用 ``aqi_max`` + ``dominant_aqi_level``，此处逐一匹配。
    """
    level_column = next(
        (name for name in ("aqi_level", "dominant_aqi_level") if ctx.has_columns(name)), None
    )
    aqi_column = next(
        (name for name in ("european_aqi", "aqi_max", "aqi_avg") if ctx.has_columns(name)), None
    )
    if level_column is None or aqi_column is None:
        return _skip("资产缺少 AQI 数值列或等级列（aqi_level / dominant_aqi_level）")

    checked = 0
    violations = 0
    samples: list[dict[str, Any]] = []

    for city_slug in ctx.frames:
        window = ctx.window(city_slug)
        if window.empty:
            continue
        aqi = pd.to_numeric(window[aqi_column], errors="coerce")
        levels = window[level_column]
        valid = aqi.notna() & levels.notna()
        if not valid.any():
            continue
        expected_levels = aqi[valid].map(aqi_level)
        mismatch = expected_levels.astype("string") != levels[valid].astype("string")
        bad = int(mismatch.sum())
        checked += int(valid.sum())
        violations += bad
        if bad and len(samples) < 5:
            offenders = window.loc[valid][mismatch].head(5 - len(samples))
            for _, row in offenders.iterrows():
                samples.append(
                    {
                        "city": city_slug,
                        "aqi": row.get(aqi_column),
                        "derived": row.get(level_column),
                        "expected": aqi_level(row.get(aqi_column)),
                    }
                )

    if checked == 0:
        return _skip("AQI 与等级字段均无有效配对值")

    rate = violations / checked
    score = _score_from_bad_rate(rate, ctx)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round((1.0 - rate) * 100, 3),
        expected=100.0,
        records_checked=checked,
        records_failed=violations,
        message=(
            f"{level_column} 与 {aqi_column} 一致率 {(1 - rate) * 100:.3f}%"
            f"（不一致 {violations} 条）"
        ),
        detail={"samples": samples, "level_column": level_column, "aqi_column": aqi_column},
    )


# ==================================================================
# 唯一性
# ==================================================================
@register("uniqueness.primary_key")
def primary_key_uniqueness(ctx: CheckContext) -> CheckOutcome:
    """主键重复率。"""
    if not ctx.asset.primary_key:
        return _skip("资产未声明主键")

    keys = [name for name in ctx.asset.primary_key if name != "city_id"]
    if not keys:
        return _skip("主键仅含城市标识，无重复可能")

    checked = 0
    duplicates = 0
    per_city: dict[str, int] = {}

    for city_slug in ctx.frames:
        window = ctx.window(city_slug)
        if window.empty:
            continue
        present = [name for name in keys if name in window.columns]
        if not present:
            continue
        total = len(window)
        unique = int(window.drop_duplicates(subset=present).shape[0])
        bad = total - unique
        checked += total
        duplicates += bad
        if bad:
            per_city[city_slug] = bad

    if checked == 0:
        return _skip("窗口内无数据")

    rate = duplicates / checked
    warn = float(ctx.default("duplicate_warn", 0.001))
    fail = float(ctx.default("duplicate_fail", 0.01))
    from atmos.utils import linear_score

    score = linear_score(1.0 - rate, warn=1.0 - warn, fail=1.0 - fail)

    return CheckOutcome(
        status=_status_for(score),
        score=score,
        observed=round((1.0 - rate) * 100, 4),
        expected=100.0,
        records_checked=checked,
        records_failed=duplicates,
        message=(
            f"主键 {list(ctx.asset.primary_key)} 唯一率 {(1 - rate) * 100:.4f}%（重复 {duplicates} 条）"
            if duplicates
            else f"主键 {list(ctx.asset.primary_key)} 完全唯一"
        ),
        detail={"duplicates_by_city": per_city, "key_columns": keys},
    )


__all__ = [
    "CheckContext",
    "CheckOutcome",
    "register",
    "evaluator_for",
    "registered_keys",
]
