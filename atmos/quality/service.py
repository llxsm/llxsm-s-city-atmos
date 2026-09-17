"""质量结果查询服务（供 API 与看板使用）。"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from atmos.logging_setup import get_logger
from atmos.models import AssetQualityScore, City, DataAsset, QualityCheck, QualityResult, utcnow
from atmos.models.enums import CheckSeverity, CheckStatus, QualityDimension
from atmos.utils import iso_z, relative_time

logger = get_logger("quality.service")


def _grade_label(grade: str | None) -> str:
    return {
        "A": "优秀",
        "B": "良好",
        "C": "关注",
        "D": "较差",
        "E": "待治理",
    }.get(grade or "", "未评测")


def score_payload(score: AssetQualityScore, asset_key: str | None = None, city_name: str | None = None) -> dict[str, Any]:
    """评分记录序列化。"""
    return {
        "asset_key": asset_key,
        "city": city_name,
        "completeness": round(score.completeness, 2),
        "timeliness": round(score.timeliness, 2),
        "validity": round(score.validity, 2),
        "consistency": round(score.consistency, 2),
        "uniqueness": round(score.uniqueness, 2),
        "overall": round(score.overall, 2),
        "grade": score.grade,
        "grade_label": _grade_label(score.grade),
        "checks_total": score.checks_total,
        "checks_failed": score.checks_failed,
        "checks_warned": score.checks_warned,
        "window_hours": score.window_hours,
        "rules_version": score.rules_version,
        "scored_at": iso_z(score.scored_at),
        "scored_ago": relative_time(score.scored_at),
    }


def result_payload(
    result: QualityResult, asset_key: str | None = None, city_name: str | None = None
) -> dict[str, Any]:
    """检查明细序列化。"""
    dimension = QualityDimension(result.dimension)
    return {
        "asset_key": asset_key,
        "city": city_name,
        "check_key": result.check_key,
        "dimension": result.dimension,
        "dimension_label": dimension.label,
        "status": result.status,
        "status_label": CheckStatus(result.status).label if result.status in CheckStatus.values() else result.status,
        "score": round(result.score, 2),
        "observed_value": result.observed_value,
        "expected_value": result.expected_value,
        "records_checked": result.records_checked,
        "records_failed": result.records_failed,
        "message": result.message,
        "detail": result.detail or {},
        "evaluated_at": iso_z(result.evaluated_at),
        "evaluated_ago": relative_time(result.evaluated_at),
    }


def latest_scores(
    session: Session,
    *,
    asset_key: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """取每个资产的最新一次评分。"""
    statement = select(AssetQualityScore).order_by(AssetQualityScore.scored_at.desc()).limit(400)
    if asset_key:
        asset = session.scalar(select(DataAsset).where(DataAsset.asset_key == asset_key))
        if asset is None:
            return []
        statement = (
            select(AssetQualityScore)
            .where(AssetQualityScore.asset_id == asset.id)
            .order_by(AssetQualityScore.scored_at.desc())
            .limit(limit)
        )
        rows = list(session.scalars(statement).all())
        return [score_payload(row, asset_key) for row in rows]

    rows = list(session.scalars(statement).all())
    assets = {item.id: item.asset_key for item in session.scalars(select(DataAsset)).all()}
    seen: set[int] = set()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        if row.asset_id in seen:
            continue
        seen.add(row.asset_id)
        payloads.append(score_payload(row, assets.get(row.asset_id)))
        if len(payloads) >= limit:
            break
    return payloads


def score_history(session: Session, asset_key: str, *, limit: int = 60) -> list[dict[str, Any]]:
    """某资产的质量评分趋势。"""
    asset = session.scalar(select(DataAsset).where(DataAsset.asset_key == asset_key))
    if asset is None:
        return []
    rows = list(
        session.scalars(
            select(AssetQualityScore)
            .where(AssetQualityScore.asset_id == asset.id)
            .order_by(AssetQualityScore.scored_at.desc())
            .limit(limit)
        ).all()
    )
    return [score_payload(row, asset_key) for row in reversed(rows)]


def recent_results(
    session: Session,
    *,
    asset_key: str | None = None,
    status: str | None = None,
    dimension: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """最近的质量检查明细。"""
    statement = (
        select(QualityResult)
        .order_by(QualityResult.evaluated_at.desc(), QualityResult.id.desc())
        .limit(limit)
    )
    if asset_key:
        asset = session.scalar(select(DataAsset).where(DataAsset.asset_key == asset_key))
        if asset is None:
            return []
        statement = statement.where(QualityResult.asset_id == asset.id)
    if status:
        statement = statement.where(QualityResult.status == status)
    if dimension:
        statement = statement.where(QualityResult.dimension == dimension)

    rows = list(session.scalars(statement).all())
    assets = {item.id: item.asset_key for item in session.scalars(select(DataAsset)).all()}
    cities = {item.id: item.name_zh for item in session.scalars(select(City)).all()}
    return [
        result_payload(row, assets.get(row.asset_id), cities.get(row.city_id) if row.city_id else None)
        for row in rows
    ]


def list_checks(session: Session, *, asset_key: str | None = None) -> list[dict[str, Any]]:
    """质量检查项目录。"""
    statement = select(QualityCheck).order_by(QualityCheck.dimension, QualityCheck.check_key)
    if asset_key:
        asset = session.scalar(select(DataAsset).where(DataAsset.asset_key == asset_key))
        if asset is None:
            return []
        statement = statement.where(QualityCheck.asset_id == asset.id)
    rows = list(session.scalars(statement).all())
    assets = {item.id: item.asset_key for item in session.scalars(select(DataAsset)).all()}
    payloads: list[dict[str, Any]] = []
    for row in rows:
        dimension = QualityDimension(row.dimension)
        severity = CheckSeverity(row.severity)
        payloads.append(
            {
                "asset_key": assets.get(row.asset_id),
                "check_key": row.check_key,
                "name": row.name,
                "dimension": row.dimension,
                "dimension_label": dimension.label,
                "severity": row.severity,
                "severity_label": severity.label,
                "severity_weight": severity.weight,
                "description": row.description,
                "params": row.params or {},
                "is_enabled": row.is_enabled,
            }
        )
    return payloads


def quality_summary(session: Session) -> dict[str, Any]:
    """平台级质量态势：维度均值、等级分布、问题排行。"""
    scores = list(
        session.scalars(
            select(AssetQualityScore).order_by(AssetQualityScore.scored_at.desc()).limit(400)
        ).all()
    )
    assets = {item.id: item.asset_key for item in session.scalars(select(DataAsset)).all()}

    latest: dict[int, AssetQualityScore] = {}
    for score in scores:
        latest.setdefault(score.asset_id, score)

    if not latest:
        return {
            "evaluated_assets": 0,
            "dimension_average": {},
            "grade_distribution": {},
            "average_overall": None,
            "worst_assets": [],
            "top_failing_checks": [],
            "status_distribution": {},
            "generated_at": iso_z(utcnow()),
        }

    values = list(latest.values())
    dimension_average = {
        "completeness": round(sum(item.completeness for item in values) / len(values), 2),
        "timeliness": round(sum(item.timeliness for item in values) / len(values), 2),
        "validity": round(sum(item.validity for item in values) / len(values), 2),
        "consistency": round(sum(item.consistency for item in values) / len(values), 2),
        "uniqueness": round(sum(item.uniqueness for item in values) / len(values), 2),
    }
    grade_distribution: dict[str, int] = {}
    for item in values:
        grade_distribution[item.grade] = grade_distribution.get(item.grade, 0) + 1

    worst = sorted(values, key=lambda item: item.overall)[:5]

    recent = list(
        session.scalars(
            select(QualityResult)
            .where(QualityResult.status.in_([CheckStatus.FAIL.value, CheckStatus.WARN.value]))
            .order_by(QualityResult.evaluated_at.desc())
            .limit(500)
        ).all()
    )
    failing_counter: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "fail": 0, "warn": 0, "name": "", "dimension": ""}
    )
    status_distribution: dict[str, int] = defaultdict(int)
    for row in recent:
        status_distribution[row.status] += 1
        entry = failing_counter[row.check_key]
        entry["count"] += 1
        entry["name"] = row.check_key
        entry["dimension"] = row.dimension
        entry["fail" if row.status == CheckStatus.FAIL.value else "warn"] += 1

    top_failing = sorted(failing_counter.items(), key=lambda item: -item[1]["count"])[:8]

    return {
        "evaluated_assets": len(latest),
        "average_overall": round(sum(item.overall for item in values) / len(values), 2),
        "dimension_average": dimension_average,
        "grade_distribution": grade_distribution,
        "worst_assets": [
            {
                "asset_key": assets.get(item.asset_id),
                "overall": round(item.overall, 2),
                "grade": item.grade,
                "completeness": round(item.completeness, 2),
                "timeliness": round(item.timeliness, 2),
                "validity": round(item.validity, 2),
                "consistency": round(item.consistency, 2),
                "uniqueness": round(item.uniqueness, 2),
                "scored_at": iso_z(item.scored_at),
            }
            for item in worst
        ],
        "top_failing_checks": [
            {
                "check_key": key,
                "dimension": value["dimension"],
                "dimension_label": QualityDimension(value["dimension"]).label,
                "count": value["count"],
                "fail": value["fail"],
                "warn": value["warn"],
            }
            for key, value in top_failing
        ],
        "status_distribution": dict(status_distribution),
        "grade_labels": {grade: _grade_label(grade) for grade in grade_distribution},
        "generated_at": iso_z(utcnow()),
    }


def open_issues_count(session: Session) -> int:
    """近 24 小时未通过/预警的检查数。"""
    from datetime import timedelta

    value = session.scalar(
        select(func.count(QualityResult.id)).where(
            QualityResult.status.in_([CheckStatus.FAIL.value, CheckStatus.WARN.value]),
            QualityResult.evaluated_at >= utcnow() - timedelta(hours=24),
        )
    )
    return int(value or 0)


__all__ = [
    "latest_scores",
    "score_history",
    "recent_results",
    "list_checks",
    "quality_summary",
    "open_issues_count",
    "score_payload",
    "result_payload",
]
