"""cron 解析与调度器。

调度出错的两种典型形态都不是崩溃，而是**悄悄不干活**：表达式写错了却被当成
"永不执行"、或者某个作业被同一分钟触发两次把上游额度刷掉。因此这里重点覆盖
边界取值、cron 的"日或周"语义、以及"同一分钟只执行一次"。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from atmos.collect.scheduler import (
    CronError,
    CronExpression,
    ScheduledJob,
    Scheduler,
    build_default_jobs,
)


def _job(name: str, cron: str, handler) -> ScheduledJob:
    return ScheduledJob(name=name, expression=CronExpression.parse(cron), handler=handler)


# ==================================================================
# 解析
# ==================================================================
def test_parses_every_minute() -> None:
    expr = CronExpression.parse("* * * * *")
    assert len(expr.minutes) == 60
    assert len(expr.hours) == 24
    assert expr.days_wildcard and expr.weekdays_wildcard


def test_parses_step_range_and_list() -> None:
    expr = CronExpression.parse("*/15 9-11,22 * * *")
    assert sorted(expr.minutes) == [0, 15, 30, 45]
    assert sorted(expr.hours) == [9, 10, 11, 22]


def test_parses_range_with_step() -> None:
    expr = CronExpression.parse("0 0-6/3 * * *")
    assert sorted(expr.hours) == [0, 3, 6]


@pytest.mark.parametrize(
    "expression",
    [
        "* * * *",            # 字段不足
        "* * * * * *",        # 字段过多
        "60 * * * *",         # 分越界
        "* 24 * * *",         # 时越界
        "* * 0 * *",          # 日从 1 开始
        "* * 32 * *",
        "* * * 13 *",
        "* * * * 8",
        "@hourly",            # 不支持别名
        "*/0 * * * *",        # 步长为 0
        "5-1 * * * *",        # 区间倒置
        "abc * * * *",
        "1,,2 * * * *",
    ],
)
def test_rejects_invalid_expressions(expression: str) -> None:
    """非法表达式必须立刻抛错，不能降级成"永不执行"—— 那样任务静默消失。"""
    with pytest.raises(CronError):
        CronExpression.parse(expression)


def test_error_message_names_the_field() -> None:
    with pytest.raises(CronError) as excinfo:
        CronExpression.parse("60 * * * *")
    assert "分" in str(excinfo.value)


# ==================================================================
# 匹配语义
# ==================================================================
def test_sunday_is_both_zero_and_seven() -> None:
    """cron 里 0 与 7 都表示周日；Python 的 weekday() 以周一为 0，容易搞混。"""
    for expression in ("0 0 * * 0", "0 0 * * 7"):
        expr = CronExpression.parse(expression)
        assert expr.matches(datetime(2026, 11, 1, 0, 0)), "2026-11-01 是周日"


def test_day_and_weekday_are_union_not_intersection() -> None:
    """``0 0 1 * 1`` 是"每月 1 号**或**每周一"，不是"且"。

    取交集是新手最常犯的错：表达式看着像"1 号的周一"，
    而 Vixie cron 的实际行为是并集。写成交集会让任务少跑很多次。
    """
    expr = CronExpression.parse("0 0 1 * 1")
    assert expr.matches(datetime(2026, 12, 1, 0, 0)), "1 号（周二）应命中"
    assert expr.matches(datetime(2026, 11, 2, 0, 0)), "周一应命中"
    assert not expr.matches(datetime(2026, 12, 2, 0, 0)), "既非 1 号也非周一"


def test_weekday_only_when_day_is_wildcard() -> None:
    expr = CronExpression.parse("0 0 * * 1")
    assert expr.matches(datetime(2026, 11, 2, 0, 0))
    assert not expr.matches(datetime(2026, 11, 3, 0, 0))


def test_month_is_respected() -> None:
    expr = CronExpression.parse("0 0 1 1 *")
    assert expr.matches(datetime(2027, 1, 1, 0, 0))
    assert not expr.matches(datetime(2027, 2, 1, 0, 0))


# ==================================================================
# 推算下一次
# ==================================================================
def test_next_after_rolls_to_next_hour() -> None:
    expr = CronExpression.parse("15 * * * *")
    assert expr.next_after(datetime(2026, 10, 27, 12, 40)) == datetime(2026, 10, 27, 13, 15)
    # 恰好命中时也要严格往后，否则同一分钟会被反复触发
    assert expr.next_after(datetime(2026, 10, 27, 12, 15)) == datetime(2026, 10, 27, 13, 15)


def test_next_after_rolls_to_tomorrow() -> None:
    expr = CronExpression.parse("20 4 * * *")
    assert expr.next_after(datetime(2026, 10, 27, 12, 40)) == datetime(2026, 10, 28, 4, 20)


def test_next_after_handles_leap_day() -> None:
    """逐分钟扫描的实现在这类表达式上会跑几十万次，这里顺带验证按天跳转的实现。"""
    expr = CronExpression.parse("0 0 29 2 *")
    assert expr.next_after(datetime(2026, 3, 1)) == datetime(2028, 2, 29)


def test_next_after_skips_second_precision() -> None:
    """秒与微秒不参与判断，结果一律落在整分钟上。"""
    expr = CronExpression.parse("* * * * *")
    result = expr.next_after(datetime(2026, 10, 27, 12, 40, 37, 123456))
    assert result == datetime(2026, 10, 27, 12, 41)


# ==================================================================
# 调度器
# ==================================================================
def test_fires_once_per_minute() -> None:
    """每 20 秒轮询的循环会在同一分钟里反复检查，绝不能重复执行。"""
    calls: list[int] = []
    job = _job("collect", "15 * * * *", lambda: calls.append(1) or 0)
    scheduler = Scheduler([job], started_at=datetime(2026, 10, 27, 12, 0))

    assert scheduler.run_pending(datetime(2026, 10, 27, 12, 15, 5)) == [("collect", 0)]
    assert scheduler.run_pending(datetime(2026, 10, 27, 12, 15, 25)) == []
    assert scheduler.run_pending(datetime(2026, 10, 27, 12, 15, 45)) == []
    assert len(calls) == 1


def test_does_not_fire_early() -> None:
    calls: list[int] = []
    job = _job("collect", "15 * * * *", lambda: calls.append(1) or 0)
    scheduler = Scheduler([job], started_at=datetime(2026, 10, 27, 12, 0))

    assert scheduler.run_pending(datetime(2026, 10, 27, 12, 14)) == []
    assert calls == []


def test_catches_up_after_a_long_job() -> None:
    """采集可能跑十分钟：期间到点的作业不能因为"错过了那一分钟"被永久跳过。"""
    job = _job("quality", "30 * * * *", lambda: 0)
    scheduler = Scheduler([job], started_at=datetime(2026, 10, 27, 12, 0))

    assert scheduler.run_pending(datetime(2026, 10, 27, 12, 31)) == [("quality", 0)]


def test_failed_job_does_not_block_the_rest() -> None:
    """单个作业抛异常必须被隔离，否则一次上游超时会让调度进程整个停摆。"""

    def boom() -> int:
        raise RuntimeError("上游超时")

    jobs = [
        _job("collect", "15 * * * *", boom),
        _job("quality", "15 * * * *", lambda: 0),
    ]
    scheduler = Scheduler(jobs, started_at=datetime(2026, 10, 27, 12, 0))

    assert scheduler.run_pending(datetime(2026, 10, 27, 12, 15)) == [
        ("collect", 1),
        ("quality", 0),
    ]


def test_job_exit_code_is_reported() -> None:
    job = _job("archive", "0 4 * * *", lambda: 2)
    scheduler = Scheduler([job], started_at=datetime(2026, 10, 27, 0, 0))
    assert scheduler.run_pending(datetime(2026, 10, 27, 4, 0)) == [("archive", 2)]


def test_describe_lists_next_run() -> None:
    job = _job("collect", "15 * * * *", lambda: 0)
    scheduler = Scheduler([job], clock=lambda: datetime(2026, 10, 27, 12, 40))
    rows = scheduler.describe()
    assert rows[0]["作业"] == "collect"
    assert rows[0]["下次"] == "2026-10-27 13:15"


# ==================================================================
# 配置装配
# ==================================================================
def test_default_jobs_are_three(workspace, settings) -> None:
    names = [job.name for job in build_default_jobs(settings)]
    assert names == ["collect", "quality", "archive"]


def test_empty_cron_disables_job(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """留空表示停用 —— 比让人写一个不可能命中的 cron 更明确。"""
    monkeypatch.setenv("ATMOS_SCHEDULE_QUALITY_CRON", "")
    from atmos.settings import reload_settings

    names = [job.name for job in build_default_jobs(reload_settings())]
    assert names == ["collect", "archive"]


def test_invalid_cron_in_settings_fails_fast(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """配置写错要在启动时就炸，而不是在凌晨四点静默不执行。"""
    monkeypatch.setenv("ATMOS_SCHEDULE_COLLECT_CRON", "99 * * * *")
    from atmos.settings import reload_settings

    with pytest.raises(CronError):
        build_default_jobs(reload_settings())


# ==================================================================
# 作业接线
# ==================================================================
class _FakeReport:
    def __init__(self, status: str) -> None:
        self._status = status

    def to_payload(self) -> dict:
        return {"status": self._status, "total_rows_written": 0}


def _patch_collector(monkeypatch: pytest.MonkeyPatch, status: str, captured: dict):
    class FakeCollector:
        async def run(self, **kwargs):
            captured.update(kwargs)
            return _FakeReport(status)

    # 处理函数内部才 import Collector，因此打模块属性即可生效
    monkeypatch.setattr("atmos.collect.runner.Collector", FakeCollector)


def test_collect_handler_skips_archive_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """每小时作业必须跳过归档 —— 默认值写反会让 15 城市 × 180 天每天多拉 24 次。"""
    captured: dict = {}
    _patch_collector(monkeypatch, "success", captured)

    from atmos.collect.scheduler import collect_handler

    assert collect_handler() == 0
    assert captured == {"scope": "all", "trigger": "scheduled", "include_archive": False}


def test_collect_handler_can_include_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}
    _patch_collector(monkeypatch, "success", captured)

    from atmos.collect.scheduler import collect_handler

    assert collect_handler(include_archive=True) == 0
    assert captured["include_archive"] is True


def test_collect_handler_maps_status_to_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """非 success 一律非零退出，否则部分失败会在日志里被当成成功。"""
    captured: dict = {}
    _patch_collector(monkeypatch, "partial", captured)

    from atmos.collect.scheduler import collect_handler

    assert collect_handler() == 1
