"""数据湖写入器。

核心不变量
----------
1. **幂等**：按资产声明的主键去重（保留最新入库版本），
   重复执行同一批次不会产生重复记录，也不会改变既有结果。
2. **模式受控**：落湖表结构由字段字典（资产目录）唯一决定。
   上游新增字段会被识别为 **schema 漂移** 并记录，而不是悄悄写入数据湖。
3. **原子落盘**：先写临时文件再 ``os.replace``，避免中断产生半截 Parquet。
4. **可溯源**：每行携带 ``ingested_at`` 与 ``source_run_id``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from atmos.errors import LakeError
from atmos.lake import layout
from atmos.logging_setup import get_logger
from atmos.settings import Settings, get_settings
from atmos.utils import sha256_of, utcnow

if TYPE_CHECKING:
    # 只在类型检查期依赖资产定义，避免 lake 与 catalog 之间的运行时循环导入。
    # 运行期通过鸭子类型使用 asset.asset_key / columns / primary_key 等属性。
    from atmos.catalog.definitions import AssetDefinition

logger = get_logger("lake.writer")

PARQUET_COMPRESSION = "zstd"

# 写入器自行注入的列：不参与 schema 漂移判定（漂移指的是"上游多给了什么"）
INJECTED_COLUMNS: frozenset[str] = frozenset({"city_id", "ingested_at", "source_run_id"})


@dataclass
class WriteReport:
    """一次落湖操作的完整结果，供运行记录与数据血缘审计使用。"""

    asset_key: str
    city_slug: str
    rows_received: int = 0
    rows_written: int = 0
    rows_merged: int = 0
    duplicates_removed: int = 0
    partitions_written: int = 0
    bytes_written: int = 0
    files: list[str] = field(default_factory=list)
    missing_columns: list[str] = field(default_factory=list)
    extra_columns: list[str] = field(default_factory=list)
    latest_data_time: datetime | None = None
    earliest_data_time: datetime | None = None
    schema_hash: str = ""
    empty: bool = False

    @property
    def has_schema_drift(self) -> bool:
        return bool(self.missing_columns or self.extra_columns)

    def to_payload(self) -> dict[str, Any]:
        return {
            "asset_key": self.asset_key,
            "city_slug": self.city_slug,
            "rows_received": self.rows_received,
            "rows_written": self.rows_written,
            "rows_merged": self.rows_merged,
            "duplicates_removed": self.duplicates_removed,
            "partitions_written": self.partitions_written,
            "bytes_written": self.bytes_written,
            "missing_columns": self.missing_columns,
            "extra_columns": self.extra_columns,
            "latest_data_time": self.latest_data_time.isoformat() if self.latest_data_time else None,
            "schema_hash": self.schema_hash,
        }


# ==================================================================
# 类型强制（数据标准落地）
# ==================================================================
def _cast_series(series: pd.Series, data_type: str) -> pd.Series:
    """按字段字典声明的类型强制转换，非法值转为空值。"""
    if data_type == "double":
        return pd.to_numeric(series, errors="coerce").astype("float64")
    if data_type == "integer":
        numeric = pd.to_numeric(series, errors="coerce")
        return numeric.round().astype("Int64")
    if data_type == "boolean":
        return series.map(
            lambda value: pd.NA
            if value is None or (isinstance(value, float) and value != value)
            else bool(value)
        ).astype("boolean")
    if data_type == "timestamp":
        return pd.to_datetime(series, errors="coerce")
    if data_type == "date":
        return pd.to_datetime(series, errors="coerce")
    # string
    return series.astype("string")


_ARROW_TYPES: dict[str, pa.DataType] = {
    "double": pa.float64(),
    "integer": pa.int64(),
    "string": pa.string(),
    "timestamp": pa.timestamp("us"),
    "date": pa.date32(),
    "boolean": pa.bool_(),
}


def _to_arrow_table(frame: pd.DataFrame, asset: AssetDefinition) -> pa.Table:
    """构造显式 schema 的 Arrow 表。

    Arrow 字段一律声明为 nullable，因为"是否允许为空"属于数据质量范畴，
    由质量引擎按字段字典判定；此处只固化物理类型。

    优先让 pyarrow 直接消费 pandas 的扩展数组（StringDtype / Int64 / boolean），
    这样 ``pd.NA`` 会被正确映射为 Arrow null；失败时退化为 Python 对象回退路径。
    """
    arrays: list[pa.Array] = []
    fields: list[pa.Field] = []

    for column in asset.columns:
        series = frame[column.name]
        target = _ARROW_TYPES[column.data_type]
        try:
            if column.data_type == "date":
                values = [
                    None
                    if pd.isna(item)
                    else item.date()
                    if isinstance(item, pd.Timestamp)
                    else item
                    for item in series
                ]
                array = pa.array(values, type=pa.date32())
            elif column.data_type == "timestamp":
                array = pa.array(
                    series.astype("datetime64[us]"), type=pa.timestamp("us"), from_pandas=True
                )
            else:
                array = pa.array(series, type=target, from_pandas=True)
        except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError, TypeError):
            array = _arrow_fallback(series, target, column, asset)
        arrays.append(array)
        fields.append(pa.field(column.name, array.type))

    return pa.Table.from_arrays(arrays, schema=pa.schema(fields))


def _arrow_fallback(
    series: pd.Series,
    target: pa.DataType,
    column: Any,
    asset: AssetDefinition,
) -> pa.Array:
    """退化路径：把 pandas 扩展数组转成纯 Python 对象后再交给 pyarrow。"""
    try:
        values = [None if pd.isna(item) else item for item in series.tolist()]
        return pa.array(values, type=target)
    except (pa.ArrowInvalid, pa.ArrowTypeError, ValueError, TypeError) as exc:
        raise LakeError(
            f"资产 {asset.asset_key} 字段 {column.name} 转换为 Arrow 类型失败: {exc}"
        ) from exc


def build_frame(
    rows: list[dict[str, Any]],
    asset: AssetDefinition,
) -> tuple[pd.DataFrame, list[str], list[str]]:
    """把行集对齐到资产字段字典，返回 (数据框, 缺失列, 多余列)。"""
    frame = pd.DataFrame(rows)
    declared = set(asset.column_names)
    extra_columns = sorted(column for column in frame.columns if column not in declared)
    if extra_columns:
        frame = frame.drop(columns=extra_columns)

    missing_columns = [column for column in asset.column_names if column not in frame.columns]
    for column in missing_columns:
        frame[column] = pd.NA

    aligned = pd.DataFrame(index=frame.index)
    for column in asset.columns:
        aligned[column.name] = _cast_series(frame[column.name], column.data_type)
    return aligned[list(asset.column_names)], missing_columns, extra_columns


# ==================================================================
# 写入器
# ==================================================================
class LakeWriter:
    """Parquet 数据湖写入器。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---------- 对外主入口 ----------
    def write(
        self,
        asset: AssetDefinition,
        city_slug: str,
        rows: list[dict[str, Any]],
        *,
        run_id: str,
        ingested_at: datetime | None = None,
    ) -> WriteReport:
        """把行集写入对应城市分区（幂等）。"""
        report = WriteReport(asset_key=asset.asset_key, city_slug=city_slug)
        report.rows_received = len(rows)
        if not rows:
            report.empty = True
            logger.info("资产 %s 城市 %s 无数据，跳过落湖", asset.asset_key, city_slug)
            return report

        stamped = ingested_at or utcnow()
        prepared = [
            {**row, "city_id": city_slug, "ingested_at": row.get("ingested_at") or stamped,
             "source_run_id": row.get("source_run_id") or run_id}
            for row in rows
        ]

        frame, missing, extra = build_frame(prepared, asset)
        report.missing_columns = missing
        report.extra_columns = sorted(set(extra) - INJECTED_COLUMNS)
        if report.extra_columns:
            logger.warning(
                "资产 %s 检测到 schema 漂移，以下上游字段未在字段字典中登记，已丢弃: %s",
                asset.asset_key,
                ", ".join(report.extra_columns),
            )
        if missing:
            logger.warning(
                "资产 %s 数据缺少字段字典中已声明的列: %s", asset.asset_key, ", ".join(missing)
            )

        report.schema_hash = sha256_of([column.to_payload() for column in asset.columns])
        report.rows_merged = len(frame)

        time_column = asset.time_column
        grouped = self._group_by_month(frame, time_column)

        for key, chunk in grouped.items():
            year, month = key if key is not None else (None, None)
            self._write_partition(asset, city_slug, year, month, chunk, report)

        if time_column and time_column in frame.columns:
            stamps = pd.to_datetime(frame[time_column], errors="coerce").dropna()
            if not stamps.empty:
                report.latest_data_time = stamps.max().to_pydatetime()
                report.earliest_data_time = stamps.min().to_pydatetime()

        logger.info(
            "落湖 %s[%s] 收到 %d 行 → 写入 %d 行（合并 %d 行，去重 %d 行，%d 个分区，%s 字节）",
            asset.asset_key,
            city_slug,
            report.rows_received,
            report.rows_written,
            report.rows_merged,
            report.duplicates_removed,
            report.partitions_written,
            f"{report.bytes_written:,}",
        )
        return report

    # ---------- 内部分区处理 ----------
    @staticmethod
    def _group_by_month(
        frame: pd.DataFrame, time_column: str | None
    ) -> dict[tuple[int, int] | None, pd.DataFrame]:
        """按 (year, month) 分组；无时间列的资产返回 ``{None: frame}`` 单一分区。"""
        if not time_column or time_column not in frame.columns:
            return {None: frame}
        stamps = pd.to_datetime(frame[time_column], errors="coerce")
        valid = stamps.notna()
        if not valid.any():
            raise LakeError(f"时间列 {time_column} 全部无法解析，拒绝落湖")
        dropped = int((~valid).sum())
        if dropped:
            logger.warning("时间列 %s 有 %d 行无法解析，已丢弃", time_column, dropped)
        frame = frame.loc[valid].copy()
        stamps = stamps.loc[valid]
        keys = list(zip(stamps.dt.year.astype(int), stamps.dt.month.astype(int)))
        return {key: frame.loc[[k == key for k in keys]] for key in sorted(set(keys))}

    def _write_partition(
        self,
        asset: "AssetDefinition",
        city_slug: str,
        year: int | None,
        month: int | None,
        chunk: pd.DataFrame,
        report: WriteReport,
    ) -> None:
        if year is None or month is None:
            target = layout.static_partition_file(asset.asset_key, city_slug, self.settings)
        else:
            target = layout.partition_file(asset.asset_key, city_slug, year, month, self.settings)
        target.parent.mkdir(parents=True, exist_ok=True)

        merged = chunk
        if target.exists():
            try:
                existing = pq.read_table(target).to_pandas()
                merged = pd.concat([existing, chunk], ignore_index=True)
            except Exception as exc:  # pragma: no cover - 仅当文件损坏
                raise LakeError(f"读取既有分区失败 {target}: {exc}") from exc

        # 先按字段字典重新强制类型，再做去重与排序。
        # 既有分区从 Parquet 读回后 dtype 会变化（DATE → object[datetime.date]、
        # TIMESTAMP 精度变化等），若先排序会因混合类型比较而失败。
        aligned = pd.DataFrame(index=merged.index)
        for column in asset.columns:
            aligned[column.name] = _cast_series(merged[column.name], column.data_type)

        if asset.primary_key:
            before = len(aligned)
            aligned = aligned.drop_duplicates(subset=list(asset.primary_key), keep="last")
            report.duplicates_removed += before - len(aligned)

        if asset.time_column and asset.time_column in aligned.columns:
            aligned = aligned.sort_values(asset.time_column, kind="stable")

        aligned = aligned.reset_index(drop=True)

        table = _to_arrow_table(aligned, asset)
        temp_path = target.with_suffix(target.suffix + layout.TEMP_SUFFIX)
        try:
            pq.write_table(
                table, temp_path, compression=PARQUET_COMPRESSION, use_dictionary=True
            )
            temp_path.replace(target)
        finally:
            if temp_path.exists():
                temp_path.unlink(missing_ok=True)

        size = target.stat().st_size
        report.partitions_written += 1
        report.bytes_written += size
        report.rows_written += len(aligned)
        report.files.append(str(target.relative_to(self.settings.lake_path)))

    # ---------- 只读辅助 ----------
    def read_partition(
        self, asset_key: str, city_slug: str, year: int | None = None, month: int | None = None
    ) -> pd.DataFrame:
        """读取单个分区（不存在时返回空表）。"""
        if year is None or month is None:
            path = layout.static_partition_file(asset_key, city_slug, self.settings)
        else:
            path = layout.partition_file(asset_key, city_slug, year, month, self.settings)
        if not path.exists():
            return pd.DataFrame()
        return pq.read_table(path).to_pandas()

    def dataset_stats(self, asset_key: str) -> dict[str, Any]:
        """扫描资产全部分区，汇总行数、体积、城市数与时间范围。"""
        partitions = layout.list_partitions(asset_key, self.settings)
        if not partitions:
            return {
                "row_count": 0,
                "byte_size": 0,
                "file_count": 0,
                "city_count": 0,
                "latest_data_time": None,
                "partition_count": 0,
            }

        total_rows = 0
        total_bytes = 0
        cities: set[str] = set()
        latest: datetime | None = None
        earliest: datetime | None = None

        asset_key_safe = asset_key
        for item in partitions:
            path = Path(str(item["path"]))
            try:
                table = pq.read_table(path)
            except Exception as exc:  # pragma: no cover
                logger.warning("分区读取失败，已跳过 %s: %s", path, exc)
                continue
            total_rows += table.num_rows
            total_bytes += int(item["bytes"])
            cities.add(str(item["city"]))
            for candidate in ("time", "date"):
                if candidate in table.column_names:
                    column = table.column(candidate).to_pylist()
                    values = [value for value in column if value is not None]
                    if values:
                        column_min, column_max = min(values), max(values)
                        earliest = column_min if earliest is None or column_min < earliest else earliest
                        latest = column_max if latest is None or column_max > latest else latest
                    break

        return {
            "row_count": total_rows,
            "byte_size": total_bytes,
            "file_count": len(partitions),
            "city_count": len(cities),
            "latest_data_time": latest,
            "earliest_data_time": earliest,
            "partition_count": len(partitions),
            "asset_key": asset_key_safe,
        }


def preview_rows(path: Path, limit: int = 5) -> list[dict[str, Any]]:
    """读取分区前若干行，用于目录页样例数据展示。"""
    if not path.exists():
        return []
    table = pq.read_table(path)
    return table.slice(0, limit).to_pylist()
