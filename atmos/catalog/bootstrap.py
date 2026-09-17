"""资产目录引导（Bootstrap）。

把 ``config/*.yaml`` 中的元数据定义同步到 SQLite 资产目录。整个过程幂等：
重复执行只更新变化的部分，不会产生重复资产，也不会清除运行与质量历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from atmos.catalog.definitions import AssetRegistry
from atmos.db import session_scope
from atmos.logging_setup import get_logger
from atmos.models import (
    AssetColumn,
    CatalogMeta,
    City,
    DataAsset,
    LineageEdge,
    QualityCheck,
    utcnow,
)

logger = get_logger("catalog.bootstrap")


@dataclass
class BootstrapReport:
    """引导结果摘要。"""

    cities_created: int = 0
    cities_updated: int = 0
    cities_deactivated: int = 0
    assets_created: int = 0
    assets_updated: int = 0
    columns_written: int = 0
    edges_created: int = 0
    edges_updated: int = 0
    checks_created: int = 0
    checks_updated: int = 0
    registry: AssetRegistry | None = None
    finished_at: datetime | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "cities_created": self.cities_created,
            "cities_updated": self.cities_updated,
            "cities_deactivated": self.cities_deactivated,
            "assets_created": self.assets_created,
            "assets_updated": self.assets_updated,
            "columns_written": self.columns_written,
            "edges_created": self.edges_created,
            "edges_updated": self.edges_updated,
            "checks_created": self.checks_created,
            "checks_updated": self.checks_updated,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


# ==================================================================
# 城市
# ==================================================================
def sync_cities(session: Session, registry: AssetRegistry, report: BootstrapReport) -> dict[str, City]:
    """同步城市主数据，返回 slug → City 映射。

    ``config/cities.yaml`` 是城市主数据的唯一真相来源，因此目录是它的**投影**：
    配置中已移除的城市会被**停用**而不是删除 —— 历史采集运行、质量结果与数据湖
    分区仍然引用该城市，硬删除会破坏引用完整性；重新加入配置即可自动恢复启用。
    """
    existing = {city.slug: city for city in session.scalars(select(City)).all()}
    declared = {definition.slug for definition in registry.cities}

    for definition in registry.cities:
        payload = definition.to_payload()
        city = existing.get(definition.slug)
        if city is None:
            city = City(**payload)
            session.add(city)
            report.cities_created += 1
            continue
        changed = _apply(city, payload)
        if changed:
            report.cities_updated += 1

    for slug, city in existing.items():
        if slug in declared or not city.is_active:
            continue
        city.is_active = False
        report.cities_deactivated += 1
        logger.warning(
            "城市 %s（%s）已不在 config/cities.yaml 中，已停用；"
            "历史数据湖分区与采集记录保留，重新加入配置即可恢复",
            slug,
            city.name_zh,
        )

    session.flush()
    return {city.slug: city for city in session.scalars(select(City)).all()}


# ==================================================================
# 数据资产与字段字典
# ==================================================================
def sync_assets(session: Session, registry: AssetRegistry, report: BootstrapReport) -> dict[str, DataAsset]:
    """同步数据资产及其字段字典，返回 asset_key → DataAsset 映射。"""
    existing = {asset.asset_key: asset for asset in session.scalars(select(DataAsset)).all()}

    for definition in registry.assets:
        payload = definition.to_payload()
        asset = existing.get(definition.asset_key)
        if asset is None:
            asset = DataAsset(**payload, schema_hash=_schema_hash(definition))
            session.add(asset)
            session.flush()
            report.assets_created += 1
        else:
            if _apply(asset, payload):
                report.assets_updated += 1
            asset.schema_hash = _schema_hash(definition)
            session.flush()

        report.columns_written += _sync_columns(session, asset, definition.columns)

    session.flush()
    return {asset.asset_key: asset for asset in session.scalars(select(DataAsset)).all()}


def _sync_columns(session: Session, asset: DataAsset, columns: tuple) -> int:
    """按字段字典对齐列元数据：新增、更新、删除已移除的列。"""
    current = {column.name: column for column in asset.columns}
    declared_names = {column.name for column in columns}
    written = 0

    for column in columns:
        payload = column.to_payload()
        payload["value_range"] = payload["value_range"]
        existing = current.get(column.name)
        if existing is None:
            session.add(AssetColumn(asset_id=asset.id, **payload))
            written += 1
            continue
        _apply(existing, payload)
        written += 1

    for name, column in current.items():
        if name not in declared_names:
            logger.warning("字段 %s.%s 已从字段字典移除，同步删除", asset.asset_key, name)
            session.delete(column)

    session.flush()
    return written


# ==================================================================
# 血缘
# ==================================================================
def sync_lineage(
    session: Session,
    registry: AssetRegistry,
    assets: dict[str, DataAsset],
    report: BootstrapReport,
) -> None:
    """同步血缘边。"""
    existing = {
        (edge.upstream_asset_id, edge.downstream_asset_id, edge.transform_ref or ""): edge
        for edge in session.scalars(select(LineageEdge)).all()
    }

    declared: set[tuple[int, int, str]] = set()
    for definition in registry.lineage:
        upstream = assets.get(definition.upstream)
        downstream = assets.get(definition.downstream)
        if upstream is None or downstream is None:
            logger.error(
                "血缘边引用了未同步的资产，已跳过: %s → %s",
                definition.upstream,
                definition.downstream,
            )
            continue
        signature = (upstream.id, downstream.id, definition.transform_ref or "")
        declared.add(signature)
        payload = {
            "transform_type": definition.transform_type,
            "description": definition.description,
        }
        edge = existing.get(signature)
        if edge is None:
            session.add(
                LineageEdge(
                    upstream_asset_id=upstream.id,
                    downstream_asset_id=downstream.id,
                    transform_ref=definition.transform_ref,
                    **payload,
                )
            )
            report.edges_created += 1
        elif _apply(edge, payload):
            report.edges_updated += 1

    # 清理已从配置中删除的边
    for signature, edge in existing.items():
        if signature not in declared:
            logger.warning("血缘边已从配置移除，同步删除: edge#%s", edge.id)
            session.delete(edge)

    session.flush()


# ==================================================================
# 质量检查项
# ==================================================================
def sync_quality_checks(
    session: Session,
    registry: AssetRegistry,
    assets: dict[str, DataAsset],
    report: BootstrapReport,
) -> None:
    """按 ``applies_to`` 展开检查项并同步到目录。"""
    existing = {
        (check.asset_id, check.check_key): check
        for check in session.scalars(select(QualityCheck)).all()
    }
    declared: set[tuple[int, int]] = set()

    for asset_key, asset in assets.items():
        for definition in registry.quality.checks_for(asset_key):
            signature = (asset.id, definition.key)
            declared.add(signature)
            payload = {
                "name": definition.name,
                "dimension": definition.dimension,
                "severity": definition.severity,
                "description": definition.description,
                "params": definition.params,
                "is_enabled": True,
            }
            check = existing.get(signature)
            if check is None:
                session.add(
                    QualityCheck(asset_id=asset.id, check_key=definition.key, **payload)
                )
                report.checks_created += 1
            elif _apply(check, payload):
                report.checks_updated += 1

    for signature, check in existing.items():
        if signature not in declared:
            logger.info("检查项 %s 已不适用于该资产，同步删除", check.check_key)
            session.delete(check)

    session.flush()


# ==================================================================
# 入口
# ==================================================================
def bootstrap_catalog(registry: AssetRegistry | None = None) -> BootstrapReport:
    """把配置元数据全量同步到资产目录（幂等）。"""
    from atmos.catalog.definitions import load_registry

    registry = registry or load_registry()
    report = BootstrapReport(registry=registry)

    with session_scope() as session:
        sync_cities(session, registry, report)
        assets = sync_assets(session, registry, report)
        sync_lineage(session, registry, assets, report)
        sync_quality_checks(session, registry, assets, report)
        _stamp_meta(session, registry)

    report.finished_at = utcnow()
    logger.info(
        "目录引导完成：城市 +%d/~%d/-%d，资产 +%d/~%d，字段 %d，血缘 +%d/~%d，检查项 +%d/~%d",
        report.cities_created,
        report.cities_updated,
        report.cities_deactivated,
        report.assets_created,
        report.assets_updated,
        report.columns_written,
        report.edges_created,
        report.edges_updated,
        report.checks_created,
        report.checks_updated,
    )
    return report


def _schema_hash(definition: Any) -> str:
    from atmos.utils import sha256_of

    return sha256_of([column.to_payload() for column in definition.columns])


def _apply(entity: Any, payload: dict[str, Any]) -> bool:
    """把 payload 写入 ORM 实体，返回是否发生变化。"""
    changed = False
    for key, value in payload.items():
        if not hasattr(entity, key):
            continue
        current = getattr(entity, key)
        if isinstance(value, list) and isinstance(current, list):
            if list(value) == list(current):
                continue
        if current != value:
            setattr(entity, key, value)
            changed = True
    return changed


def _stamp_meta(session: Session, registry: AssetRegistry) -> None:
    """记录引导时间与规则版本，供 API 展示目录新鲜度。"""
    stamps = {
        "bootstrapped_at": utcnow().isoformat(),
        "rules_version": registry.quality.version,
        "asset_count": str(len(registry.assets)),
        "city_count": str(len(registry.cities)),
    }
    existing = {item.key: item for item in session.scalars(select(CatalogMeta)).all()}
    for key, value in stamps.items():
        item = existing.get(key)
        if item is None:
            session.add(CatalogMeta(key=key, value=value))
        else:
            item.value = value
    session.flush()


def read_meta(session: Session) -> dict[str, str]:
    """读取目录元信息。"""
    return {item.key: item.value or "" for item in session.scalars(select(CatalogMeta)).all()}


__all__ = [
    "BootstrapReport",
    "bootstrap_catalog",
    "sync_cities",
    "sync_assets",
    "sync_lineage",
    "sync_quality_checks",
    "read_meta",
]
