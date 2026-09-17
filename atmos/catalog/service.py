"""资产目录查询服务。

对 API 层屏蔽 ORM 细节，统一输出面向"数据资产管理"语义的视图：
资产卡片、字段字典、责任矩阵、SLA 履约、治理巡检。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from atmos.catalog.bootstrap import read_meta
from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.logging_setup import get_logger
from atmos.models import (
    AssetColumn,
    AssetQualityScore,
    AssetVersion,
    City,
    CollectionRun,
    DataAsset,
    QualityCheck,
    QualityResult,
    utcnow,
)
from atmos.models.enums import (
    AssetDomain,
    AssetLayer,
    AssetStatus,
    CheckStatus,
    Granularity,
    QualityDimension,
    RunStatus,
    Sensitivity,
    UpdateFrequency,
)
from atmos.utils import iso_z, relative_time

logger = get_logger("catalog.service")


# ==================================================================
# 序列化
# ==================================================================
def _enum_label(enum_cls: Any, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return enum_cls(value).label
    except ValueError:
        return value


def _asset_summary(asset: DataAsset) -> dict[str, Any]:
    freshness = _freshness_state(asset)
    return {
        "key": asset.asset_key,
        "name": asset.name,
        "domain": asset.domain,
        "domain_label": _enum_label(AssetDomain, asset.domain),
        "layer": asset.layer,
        "layer_label": _enum_label(AssetLayer, asset.layer),
        "owner": asset.owner,
        "steward": asset.steward,
        "status": asset.status,
        "status_label": _enum_label(AssetStatus, asset.status),
        "description": asset.description,
        "granularity": asset.granularity,
        "granularity_label": _enum_label(Granularity, asset.granularity),
        "update_frequency": asset.update_frequency,
        "update_frequency_label": _enum_label(UpdateFrequency, asset.update_frequency),
        "sensitivity": asset.sensitivity,
        "sensitivity_label": _enum_label(Sensitivity, asset.sensitivity),
        "row_count": asset.row_count,
        "byte_size": asset.byte_size,
        "city_count": asset.city_count,
        "column_count": len(asset.columns),
        "latest_data_time": iso_z(asset.latest_data_time),
        "latest_data_ago": relative_time(asset.latest_data_time),
        "latest_run_at": iso_z(asset.latest_run_at),
        "latest_score": asset.latest_score,
        "latest_grade": asset.latest_grade,
        "sla_freshness_minutes": asset.sla_freshness_minutes,
        "sla_completeness": asset.sla_completeness,
        "freshness": freshness,
        "tags": asset.tags or [],
        "source_system": asset.source_system,
    }


def _freshness_state(asset: DataAsset) -> dict[str, Any]:
    """按 SLA 判定资产生效状态。

    无时间列的参考资产（如地理编码主数据）没有"数据时间"概念，
    其新鲜度应以最近一次成功采集时间为基准，否则会被误判为长期超期。
    """
    reference = asset.latest_data_time
    basis = "data_time"
    if reference is None and not asset.time_column:
        reference = asset.latest_run_at
        basis = "last_run"

    if reference is None:
        return {
            "state": "no_data",
            "label": "无数据",
            "delay_minutes": None,
            "breached": True,
            "basis": basis,
        }
    delay = (utcnow() - reference).total_seconds() / 60.0
    sla = asset.sla_freshness_minutes
    if sla is None:
        return {
            "state": "unknown",
            "label": "未定义 SLA",
            "delay_minutes": round(delay, 1),
            "breached": False,
            "basis": basis,
        }
    breached = delay > sla
    return {
        "state": "breached" if breached else "healthy",
        "label": "超出 SLA" if breached else "符合 SLA",
        "delay_minutes": round(delay, 1),
        "sla_minutes": sla,
        "breached": breached,
        "basis": basis,
    }


def _column_payload(column: AssetColumn) -> dict[str, Any]:
    return {
        "name": column.name,
        "data_type": column.data_type,
        "unit": column.unit,
        "description": column.description,
        "value_range": column.value_range,
        "enum_values": column.enum_values,
        "is_nullable": column.is_nullable,
        "is_primary_key": column.is_primary_key,
        "is_pii": column.is_pii,
        "is_derived": column.is_derived,
        "ordinal": column.ordinal,
    }


def _version_payload(version: AssetVersion) -> dict[str, Any]:
    return {
        "version": version.version,
        "run_id": version.run_id,
        "row_count": version.row_count,
        "byte_size": version.byte_size,
        "file_count": version.file_count,
        "city_count": version.city_count,
        "snapshot_at": iso_z(version.snapshot_at),
        "snapshot_ago": relative_time(version.snapshot_at),
    }


def _city_payload(city: City) -> dict[str, Any]:
    return {
        "slug": city.slug,
        "name_zh": city.name_zh,
        "name_en": city.name_en,
        "country": city.country,
        "country_code": city.country_code,
        "admin1": city.admin1,
        "latitude": city.latitude,
        "longitude": city.longitude,
        "timezone": city.timezone,
        "elevation": city.elevation,
        "population": city.population,
        "climate_zone": city.climate_zone,
        "is_active": city.is_active,
        "tags": city.tags or [],
    }


# ==================================================================
# 资产
# ==================================================================
def list_assets(
    session: Session,
    *,
    domain: str | None = None,
    layer: str | None = None,
    status: str | None = None,
    search: str | None = None,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    """按条件检索资产卡片。"""
    statement = select(DataAsset).options(selectinload(DataAsset.columns))
    if domain:
        statement = statement.where(DataAsset.domain == domain)
    if layer:
        statement = statement.where(DataAsset.layer == layer)
    if status:
        statement = statement.where(DataAsset.status == status)
    if owner:
        statement = statement.where(DataAsset.owner == owner)
    statement = statement.order_by(DataAsset.layer, DataAsset.asset_key)

    assets = list(session.scalars(statement).all())
    if search:
        needle = search.strip().lower()
        assets = [
            asset
            for asset in assets
            if needle in asset.asset_key.lower()
            or needle in asset.name.lower()
            or needle in (asset.description or "").lower()
            or any(needle in tag.lower() for tag in (asset.tags or []))
            or any(needle in column.name.lower() for column in asset.columns)
        ]
    return [_asset_summary(asset) for asset in assets]


def get_asset_detail(session: Session, asset_key: str) -> dict[str, Any] | None:
    """资产全量详情：卡片 + 字段字典 + 版本 + 检查项 + 最近运行。"""
    asset = session.scalar(
        select(DataAsset)
        .where(DataAsset.asset_key == asset_key)
        .options(selectinload(DataAsset.columns), selectinload(DataAsset.checks))
    )
    if asset is None:
        return None

    versions = list(
        session.scalars(
            select(AssetVersion)
            .where(AssetVersion.asset_id == asset.id)
            .order_by(AssetVersion.snapshot_at.desc())
            .limit(10)
        ).all()
    )
    runs = list(
        session.scalars(
            select(CollectionRun)
            .where(CollectionRun.asset_id == asset.id)
            .order_by(CollectionRun.started_at.desc())
            .limit(10)
        ).all()
    )
    score = session.scalar(
        select(AssetQualityScore)
        .where(AssetQualityScore.asset_id == asset.id)
        .order_by(AssetQualityScore.scored_at.desc())
        .limit(1)
    )
    failed = session.scalar(
        select(func.count(QualityResult.id)).where(
            QualityResult.asset_id == asset.id,
            QualityResult.status.in_([CheckStatus.FAIL.value, CheckStatus.WARN.value]),
            QualityResult.evaluated_at >= utcnow() - timedelta(hours=24),
        )
    )

    detail = _asset_summary(asset)
    detail.update(
        {
            "business_definition": asset.business_definition,
            "source_product": asset.source_product,
            "source_endpoint": asset.source_endpoint,
            "source_doc": asset.source_doc,
            "license": asset.license,
            "storage_uri": asset.storage_uri,
            "storage_format": asset.storage_format,
            "partition_keys": asset.partition_keys or [],
            "primary_key": asset.primary_key or [],
            "time_column": asset.time_column,
            "schema_hash": asset.schema_hash,
            "created_at": iso_z(asset.created_at),
            "updated_at": iso_z(asset.updated_at),
            "columns": [_column_payload(column) for column in asset.columns],
            "versions": [_version_payload(version) for version in versions],
            "checks": [
                {
                    "check_key": check.check_key,
                    "name": check.name,
                    "dimension": check.dimension,
                    "dimension_label": _enum_label(QualityDimension, check.dimension),
                    "severity": check.severity,
                    "description": check.description,
                    "params": check.params,
                    "is_enabled": check.is_enabled,
                }
                for check in sorted(asset.checks, key=lambda item: (item.dimension, item.check_key))
            ],
            "recent_runs": [_run_payload(run) for run in runs],
            "quality": _score_payload(score) if score else None,
            "open_issues_24h": int(failed or 0),
        }
    )
    return detail


def _score_payload(score: AssetQualityScore) -> dict[str, Any]:
    return {
        "completeness": round(score.completeness, 2),
        "timeliness": round(score.timeliness, 2),
        "validity": round(score.validity, 2),
        "consistency": round(score.consistency, 2),
        "uniqueness": round(score.uniqueness, 2),
        "overall": round(score.overall, 2),
        "grade": score.grade,
        "checks_total": score.checks_total,
        "checks_failed": score.checks_failed,
        "checks_warned": score.checks_warned,
        "window_hours": score.window_hours,
        "scored_at": iso_z(score.scored_at),
        "scored_ago": relative_time(score.scored_at),
    }


def _run_payload(run: CollectionRun) -> dict[str, Any]:
    return {
        "run_id": run.run_id,
        "job": run.job,
        "asset_key": run.asset.asset_key if run.asset else None,
        "city_slug": run.city.slug if run.city else None,
        "city_name": run.city.name_zh if run.city else None,
        "status": run.status,
        "status_label": _enum_label(RunStatus, run.status),
        "trigger": run.trigger,
        "started_at": iso_z(run.started_at),
        "started_ago": relative_time(run.started_at),
        "duration_ms": run.duration_ms,
        "rows_written": run.rows_written,
        "bytes_written": run.bytes_written,
        "api_calls": run.api_calls,
        "partitions_written": run.partitions_written,
        "error_type": run.error_type,
        "error_message": run.error_message,
    }


# ==================================================================
# 城市
# ==================================================================
def list_cities(session: Session, *, active_only: bool = False) -> list[dict[str, Any]]:
    """城市清单，附带每个城市的资产覆盖度。"""
    statement = select(City).order_by(City.slug)
    if active_only:
        statement = statement.where(City.is_active.is_(True))
    cities = list(session.scalars(statement).all())

    covered = {
        row[0]
        for row in session.execute(
            select(CollectionRun.city_id)
            .where(CollectionRun.status == RunStatus.SUCCESS.value)
            .distinct()
        ).all()
    }
    payloads = []
    for city in cities:
        item = _city_payload(city)
        item["has_data"] = city.id in covered
        payloads.append(item)
    return payloads


# ==================================================================
# 平台总览
# ==================================================================
def overview(session: Session, registry: AssetRegistry | None = None) -> dict[str, Any]:
    """首页仪表盘所需的平台级 KPI。"""
    registry = registry or load_registry()
    assets = list(session.scalars(select(DataAsset).options(selectinload(DataAsset.columns))).all())

    by_layer: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    total_rows = 0
    total_bytes = 0
    sla_breaches: list[dict[str, Any]] = []
    grade_distribution: dict[str, int] = {}

    for asset in assets:
        by_layer[asset.layer] = by_layer.get(asset.layer, 0) + 1
        by_domain[asset.domain] = by_domain.get(asset.domain, 0) + 1
        total_rows += asset.row_count
        total_bytes += asset.byte_size
        summary = _asset_summary(asset)
        if summary["freshness"]["breached"] and asset.status == AssetStatus.ACTIVE.value:
            sla_breaches.append(
                {
                    "key": asset.asset_key,
                    "name": asset.name,
                    "layer": asset.layer,
                    "delay_minutes": summary["freshness"].get("delay_minutes"),
                    "sla_minutes": asset.sla_freshness_minutes,
                }
            )
        if asset.latest_grade:
            grade_distribution[asset.latest_grade] = grade_distribution.get(asset.latest_grade, 0) + 1

    latest_scores = list(
        session.scalars(
            select(AssetQualityScore).order_by(AssetQualityScore.scored_at.desc()).limit(200)
        ).all()
    )
    seen_assets: set[int] = set()
    latest_per_asset: list[AssetQualityScore] = []
    for score in latest_scores:
        if score.asset_id in seen_assets:
            continue
        seen_assets.add(score.asset_id)
        latest_per_asset.append(score)

    avg_score = (
        round(sum(item.overall for item in latest_per_asset) / len(latest_per_asset), 2)
        if latest_per_asset
        else None
    )

    runs_24h = list(
        session.scalars(
            select(CollectionRun).where(CollectionRun.started_at >= utcnow() - timedelta(hours=24))
        ).all()
    )
    succeeded = sum(1 for run in runs_24h if run.status == RunStatus.SUCCESS.value)
    failed = sum(1 for run in runs_24h if run.status == RunStatus.FAILED.value)
    partial = sum(1 for run in runs_24h if run.status == RunStatus.PARTIAL.value)

    cities = session.scalar(select(func.count(City.id))) or 0
    active_cities = session.scalar(
        select(func.count(City.id)).where(City.is_active.is_(True))
    ) or 0
    column_count = session.scalar(select(func.count(AssetColumn.id))) or 0

    dimension_totals: dict[str, list[float]] = {}
    for score in latest_per_asset:
        dimension_totals.setdefault("completeness", []).append(score.completeness)
        dimension_totals.setdefault("timeliness", []).append(score.timeliness)
        dimension_totals.setdefault("validity", []).append(score.validity)
        dimension_totals.setdefault("consistency", []).append(score.consistency)
        dimension_totals.setdefault("uniqueness", []).append(score.uniqueness)
    dimension_average = {
        dimension: round(sum(values) / len(values), 2)
        for dimension, values in dimension_totals.items()
        if values
    }

    return {
        "asset_count": len(assets),
        "asset_count_by_layer": by_layer,
        "asset_count_by_domain": by_domain,
        "column_count": column_count,
        "city_count": cities,
        "active_city_count": active_cities,
        "total_rows": total_rows,
        "total_bytes": total_bytes,
        "avg_quality_score": avg_score,
        "quality_dimension_average": dimension_average,
        "grade_distribution": grade_distribution,
        "sla_breach_count": len(sla_breaches),
        "sla_breaches": sorted(sla_breaches, key=lambda item: -(item["delay_minutes"] or 0))[:10],
        "runs_24h": {
            "total": len(runs_24h),
            "success": succeeded,
            "failed": failed,
            "partial": partial,
            "success_rate": round(succeeded / len(runs_24h) * 100, 1) if runs_24h else None,
        },
        "open_quality_issues": sum(item.checks_failed + item.checks_warned for item in latest_per_asset),
        "meta": read_meta(session),
        "generated_at": iso_z(utcnow()),
    }


# ==================================================================
# 治理巡检
# ==================================================================
def governance_report(session: Session, registry: AssetRegistry | None = None) -> dict[str, Any]:
    """数据治理体检：责任、契约、血缘、质量四条线的问题清单。"""
    registry = registry or load_registry()
    assets = list(session.scalars(select(DataAsset).options(selectinload(DataAsset.columns))).all())

    missing_owner = [asset.asset_key for asset in assets if not asset.owner or asset.owner == "未指派"]
    missing_steward = [asset.asset_key for asset in assets if not asset.steward]
    missing_sla = [asset.asset_key for asset in assets if asset.sla_freshness_minutes is None]
    missing_pk = [asset.asset_key for asset in assets if not asset.primary_key]
    missing_definition = [asset.asset_key for asset in assets if not asset.business_definition]
    undocumented_columns = [
        f"{asset.asset_key}.{column.name}"
        for asset in assets
        for column in asset.columns
        if not column.description
    ]
    stale = [
        {"key": asset.asset_key, "name": asset.name, "ago": relative_time(asset.latest_data_time)}
        for asset in assets
        if asset.status == AssetStatus.ACTIVE.value and _freshness_state(asset)["breached"]
    ]

    scores = list(
        session.scalars(
            select(AssetQualityScore).order_by(AssetQualityScore.scored_at.desc()).limit(200)
        ).all()
    )
    seen: set[int] = set()
    low_grade = []
    for score in scores:
        if score.asset_id in seen:
            continue
        seen.add(score.asset_id)
        if score.grade in {"D", "E"}:
            low_grade.append(
                {
                    "asset_id": score.asset_id,
                    "grade": score.grade,
                    "overall": round(score.overall, 2),
                    "scored_at": iso_z(score.scored_at),
                }
            )

    assets_by_id = {asset.id: asset.asset_key for asset in assets}
    for item in low_grade:
        item["key"] = assets_by_id.get(item.pop("asset_id"))

    checks = [
        {
            "key": "responsibility.owner",
            "name": "资产责任人就位",
            "passed": not missing_owner,
            "affected": missing_owner,
        },
        {
            "key": "responsibility.steward",
            "name": "资产管理员就位",
            "passed": not missing_steward,
            "affected": missing_steward,
        },
        {
            "key": "contract.sla",
            "name": "已声明 SLA",
            "passed": not missing_sla,
            "affected": missing_sla,
        },
        {
            "key": "contract.primary_key",
            "name": "已声明主键",
            "passed": not missing_pk,
            "affected": missing_pk,
        },
        {
            "key": "documentation.business_definition",
            "name": "已写明业务口径",
            "passed": not missing_definition,
            "affected": missing_definition,
        },
        {
            "key": "documentation.columns",
            "name": "字段均有业务说明",
            "passed": not undocumented_columns,
            "affected": undocumented_columns[:20],
        },
        {
            "key": "operation.freshness",
            "name": "在用资产数据新鲜",
            "passed": not stale,
            "affected": stale,
        },
        {
            "key": "quality.grade",
            "name": "质量等级达标（C 及以上）",
            "passed": not low_grade,
            "affected": low_grade,
        },
    ]

    passed = sum(1 for item in checks if item["passed"])
    return {
        "score": round(passed / len(checks) * 100, 1),
        "passed_checks": passed,
        "total_checks": len(checks),
        "checks": checks,
        "generated_at": iso_z(utcnow()),
    }


__all__ = [
    "list_assets",
    "get_asset_detail",
    "list_cities",
    "overview",
    "governance_report",
]
