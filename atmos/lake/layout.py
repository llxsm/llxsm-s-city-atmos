"""数据湖目录布局约定。

分区方案
--------
时序资产（字段字典中声明了 ``time_column``）::

    <lake>/<asset_key>/city=<slug>/year=<YYYY>/month=<MM>/data.parquet

静态/参考资产（无时间列，例如地理编码参考数据）::

    <lake>/<asset_key>/city=<slug>/data.parquet

选择依据
--------
* 按 ``city`` 分区：查询几乎总带城市条件，且能天然隔离单城市回补影响面。
* 按 ``year/month`` 分区：逐小时资产单城市单月约 720 行，文件大小适中；
  写入时只需重写当前月分区，而非全量。
* 每分区单文件（``data.parquet``）：以 **重写代替追加**，
  通过主键去重实现幂等，避免小文件堆积与重复数据。
* 扫描路径统一用递归通配 ``**``，从而同时兼容上述两种层级深度，
  静态资产不会被迫落进一个虚假的 ``year=1970`` 分区。
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

from atmos.settings import Settings, get_settings

PARTITION_FILENAME = "data.parquet"
TEMP_SUFFIX = ".tmp"

# 递归通配：兼容 city=<slug>/[year=/month=]/data.parquet 两种深度
RECURSIVE_GLOB = "**"


def dataset_dir(asset_key: str, settings: Settings | None = None) -> Path:
    """资产在数据湖中的根目录。"""
    settings = settings or get_settings()
    return settings.lake_path / asset_key


def asset_glob(asset_key: str, settings: Settings | None = None) -> str:
    """供 DuckDB ``read_parquet`` 使用的通配路径（POSIX 风格）。"""
    root = dataset_dir(asset_key, settings)
    return (root / RECURSIVE_GLOB / PARTITION_FILENAME).as_posix()


def city_glob(asset_key: str, city_slug: str, settings: Settings | None = None) -> str:
    """单城市范围的通配路径。"""
    root = dataset_dir(asset_key, settings) / f"city={city_slug}"
    return (root / RECURSIVE_GLOB / PARTITION_FILENAME).as_posix()


def city_dir(asset_key: str, city_slug: str, settings: Settings | None = None) -> Path:
    """城市分区根目录。"""
    return dataset_dir(asset_key, settings) / f"city={city_slug}"


def static_partition_dir(
    asset_key: str, city_slug: str, settings: Settings | None = None
) -> Path:
    """无时间列资产的落湖目录（不分年月）。"""
    return city_dir(asset_key, city_slug, settings)


def static_partition_file(
    asset_key: str, city_slug: str, settings: Settings | None = None
) -> Path:
    """无时间列资产的数据文件。"""
    return static_partition_dir(asset_key, city_slug, settings) / PARTITION_FILENAME


def partition_dir(
    asset_key: str,
    city_slug: str,
    year: int,
    month: int,
    settings: Settings | None = None,
) -> Path:
    """分区目录。"""
    return city_dir(asset_key, city_slug, settings) / f"year={year:04d}" / f"month={month:02d}"


def partition_file(
    asset_key: str,
    city_slug: str,
    year: int,
    month: int,
    settings: Settings | None = None,
) -> Path:
    """分区数据文件。"""
    return partition_dir(asset_key, city_slug, year, month, settings) / PARTITION_FILENAME


def month_span(start: datetime | date, end: datetime | date) -> list[tuple[int, int]]:
    """两个时间点之间覆盖的所有 ``(year, month)`` 分区，按时间升序。"""
    start_year, start_month = start.year, start.month
    end_year, end_month = end.year, end.month
    result: list[tuple[int, int]] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        result.append((year, month))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return result


def list_partitions(asset_key: str, settings: Settings | None = None) -> list[dict[str, object]]:
    """枚举某资产现有的全部分区及其体积。

    ``year`` / ``month`` 在静态资产上为 ``None``。
    """
    root = dataset_dir(asset_key, settings)
    if not root.exists():
        return []

    found: list[dict[str, object]] = []
    for city_path in sorted(root.glob("city=*")):
        if not city_path.is_dir():
            continue
        for file_path in sorted(city_path.rglob(PARTITION_FILENAME)):
            relative = file_path.relative_to(city_path)
            year = month = None
            for part in relative.parts:
                if part.startswith("year="):
                    year = int(part.split("=", 1)[1])
                elif part.startswith("month="):
                    month = int(part.split("=", 1)[1])
            stat = file_path.stat()
            found.append(
                {
                    "city": city_path.name.split("=", 1)[-1],
                    "year": year,
                    "month": month,
                    "path": file_path,
                    "bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime),
                }
            )
    return found


def partition_label(city_slug: str, year: int | None, month: int | None) -> str:
    """人类可读的分区标签。"""
    if year is None or month is None:
        return f"city={city_slug}"
    return f"city={city_slug}/year={year:04d}/month={month:02d}"
