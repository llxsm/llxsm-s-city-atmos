"""采集调度器。

职责
----
按依赖顺序编排作业、管理批次 ID、持久化运行记录、刷新资产统计。
作业实现本身在 ``atmos.collect.jobs``，本模块只管"什么时候跑、跑完记什么"。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.catalog.stats import refresh_asset_stats
from atmos.collect.jobs import (
    ASYNC_JOB_RUNNERS,
    JOB_SPECS,
    SYNC_JOB_RUNNERS,
    JobContext,
    JobOutcome,
    PipelineReport,
)
from atmos.db import session_scope
from atmos.logging_setup import get_logger
from atmos.models import City, CollectionRun
from atmos.models.enums import RunStatus, TriggerType
from atmos.settings import Settings, get_settings
from atmos.sources.openmeteo import OpenMeteoClient
from atmos.utils import iso_z, new_run_id, utcnow

logger = get_logger("collect.runner")

SCOPE_STAGES: dict[str, tuple[str, ...]] = {
    "all": ("reference", "collect", "derive"),
    "reference": ("reference",),
    "collect": ("reference", "collect"),
    "derive": ("derive",),
    "status_only": (),
}


class Collector:
    """采集管道执行器。

    ``transport`` 用于注入自定义 HTTP 传输层（测试时替换为 MockTransport，
    从而在不触网的前提下验证整条管道）。
    """

    def __init__(
        self,
        registry: AssetRegistry | None = None,
        settings: Settings | None = None,
        *,
        transport: Any | None = None,
    ) -> None:
        self.registry = registry or load_registry()
        self.settings = settings or get_settings()
        self.transport = transport

    # ---------- 城市选择 ----------
    def resolve_cities(self, city_slugs: list[str] | None = None) -> list[Any]:
        """确定本次采集的城市集合。

        优先使用目录中的城市主数据；目录为空时退化为配置定义，
        以保证首次运行（尚未 bootstrap）也能工作。
        """
        with session_scope() as session:
            statement = select(City).where(City.is_active.is_(True)).order_by(City.slug)
            if city_slugs:
                statement = select(City).where(City.slug.in_(city_slugs)).order_by(City.slug)
            cities = list(session.scalars(statement).all())
            for city in cities:
                session.expunge(city)

        if not cities:
            logger.info("目录中无城市记录，改用 config/cities.yaml 定义")
            definitions = self.registry.active_cities
            if city_slugs:
                wanted = set(city_slugs)
                definitions = tuple(item for item in definitions if item.slug in wanted)
            return list(definitions)

        unknown = (
            sorted(set(city_slugs) - {city.slug for city in cities}) if city_slugs else []
        )
        if unknown:
            logger.warning("以下城市未在目录中注册，已忽略: %s", ", ".join(unknown))

        limit = self.settings.collect_max_cities
        if len(cities) > limit:
            logger.warning(
                "城市数量 %d 超过 ATMOS_COLLECT_MAX_CITIES=%d，本次仅采集前 %d 个",
                len(cities),
                limit,
                limit,
            )
            cities = cities[:limit]
        return cities

    # ---------- 主入口 ----------
    async def run(
        self,
        *,
        scope: str = "all",
        city_slugs: list[str] | None = None,
        trigger: str = TriggerType.MANUAL.value,
        run_id: str | None = None,
        include_archive: bool = True,
    ) -> PipelineReport:
        """执行采集管道。"""
        if scope not in SCOPE_STAGES:
            raise ValueError(f"未知的采集范围: {scope}（可选 {sorted(SCOPE_STAGES)}）")

        batch_id = run_id or new_run_id("atmos")
        report = PipelineReport(run_id=batch_id, trigger=trigger, scope=scope)
        stages = SCOPE_STAGES[scope]

        cities = self.resolve_cities(city_slugs) if stages else []
        if stages and not cities:
            report.jobs = []
            report.finished_at = utcnow()
            logger.warning("没有可采集的城市，管道结束")
            return report

        context = JobContext(
            registry=self.registry,
            settings=self.settings,
            run_id=batch_id,
            trigger=trigger,
            cities=cities,
        )

        specs = [
            spec
            for spec in JOB_SPECS
            if spec.stage in stages and (include_archive or spec.name != "collect_weather_archive")
        ]

        logger.info(
            "采集管道启动 run_id=%s 范围=%s 城市=%d 作业=%d",
            batch_id,
            scope,
            len(cities),
            len(specs),
        )

        async with OpenMeteoClient(self.settings, transport=self.transport) as client:
            for spec in specs:
                outcome = await self._execute(spec.name, context, client)
                report.jobs.append(outcome)
                self._persist(outcome)
                self._refresh_stats(outcome)

        report.finished_at = utcnow()
        logger.info(
            "采集管道结束 run_id=%s 状态=%s 行数=%s API 调用=%s 用时=%.1fs",
            batch_id,
            report.status,
            f"{report.total_rows_written:,}",
            report.total_api_calls,
            (report.duration_ms or 0) / 1000.0,
        )
        return report

    async def _execute(
        self, name: str, context: JobContext, client: OpenMeteoClient
    ) -> JobOutcome:
        """执行单个作业，捕获所有异常避免中断整条管道。"""
        started = utcnow()
        try:
            if name in ASYNC_JOB_RUNNERS:
                return await ASYNC_JOB_RUNNERS[name](context, client)
            if name in SYNC_JOB_RUNNERS:
                return SYNC_JOB_RUNNERS[name](context)
            raise KeyError(f"未注册的作业: {name}")
        except Exception as exc:
            logger.exception("作业 %s 执行失败", name)
            spec = next((item for item in JOB_SPECS if item.name == name), None)
            return JobOutcome(
                job=name,
                asset_key=spec.asset_key if spec else None,
                run_id=context.run_id,
                trigger=context.trigger,
                status=RunStatus.FAILED.value,
                started_at=started,
                finished_at=utcnow(),
                notes=[f"作业级异常: {type(exc).__name__}: {exc}"],
            )

    # ---------- 持久化 ----------
    def _persist(self, outcome: JobOutcome) -> None:
        """把作业结果写为运行记录（每城市一条）。"""
        if not outcome.cities:
            self._persist_single(outcome, city_slug=None)
            return
        for city_outcome in outcome.cities:
            self._persist_single(outcome, city_slug=city_outcome.city_slug, city_outcome=city_outcome)

    def _persist_single(
        self, outcome: JobOutcome, *, city_slug: str | None, city_outcome: Any = None
    ) -> None:
        from atmos.models import DataAsset

        with session_scope() as session:
            asset_row = (
                session.scalar(select(DataAsset).where(DataAsset.asset_key == outcome.asset_key))
                if outcome.asset_key
                else None
            )
            city_row = (
                session.scalar(select(City).where(City.slug == city_slug)) if city_slug else None
            )
            status = city_outcome.status if city_outcome is not None else outcome.status
            session.add(
                CollectionRun(
                    run_id=outcome.run_id,
                    job=outcome.job,
                    asset_id=asset_row.id if asset_row else None,
                    city_id=city_row.id if city_row else None,
                    status=status,
                    trigger=outcome.trigger,
                    started_at=outcome.started_at,
                    finished_at=outcome.finished_at,
                    duration_ms=outcome.duration_ms,
                    rows_written=city_outcome.rows_written if city_outcome else outcome.rows_written,
                    rows_read=city_outcome.rows_received if city_outcome else outcome.rows_received,
                    bytes_written=city_outcome.bytes_written
                    if city_outcome
                    else outcome.bytes_written,
                    api_calls=city_outcome.api_calls if city_outcome else outcome.api_calls,
                    partitions_written=city_outcome.partitions_written
                    if city_outcome
                    else outcome.partitions_written,
                    error_type=city_outcome.error_type if city_outcome else None,
                    error_message=city_outcome.error_message if city_outcome else None,
                    detail={
                        "notes": outcome.notes,
                        **(
                            {"schema_drift": city_outcome.schema_drift}
                            if city_outcome and city_outcome.schema_drift
                            else {}
                        ),
                        **(
                            {"city_detail": city_outcome.detail}
                            if city_outcome and city_outcome.detail
                            else {}
                        ),
                    },
                )
            )

    def _refresh_stats(self, outcome: JobOutcome) -> None:
        """作业完成后刷新资产统计（派生作业会覆盖上游统计，故统一在此重算）。"""
        if not outcome.asset_key or outcome.status == RunStatus.FAILED.value:
            return
        try:
            refresh_asset_stats(outcome.asset_key, run_id=outcome.run_id, registry=self.registry)
        except Exception as exc:  # pragma: no cover
            logger.error("资产 %s 统计刷新失败: %s", outcome.asset_key, exc)


# ==================================================================
# 便捷函数
# ==================================================================
async def run_collection(
    *,
    scope: str = "all",
    city_slugs: list[str] | None = None,
    trigger: str = TriggerType.CLI.value,
    include_archive: bool = True,
) -> PipelineReport:
    """一步式采集入口。"""
    return await Collector().run(
        scope=scope, city_slugs=city_slugs, trigger=trigger, include_archive=include_archive
    )


def recent_runs(limit: int = 100, *, asset_key: str | None = None) -> list[dict[str, Any]]:
    """最近运行记录（供运维接口与看板使用）。"""
    from atmos.models import DataAsset

    with session_scope() as session:
        statement = select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(limit)
        if asset_key:
            asset_row = session.scalar(
                select(DataAsset).where(DataAsset.asset_key == asset_key)
            )
            if asset_row is None:
                return []
            statement = (
                select(CollectionRun)
                .where(CollectionRun.asset_id == asset_row.id)
                .order_by(CollectionRun.started_at.desc())
                .limit(limit)
            )
        runs = list(session.scalars(statement).all())
        assets = {item.id: item.asset_key for item in session.scalars(select(DataAsset)).all()}
        cities = {item.id: item.name_zh for item in session.scalars(select(City)).all()}
        return [
            {
                "run_id": run.run_id,
                "job": run.job,
                "asset_key": assets.get(run.asset_id) if run.asset_id else None,
                "city_slug": run.city.slug if run.city else None,
                "city_name": cities.get(run.city_id) if run.city_id else None,
                "status": run.status,
                "trigger": run.trigger,
                "started_at": iso_z(run.started_at),
                "finished_at": iso_z(run.finished_at),
                "duration_ms": run.duration_ms,
                "rows_written": run.rows_written,
                "bytes_written": run.bytes_written,
                "api_calls": run.api_calls,
                "partitions_written": run.partitions_written,
                "error_type": run.error_type,
                "error_message": run.error_message,
                "detail": run.detail or {},
            }
            for run in runs
        ]


def run_summary(limit_hours: int = 24) -> dict[str, Any]:
    """运行态势汇总。"""
    from datetime import timedelta

    with session_scope() as session:
        since = utcnow() - timedelta(hours=limit_hours)
        runs = list(
            session.scalars(
                select(CollectionRun).where(CollectionRun.started_at >= since)
            ).all()
        )
        by_status: dict[str, int] = {}
        by_job: dict[str, dict[str, int]] = {}
        for run in runs:
            by_status[run.status] = by_status.get(run.status, 0) + 1
            bucket = by_job.setdefault(run.job, {"total": 0, "success": 0, "failed": 0, "partial": 0})
            bucket["total"] += 1
            if run.status in bucket:
                bucket[run.status] += 1
        last_run: datetime | None = max((run.started_at for run in runs), default=None)
        return {
            "window_hours": limit_hours,
            "total": len(runs),
            "by_status": by_status,
            "by_job": by_job,
            "last_run_at": iso_z(last_run),
            "total_rows_written": sum(run.rows_written for run in runs),
            "total_api_calls": sum(run.api_calls for run in runs),
            "total_bytes_written": sum(run.bytes_written for run in runs),
            "success_rate": round(
                by_status.get(RunStatus.SUCCESS.value, 0) / len(runs) * 100, 1
            )
            if runs
            else None,
        }


__all__ = ["Collector", "run_collection", "recent_runs", "run_summary", "SCOPE_STAGES"]
