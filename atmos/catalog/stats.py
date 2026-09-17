"""资产统计与版本快照。

数据湖是数据的真相来源，目录里的体积/行数/时间范围属于 **派生统计**，
必须能从数据湖重算。本模块负责重算并把结果与版本快照写回目录，
从而支持"资产目录与物理数据是否一致"的审计。
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.db import session_scope
from atmos.lake.layout import list_partitions
from atmos.lake.writer import LakeWriter
from atmos.logging_setup import get_logger
from atmos.models import AssetVersion, DataAsset, utcnow

logger = get_logger("catalog.stats")


def refresh_asset_stats(
    asset_key: str,
    *,
    run_id: str | None = None,
    session: Session | None = None,
    registry: AssetRegistry | None = None,
) -> dict[str, Any]:
    """扫描数据湖重算某资产的统计并快照为版本。

    可传入外部 ``session`` 以参与更大事务；否则自行开启事务。
    """
    if session is not None:
        return _refresh(session, asset_key, run_id, registry)
    with session_scope() as owned:
        return _refresh(owned, asset_key, run_id, registry)


def _refresh(
    session: Session,
    asset_key: str,
    run_id: str | None,
    registry: AssetRegistry | None,
) -> dict[str, Any]:
    asset = session.scalar(select(DataAsset).where(DataAsset.asset_key == asset_key))
    if asset is None:
        logger.warning("资产 %s 不在目录中，跳过统计刷新", asset_key)
        return {}

    settings_registry = registry or load_registry()
    writer = LakeWriter()
    stats = writer.dataset_stats(asset_key)

    asset.row_count = int(stats["row_count"])
    asset.byte_size = int(stats["byte_size"])
    asset.city_count = int(stats["city_count"])
    latest = stats.get("latest_data_time")
    # 逐日资产的时间列物理类型为 DATE，读回是 date 而非 datetime
    if isinstance(latest, datetime):
        asset.latest_data_time = latest.replace(tzinfo=None)
    elif isinstance(latest, date):
        asset.latest_data_time = datetime.combine(latest, time.min)
    if run_id:
        asset.latest_run_at = utcnow()

    snapshot_at = utcnow()
    version = AssetVersion(
        asset_id=asset.id,
        version=f"snap-{snapshot_at.strftime('%Y%m%dT%H%M%S')}",
        run_id=run_id,
        row_count=asset.row_count,
        byte_size=asset.byte_size,
        file_count=int(stats["file_count"]),
        city_count=asset.city_count,
        schema_hash=asset.schema_hash,
        snapshot_at=snapshot_at,
        partition_stats=_partition_summary(asset_key, settings_registry),
    )
    session.add(version)
    session.flush()

    logger.debug(
        "资产 %s 统计刷新：%d 行 / %s 字节 / %d 城市",
        asset_key,
        asset.row_count,
        f"{asset.byte_size:,}",
        asset.city_count,
    )
    return {
        "asset_key": asset_key,
        "row_count": asset.row_count,
        "byte_size": asset.byte_size,
        "city_count": asset.city_count,
        "file_count": int(stats["file_count"]),
        "latest_data_time": asset.latest_data_time,
        "version": version.version,
    }


def _partition_summary(asset_key: str, registry: AssetRegistry) -> dict[str, Any]:
    """分区级统计，用于资产版本快照的体积分布分析。"""
    partitions = list_partitions(asset_key)
    by_city: dict[str, dict[str, int]] = {}
    for item in partitions:
        city = str(item["city"])
        bucket = by_city.setdefault(city, {"partitions": 0, "bytes": 0})
        bucket["partitions"] += 1
        bucket["bytes"] += int(item["bytes"])
    return {
        "total_partitions": len(partitions),
        "cities": by_city,
        "granularity": registry.asset(asset_key).granularity if registry.has_asset(asset_key) else None,
    }


def refresh_all_stats(*, run_id: str | None = None) -> list[dict[str, Any]]:
    """刷新全部在册资产的统计。"""
    registry = load_registry()
    results: list[dict[str, Any]] = []
    for asset in registry.assets:
        try:
            results.append(refresh_asset_stats(asset.asset_key, run_id=run_id, registry=registry))
        except Exception as exc:  # pragma: no cover - 单资产失败不影响其他
            logger.error("资产 %s 统计刷新失败: %s", asset.asset_key, exc)
    return [item for item in results if item]


def refresh_asset_scores(asset_key: str, overall: float, grade: str, session: Session) -> None:
    """把最新质量总评回写到资产行，供目录列表快速展示。"""
    asset = session.scalar(select(DataAsset).where(DataAsset.asset_key == asset_key))
    if asset is None:
        return
    asset.latest_score = round(overall, 2)
    asset.latest_grade = grade


__all__ = ["refresh_asset_stats", "refresh_all_stats", "refresh_asset_scores"]
