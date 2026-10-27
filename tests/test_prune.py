"""保留策略测试：干跑只报数、只删过期、保住"最新一条"、导出文件按时间清理。

这几条断言背后各有一条产品判断，写测试是为了把它们钉住：
* 默认干跑 —— 删除不可逆，一个命令必须能先看清楚；
* 不碰 parquet —— 测试里显式断言数据湖文件仍在；
* 最新一条评分永远保留 —— 否则停止采集的资产会突然"无数据"，看起来像故障。
"""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from atmos.db import session_scope
from atmos.models.catalog import AssetQualityScore, CollectionRun, DataAsset, QualityResult, utcnow
from atmos.prune import prune


def _first_asset_id() -> int:
    with session_scope() as session:
        return int(session.scalar(select(DataAsset.id).order_by(DataAsset.id).limit(1)))


def _seed_run(days_ago: int) -> None:
    with session_scope() as session:
        session.add(
            CollectionRun(
                run_id=f"run-{days_ago}",
                job="collect_weather_forecast",
                asset_id=_first_asset_id(),
                status="success",
                started_at=utcnow() - timedelta(days=days_ago),
            )
        )


def _seed_result(days_ago: int) -> None:
    with session_scope() as session:
        session.add(
            QualityResult(
                asset_id=_first_asset_id(),
                check_key="completeness_rate",
                dimension="completeness",
                status="pass",
                score=90.0,
                evaluated_at=utcnow() - timedelta(days=days_ago),
            )
        )


def _seed_score(days_ago: int, overall: float) -> None:
    with session_scope() as session:
        session.add(
            AssetQualityScore(
                asset_id=_first_asset_id(),
                overall=overall,
                grade="B",
                scored_at=utcnow() - timedelta(days=days_ago),
            )
        )


def _count(model) -> int:  # type: ignore[no-untyped-def]
    with session_scope() as session:
        return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_dry_run_reports_without_deleting(workspace) -> None:
    _seed_run(days_ago=120)
    _seed_result(days_ago=120)
    _seed_score(days_ago=120, overall=50.0)
    _seed_score(days_ago=1, overall=80.0)

    report = prune(days=90)

    assert report.dry_run is True
    assert report.rows["collection_runs"] == 1
    assert report.rows["quality_results"] == 1
    assert report.rows["asset_quality_scores"] == 1  # 老的那条，但最新一条不算
    assert _count(CollectionRun) == 1
    assert _count(QualityResult) == 1
    assert _count(AssetQualityScore) == 2


def test_apply_deletes_only_expired(workspace) -> None:
    _seed_run(days_ago=120)
    _seed_run(days_ago=2)
    _seed_result(days_ago=120)
    _seed_result(days_ago=2)

    report = prune(days=90, dry_run=False)

    assert report.rows["collection_runs"] == 1
    assert _count(CollectionRun) == 1
    assert _count(QualityResult) == 1


def test_keeps_latest_score_even_when_it_is_stale(workspace) -> None:
    """资产停止采集后，最后一条评分可能早就过期，但它仍是"当前状态"。"""
    _seed_score(days_ago=200, overall=60.0)

    report = prune(days=90, dry_run=False)

    assert report.rows["asset_quality_scores"] == 0
    assert _count(AssetQualityScore) == 1


def test_prunes_stale_exports(workspace, settings) -> None:
    exports = settings.export_path
    exports.mkdir(parents=True, exist_ok=True)
    old = exports / "old.csv"
    fresh = exports / "fresh.csv"
    old.write_text("a,b\n1,2\n", encoding="utf-8")
    fresh.write_text("a,b\n3,4\n", encoding="utf-8")
    stale = (utcnow() - timedelta(days=60)).replace(tzinfo=None).timestamp()
    os.utime(old, (stale, stale))

    report = prune(days=90, export_days=30, dry_run=False)

    assert report.export_files == 1
    assert not old.exists()
    assert fresh.exists(), "当期导出文件不能删"


def test_never_touches_the_lake(workspace, settings) -> None:
    """数据湖是唯一不可再生的东西，保留策略不得碰它。"""
    partition = settings.lake_path / "atmos.city_environment.daily" / "city=beijing" / "year=2026" / "month=01"
    partition.mkdir(parents=True, exist_ok=True)
    parquet = partition / "data.parquet"
    parquet.write_bytes(b"PAR1")

    prune(days=1, export_days=1, dry_run=False)

    assert parquet.exists()


def test_rejects_non_positive_days(workspace) -> None:
    with pytest.raises(ValueError):
        prune(days=0)
