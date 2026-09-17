"""数据湖写入器测试：幂等、去重、模式漂移、原子性。"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pyarrow.parquet as pq

from atmos.lake import layout
from atmos.lake.writer import LakeWriter
from atmos.sources.specs import ASSET_FORECAST_HOURLY
from tests.conftest import forecast_rows


def _writer() -> LakeWriter:
    return LakeWriter()


def test_write_creates_partitioned_file(workspace, registry) -> None:
    """写入后应生成符合分区约定的 Parquet 文件。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    report = _writer().write(asset, "beijing", forecast_rows(6), run_id="run-1")

    assert report.rows_written == 6
    assert report.partitions_written == 1
    target = layout.partition_file(ASSET_FORECAST_HOURLY, "beijing", 2026, 1)
    assert target.exists()
    assert report.bytes_written == target.stat().st_size

    table = pq.read_table(target)
    assert table.num_rows == 6
    assert set(asset.column_names) <= set(table.column_names)


def test_write_is_idempotent(workspace, registry) -> None:
    """重复写入同一批次不应产生重复记录。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    writer = _writer()
    rows = forecast_rows(6)

    first = writer.write(asset, "beijing", rows, run_id="run-1")
    second = writer.write(asset, "beijing", rows, run_id="run-2")

    assert first.rows_written == 6
    assert second.rows_written == 6
    assert second.duplicates_removed == 6
    assert writer.dataset_stats(ASSET_FORECAST_HOURLY)["row_count"] == 6


def test_latest_ingest_wins_on_conflict(workspace, registry) -> None:
    """主键冲突时应保留最新入库版本（值被覆盖）。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    writer = _writer()
    rows = forecast_rows(3)

    writer.write(asset, "beijing", rows, run_id="run-1")
    updated = [dict(row) for row in rows]
    updated[0]["temperature_2m"] = 99.0
    writer.write(asset, "beijing", updated, run_id="run-2")

    frame = writer.read_partition(ASSET_FORECAST_HOURLY, "beijing", 2026, 1)
    assert len(frame) == 3
    assert float(frame.loc[frame["time"] == rows[0]["time"], "temperature_2m"].iloc[0]) == 99.0
    assert frame.loc[frame["time"] == rows[0]["time"], "source_run_id"].iloc[0] == "run-2"


def test_incremental_append_across_partitions(workspace, registry) -> None:
    """跨月数据应落到不同分区，且总量正确。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    writer = _writer()

    # 从 1 月 31 日 22:00 起 4 小时 → 覆盖 1 月与 2 月两个分区
    crossing = forecast_rows(4, start="2026-01-31T22:00")
    writer.write(asset, "beijing", crossing, run_id="run-1")

    assert layout.partition_file(ASSET_FORECAST_HOURLY, "beijing", 2026, 1).exists()
    assert layout.partition_file(ASSET_FORECAST_HOURLY, "beijing", 2026, 2).exists()
    assert writer.dataset_stats(ASSET_FORECAST_HOURLY)["row_count"] == 4
    assert writer.read_partition(ASSET_FORECAST_HOURLY, "beijing", 2026, 1).shape[0] == 2
    assert writer.read_partition(ASSET_FORECAST_HOURLY, "beijing", 2026, 2).shape[0] == 2


def test_schema_drift_is_detected_and_dropped(workspace, registry) -> None:
    """上游新增字段应被识别为漂移并丢弃，不污染数据湖模式。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    rows = forecast_rows(2)
    for row in rows:
        row["brand_new_upstream_field"] = 1.23

    report = _writer().write(asset, "beijing", rows, run_id="run-1")
    assert report.has_schema_drift
    assert report.extra_columns == ["brand_new_upstream_field"]

    table = pq.read_table(layout.partition_file(ASSET_FORECAST_HOURLY, "beijing", 2026, 1))
    assert "brand_new_upstream_field" not in table.column_names


def test_missing_declared_columns_are_filled_and_reported(workspace, registry) -> None:
    """缺失声明列应补空并记录，保证物理模式稳定。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    rows = forecast_rows(2)
    for row in rows:
        row.pop("uv_index")

    report = _writer().write(asset, "beijing", rows, run_id="run-1")
    assert "uv_index" in report.missing_columns

    table = pq.read_table(layout.partition_file(ASSET_FORECAST_HOURLY, "beijing", 2026, 1))
    assert "uv_index" in table.column_names
    assert table.column("uv_index").null_count == 2


def test_out_of_type_values_become_nulls(workspace, registry) -> None:
    """非法值在类型强制阶段转为空值，而不是让整个批次失败。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    rows = forecast_rows(2)
    rows[0]["temperature_2m"] = "not-a-number"

    report = _writer().write(asset, "beijing", rows, run_id="run-1")
    assert report.rows_written == 2
    table = pq.read_table(layout.partition_file(ASSET_FORECAST_HOURLY, "beijing", 2026, 1))
    frame = table.to_pandas()
    assert pd.isna(frame.iloc[0]["temperature_2m"])


def test_empty_rows_are_skipped(workspace, registry) -> None:
    """空数据不应创建分区文件。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    report = _writer().write(asset, "beijing", [], run_id="run-1")
    assert report.empty
    assert not layout.dataset_dir(ASSET_FORECAST_HOURLY).exists()


def test_arrow_types_match_field_dictionary(workspace, registry) -> None:
    """落湖物理类型必须与字段字典声明一致（数据标准落地校验）。"""
    from atmos.analytics.query import LakeQuery

    asset = registry.asset(ASSET_FORECAST_HOURLY)
    _writer().write(asset, "beijing", forecast_rows(3), run_id="run-1")

    schema = LakeQuery().schema(ASSET_FORECAST_HOURLY)
    assert schema["temperature_2m"] == "DOUBLE"
    assert schema["weather_code"] == "BIGINT"
    assert schema["time"] == "TIMESTAMP"
    assert schema["source_run_id"] == "VARCHAR"


def test_no_temp_files_left_behind(workspace, registry) -> None:
    """原子写入不应残留临时文件。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    _writer().write(asset, "beijing", forecast_rows(3), run_id="run-1")
    leftovers = list(layout.dataset_dir(ASSET_FORECAST_HOURLY).rglob("*.tmp"))
    assert leftovers == []


def test_dataset_stats_reports_cities_and_time_bounds(workspace, registry) -> None:
    """统计应正确汇总城市数、行数与时间范围。"""
    asset = registry.asset(ASSET_FORECAST_HOURLY)
    writer = _writer()
    writer.write(asset, "beijing", forecast_rows(3), run_id="run-1")
    writer.write(asset, "shanghai", forecast_rows(3), run_id="run-1")

    stats = writer.dataset_stats(ASSET_FORECAST_HOURLY)
    assert stats["row_count"] == 6
    assert stats["city_count"] == 2
    assert stats["file_count"] == 2
    assert isinstance(stats["latest_data_time"], datetime)
