"""保留策略：清理过期的运行记录、质量明细与导出文件。

设计取舍
--------
1. **不碰数据湖。** Parquet 分区是唯一"删了要重新采集才能回来"的东西，而且
   ERA5 有约 5 天发布延迟、越早的历史越慢。运行记录、质量明细、导出文件都只是
   观测与派生产物，删掉的代价仅是"看不到那么久之前发生过什么"。
   （真要治理数据湖是另一个话题：分区是按**月**的，最小删除单位就是一个月。）

2. **默认干跑。** 不带 ``apply`` 只统计将要删除的数量，不落刀。删除不可逆，
   这个命令必须能先看清楚再执行。

3. **"当前状态"必须留住。** ``asset_quality_scores`` 是"这个资产现在多少分"的
   来源。若某个资产停止采集，它最后一条评分会随日期变老 —— 纯按时间删，页面上
   这个资产会从"63 分"变成"无数据"，看起来像数据出了问题。因此额外加一条约束：
   每个 (资产, 城市) 永远保留最新一条，无论多旧。

4. **删完不自动 VACUUM。** SQLite 的 DELETE 只把页标记为空闲、文件并不缩小；
   VACUUM 会重写整个库，期间需要约两倍磁盘并加独占锁。默认不做，需要时显式
   ``--vacuum``，最好挑低峰期。

5. **不动 ``asset_versions``。** 它承载版本对比，每行还带分区统计 JSON；先观察
   实际体积再决定要不要纳入清理，避免一上来就把"版本历史"这个功能删瘸。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import and_, delete, func, select, text

from atmos.db import get_engine, session_scope
from atmos.logging_setup import get_logger
from atmos.models.catalog import AssetQualityScore, CollectionRun, QualityResult, utcnow
from atmos.settings import Settings, get_settings

logger = get_logger("prune")


@dataclass
class PruneReport:
    """一次清理（或干跑）的结果。"""

    days: int
    export_days: int
    cutoff: datetime
    export_cutoff: datetime
    dry_run: bool
    rows: dict[str, int] = field(default_factory=dict)
    export_files: int = 0
    export_bytes: int = 0
    vacuumed: bool = False

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())

    def to_payload(self) -> dict[str, Any]:
        return {
            "days": self.days,
            "export_days": self.export_days,
            "cutoff": self.cutoff.isoformat(),
            "export_cutoff": self.export_cutoff.isoformat(),
            "dry_run": self.dry_run,
            "rows": dict(self.rows),
            "total_rows": self.total_rows,
            "export_files": self.export_files,
            "export_bytes": self.export_bytes,
            "vacuumed": self.vacuumed,
        }


def _count(session, model, condition) -> int:  # type: ignore[no-untyped-def]
    """满足条件的行数。删除前先数，干跑才有意义。"""
    return int(session.scalar(select(func.count()).select_from(model).where(condition)) or 0)


def _score_condition(cutoff: datetime):  # type: ignore[no-untyped-def]
    """评分表的过期条件：老于 cutoff，且不是该 (资产, 城市) 的最新一条。"""
    newest_ids = (
        select(func.max(AssetQualityScore.id))
        .group_by(AssetQualityScore.asset_id, AssetQualityScore.city_id)
        .scalar_subquery()
    )
    return and_(
        AssetQualityScore.scored_at < cutoff,
        AssetQualityScore.id.not_in(newest_ids),
    )


def _mtime_utc(path: Path) -> datetime:
    """文件修改时间，统一换算成**朴素 UTC**。

    与库里的时间戳保持同一口径。若这里用本地时间而 cutoff 是 UTC，
    在 UTC+8 的机器上会整体错 8 小时 —— 要么该删的删不掉，要么误删当天的文件。
    """
    stamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return stamp.replace(tzinfo=None)


def _collect_exports(root: Path, cutoff: datetime) -> list[Path]:
    """导出目录里早于 cutoff 的文件。目录不存在时返回空列表（首次部署常见）。"""
    if not root.exists():
        return []
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and _mtime_utc(path) < cutoff
    ]


def prune(
    *,
    days: int | None = None,
    export_days: int | None = None,
    dry_run: bool = True,
    vacuum: bool = False,
    settings: Settings | None = None,
) -> PruneReport:
    """按保留天数清理元数据与导出文件。

    ``dry_run=True``（默认）只统计不删除；``vacuum=True`` 在删除后回收文件空间。
    """
    settings = settings or get_settings()
    keep_days = settings.retention_days if days is None else days
    keep_export_days = settings.retention_export_days if export_days is None else export_days
    if keep_days < 1 or keep_export_days < 1:
        raise ValueError("保留天数必须为正整数")

    cutoff = utcnow() - timedelta(days=keep_days)
    export_cutoff = utcnow() - timedelta(days=keep_export_days)
    report = PruneReport(
        days=keep_days,
        export_days=keep_export_days,
        cutoff=cutoff,
        export_cutoff=export_cutoff,
        dry_run=dry_run,
    )

    targets = (
        ("collection_runs", CollectionRun, CollectionRun.started_at < cutoff),
        ("quality_results", QualityResult, QualityResult.evaluated_at < cutoff),
        ("asset_quality_scores", AssetQualityScore, _score_condition(cutoff)),
    )

    with session_scope() as session:
        for name, model, condition in targets:
            count = _count(session, model, condition)
            report.rows[name] = count
            if count and not dry_run:
                session.execute(delete(model).where(condition))
                logger.info("已清理 %s：%s 行（早于 %s）", name, count, cutoff.isoformat())

        stale_exports = _collect_exports(settings.export_path, export_cutoff)
        report.export_files = len(stale_exports)
        report.export_bytes = sum(path.stat().st_size for path in stale_exports)
        if stale_exports and not dry_run:
            for path in stale_exports:
                path.unlink(missing_ok=True)
            logger.info("已清理导出文件：%s 个", len(stale_exports))

    if vacuum and not dry_run:
        # VACUUM 不能在事务里执行，必须在提交之后用 AUTOCOMMIT 连接。
        with get_engine().execution_options(isolation_level="AUTOCOMMIT").connect() as connection:
            connection.execute(text("VACUUM"))
        report.vacuumed = True
        logger.info("已执行 VACUUM 回收文件空间")

    return report


__all__ = ["PruneReport", "prune"]
