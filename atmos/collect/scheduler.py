"""cron 表达式解析与常驻调度进程（``python -m atmos schedule``）。

**为什么不用 APScheduler / croniter。** 需要的能力只是标准 5 段 cron，自己实现
不到 200 行；而调度库会顺带引入它自己的时区模型、作业持久化与并发控制语义 ——
这三样恰恰是这个项目需要自己说清楚的：

* **时区**：一律按**服务器本地时间**计算（与 systemd timer、``crontab`` 一致）；
  数据侧的"城市本地时间"是口径问题，不参与调度；
* **作业状态**：不落盘。历史由 ``collection_runs`` 等运行记录承担，调度器本身
  重启后从"现在"重新算下一次，不做"补跑停机期间错过的任务"——
  补跑一小时前的天气采集没有业务意义，而补跑归档由每日作业天然覆盖；
* **串行**：采集打上游、评测整表读数据，两者并发只会互相拖慢，还会把
  Open-Meteo 的免费额度用在同一批数据上。

**cron 语义里的两个坑**（都有测试）：

1. **日与周之间是"或"不是"与"**：``0 0 1 * 1`` 是"每月 1 号**或**每周一"。
   两者都被限定时取并集，这是 Vixie cron 的行为，直觉上很容易写成交集；
2. **周日既是 0 也是 7**：两种写法都接受。Python 的 ``weekday()`` 以周一为 0，
   与 cron 不同，因此统一用 ``isoweekday() % 7`` 换算。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from atmos.logging_setup import get_logger

logger = get_logger("collect.scheduler")

#: 作业处理函数：同步、返回退出码（0 表示成功）。
JobHandler = Callable[[], int]


class CronError(ValueError):
    """cron 表达式非法。解析失败一律立即抛出，不要静默降级成"永不执行"。"""


# ==================================================================
# 表达式解析
# ==================================================================
def _parse_field(text: str, low: int, high: int, label: str) -> tuple[frozenset[int], bool]:
    """解析单个字段，返回 (取值集合, 是否恰好为 ``*``)。

    支持 ``*``、``a``、``a-b``、``a,b,c``、``*/n``、``a-b/n``。
    """
    text = text.strip()
    if not text:
        raise CronError(f"{label}字段为空")

    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"{label}字段含空项: {text!r}")

        step = 1
        if "/" in part:
            span, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                raise CronError(f"{label}字段步长非法: {part!r}")
            step = int(step_text)
            part = span.strip()

        if part == "*":
            start, end = low, high
        elif "-" in part:
            left, _, right = part.partition("-")
            if not (left.strip().isdigit() and right.strip().isdigit()):
                raise CronError(f"{label}字段区间非法: {part!r}")
            start, end = int(left), int(right)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError(f"{label}字段无法解析: {part!r}")

        if not (low <= start <= high and low <= end <= high):
            raise CronError(f"{label}字段超出范围 {low}-{high}: {part!r}")
        if start > end:
            raise CronError(f"{label}字段区间起点大于终点: {part!r}")
        values.update(range(start, end + 1, step))

    return frozenset(values), text == "*"


@dataclass(frozen=True)
class CronExpression:
    """一个已解析的 5 段 cron 表达式。"""

    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    days_wildcard: bool = False
    weekdays_wildcard: bool = False

    @classmethod
    def parse(cls, expression: str) -> CronExpression:
        parts = expression.split()
        if len(parts) != 5:
            raise CronError(
                f"cron 表达式需要 5 个字段（分 时 日 月 周），实际 {len(parts)} 个: {expression!r}"
            )
        minutes, _ = _parse_field(parts[0], 0, 59, "分")
        hours, _ = _parse_field(parts[1], 0, 23, "时")
        days, days_wildcard = _parse_field(parts[2], 1, 31, "日")
        months, _ = _parse_field(parts[3], 1, 12, "月")
        weekdays, weekdays_wildcard = _parse_field(parts[4], 0, 7, "周")
        return cls(
            source=" ".join(parts),
            minutes=minutes,
            hours=hours,
            days=days,
            months=months,
            # 7 与 0 都表示周日
            weekdays=frozenset(0 if value == 7 else value for value in weekdays),
            days_wildcard=days_wildcard,
            weekdays_wildcard=weekdays_wildcard,
        )

    # ---------- 匹配 ----------
    def _day_matches(self, moment: datetime) -> bool:
        weekday = moment.isoweekday() % 7  # isoweekday: 周一=1…周日=7 → 取模后周日=0
        day_ok = moment.day in self.days
        weekday_ok = weekday in self.weekdays

        if self.days_wildcard and self.weekdays_wildcard:
            return True
        if self.days_wildcard:
            return weekday_ok
        if self.weekdays_wildcard:
            return day_ok
        return day_ok or weekday_ok

    def matches(self, moment: datetime) -> bool:
        """该时刻（精确到分钟）是否命中。秒与微秒不参与判断。"""
        return (
            moment.minute in self.minutes
            and moment.hour in self.hours
            and moment.month in self.months
            and self._day_matches(moment)
        )

    # ---------- 推算 ----------
    def next_after(self, moment: datetime) -> datetime:
        """严格晚于 ``moment`` 的下一个命中时刻。

        先按天筛选、再在命中日里取最小的 (时, 分)，避免逐分钟扫描
        （``0 0 29 2 *`` 这类表达式逐分钟扫描要跑几十万次）。
        """
        start = (moment + timedelta(minutes=1)).replace(second=0, microsecond=0)
        cursor = start.replace(hour=0, minute=0)

        for _ in range(366 * 2):
            if cursor.month in self.months and self._day_matches(cursor):
                for hour in sorted(self.hours):
                    for minute in sorted(self.minutes):
                        candidate = cursor.replace(hour=hour, minute=minute)
                        if candidate >= start:
                            return candidate
            cursor += timedelta(days=1)

        raise CronError(f"{self.source} 在未来两年内没有可执行时间")  # pragma: no cover


# ==================================================================
# 作业与调度器
# ==================================================================
@dataclass
class ScheduledJob:
    """一个定时作业。"""

    name: str
    expression: CronExpression
    handler: JobHandler
    description: str = ""
    last_fired: datetime | None = field(default=None, repr=False)


class Scheduler:
    """把一组作业按 cron 触发，串行执行。

    ``clock`` 可注入，测试里用它把时间钉死，不睡真时钟。
    """

    def __init__(
        self,
        jobs: Sequence[ScheduledJob],
        *,
        clock: Callable[[], datetime] = datetime.now,
        started_at: datetime | None = None,
    ) -> None:
        self._jobs = list(jobs)
        self._clock = clock
        self._started_at = (started_at or clock()).replace(second=0, microsecond=0)

    @property
    def jobs(self) -> tuple[ScheduledJob, ...]:
        return tuple(self._jobs)

    def due(self, moment: datetime | None = None) -> list[ScheduledJob]:
        """返回"自上次执行以来已到点"的作业。

        判定方式是"下一个应执行时刻是否已过去"，而不是"当前分钟是否命中"。
        差别在于长作业：若一个作业跑了 5 分钟，期间到点的另一个作业不会
        因为"错过了那一分钟"被永久跳过 —— 这比精确到分钟更重要。
        """
        now = (moment or self._clock()).replace(second=0, microsecond=0)
        pending: list[ScheduledJob] = []
        for job in self._jobs:
            baseline = job.last_fired or self._started_at
            try:
                if job.expression.next_after(baseline) <= now:
                    pending.append(job)
            except CronError:  # pragma: no cover - 解析期已拦截
                logger.error("作业 %s 的 cron 无法推算下次时间，跳过", job.name)
        return pending

    def run_pending(self, moment: datetime | None = None) -> list[tuple[str, int]]:
        """执行到点的作业，返回 ``[(作业名, 退出码)]``。

        单个作业抛异常不能拖垮调度进程：捕获、记录、继续等下一次。
        """
        now = (moment or self._clock()).replace(second=0, microsecond=0)
        results: list[tuple[str, int]] = []
        for job in self.due(now):
            job.last_fired = now
            started = time.monotonic()
            logger.info("作业 %s 开始", job.name)
            try:
                code = job.handler()
            except Exception:
                logger.exception("作业 %s 抛出异常", job.name)
                code = 1
            logger.info("作业 %s 结束：退出码 %s，耗时 %.1fs", job.name, code, time.monotonic() - started)
            results.append((job.name, code))
        return results

    def describe(self) -> list[dict[str, str]]:
        """作业清单与各自的下次触发时间（供 ``--list`` 与启动日志使用）。"""
        rows: list[dict[str, str]] = []
        for job in self._jobs:
            try:
                upcoming = job.expression.next_after(self._clock()).strftime("%Y-%m-%d %H:%M")
            except CronError:  # pragma: no cover
                upcoming = "无法推算"
            rows.append(
                {
                    "作业": job.name,
                    "cron": job.expression.source,
                    "下次": upcoming,
                    "说明": job.description,
                }
            )
        return rows

    def run_forever(self, *, poll_seconds: float = 20.0) -> None:
        """常驻循环。``poll_seconds`` 只需小于 60 秒，不必与 cron 精度对齐。"""
        logger.info("调度启动，每 %.0f 秒检查一次（Ctrl+C 停止）", poll_seconds)
        for job in self._jobs:
            try:
                upcoming = job.expression.next_after(self._clock()).strftime("%m-%d %H:%M")
            except CronError:  # pragma: no cover
                upcoming = "无法推算"
            logger.info("  作业 %-8s cron=%-14s 下次 %s", job.name, job.expression.source, upcoming)

        while True:
            self.run_pending()
            time.sleep(poll_seconds)


# ==================================================================
# 默认作业：采集 / 评测 / 归档
# ==================================================================
def collect_handler(*, include_archive: bool = False) -> int:
    """增量采集。默认跳过归档：ERA5 有约 5 天发布延迟，每小时重拉 180 天窗口毫无意义。"""
    from atmos.collect.runner import Collector
    from atmos.models.enums import TriggerType

    report = asyncio.run(
        Collector().run(
            scope="all",
            trigger=TriggerType.SCHEDULED.value,
            include_archive=include_archive,
        )
    )
    payload = report.to_payload()
    logger.info("采集完成：状态=%s，写入 %s 行", payload["status"], payload["total_rows_written"])
    return 0 if payload["status"] == "success" else 1


def quality_handler() -> int:
    """五维质量评测（全部资产）。"""
    from atmos.db import session_scope
    from atmos.quality.engine import QualityEngine

    engine = QualityEngine()
    with session_scope() as session:
        reports = engine.evaluate_all(session)
    grades = "，".join(f"{item.asset_key}={item.grade}" for item in reports)
    logger.info("质量评测完成：%s", grades or "无资产")
    return 0


def prune_handler() -> int:
    """清理过期运行记录与导出文件（不碰 parquet）。"""
    from atmos.prune import prune

    report = prune(dry_run=False)
    logger.info(
        "保留策略执行：清理 %s 行元数据、%s 个导出文件（早于 %s）",
        report.total_rows,
        report.export_files,
        report.cutoff.isoformat(),
    )
    return 0


def build_default_jobs(settings) -> list[ScheduledJob]:  # type: ignore[no-untyped-def]
    """按配置装配默认作业。cron 为空的作业直接不登记（即停用）。"""
    from functools import partial

    specs = [
        (
            "collect",
            settings.schedule_collect_cron,
            partial(collect_handler, include_archive=False),
            "增量采集（跳过 ERA5 归档）",
        ),
        (
            "quality",
            settings.schedule_quality_cron,
            quality_handler,
            "五维质量评测",
        ),
        (
            "archive",
            settings.schedule_archive_cron,
            partial(collect_handler, include_archive=True),
            "完整采集（含 ERA5 归档回补）",
        ),
        (
            "prune",
            settings.schedule_prune_cron,
            prune_handler,
            "清理过期运行记录与导出文件（不含 parquet）",
        ),
    ]

    jobs: list[ScheduledJob] = []
    for name, expression, handler, description in specs:
        if not expression.strip():
            logger.info("作业 %s 已停用（cron 为空）", name)
            continue
        jobs.append(
            ScheduledJob(
                name=name,
                expression=CronExpression.parse(expression),
                handler=handler,
                description=description,
            )
        )
    return jobs


__all__ = [
    "CronError",
    "CronExpression",
    "ScheduledJob",
    "Scheduler",
    "build_default_jobs",
    "collect_handler",
    "quality_handler",
]
