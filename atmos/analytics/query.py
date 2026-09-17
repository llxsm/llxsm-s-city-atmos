"""分析查询层：DuckDB 直读 Parquet 数据湖。

为什么用 DuckDB
---------------
* 零拷贝读取 Parquet，无需把数据装载进 SQLite；
* 完整 SQL 支持聚合、窗口函数、透视，足以支撑看板与质量统计；
* 进程内运行，无需额外服务，保持"一键启动"。

连接策略
--------
每次查询新建一个只读连接并显式关闭。DuckDB 的连接创建开销很低，
而独立连接天然隔离了线程与状态，避免 FastAPI 线程池下的共享态问题。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import duckdb
import pandas as pd

from atmos.errors import LakeError
from atmos.lake import layout
from atmos.logging_setup import get_logger
from atmos.settings import Settings, get_settings

logger = get_logger("analytics.query")

# 允许通过 API 透出的列名白名单校验（防注入）
_IDENTIFIER_SAFE = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")


def is_safe_identifier(name: str) -> bool:
    """校验 SQL 标识符仅含字母数字下划线，且不以数字开头。"""
    return bool(name) and name[0] not in "0123456789" and set(name) <= _IDENTIFIER_SAFE


@contextmanager
def duckdb_connection(settings: Settings | None = None):
    """只读 DuckDB 连接（进程内，无持久化状态）。"""
    settings = settings or get_settings()
    connection = duckdb.connect(database=":memory:")
    try:
        connection.execute("SET enable_progress_bar=false")
        yield connection
    finally:
        connection.close()


class LakeQuery:
    """面向数据湖的查询门面。"""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---------- 关系构造 ----------
    def relation_sql(self, asset_key: str, city_slug: str | None = None) -> str | None:
        """返回 ``read_parquet(...)`` 片段；无分区时返回 None。"""
        if city_slug:
            glob = layout.city_glob(asset_key, city_slug, self.settings)
            probe = layout.dataset_dir(asset_key, self.settings) / f"city={city_slug}"
        else:
            glob = layout.asset_glob(asset_key, self.settings)
            probe = layout.dataset_dir(asset_key, self.settings)
        if not probe.exists() or not any(probe.rglob(layout.PARTITION_FILENAME)):
            return None
        escaped = glob.replace("'", "''")
        return f"read_parquet('{escaped}', hive_partitioning=false)"

    def has_data(self, asset_key: str, city_slug: str | None = None) -> bool:
        return self.relation_sql(asset_key, city_slug) is not None

    # ---------- 通用取数 ----------
    def fetch(
        self,
        asset_key: str,
        *,
        city_slug: str | None = None,
        columns: Sequence[str] | None = None,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        time_column: str | None = None,
        where: str | None = None,
        order_by: str | None = None,
        limit: int | None = None,
        descending: bool = False,
    ) -> pd.DataFrame:
        """按条件读取资产数据为 DataFrame；无数据时返回空表。"""
        relation = self.relation_sql(asset_key, city_slug)
        if relation is None:
            return pd.DataFrame()

        if columns:
            invalid = [name for name in columns if not is_safe_identifier(name)]
            if invalid:
                raise LakeError(f"非法列名: {invalid}")
            projection = ", ".join(f'"{name}"' for name in columns)
        else:
            projection = "*"

        clauses: list[str] = []
        # 注意：当传入 city_slug 时，relation_sql 已通过分区 glob（city=<slug>/…）
        # 做了范围限定，此处不再重复过滤 city_id —— 参考层等资产并不含该列。
        if time_column:
            if not is_safe_identifier(time_column):
                raise LakeError(f"非法时间列名: {time_column}")
            if start is not None:
                clauses.append(f"\"{time_column}\" >= TIMESTAMP '{_as_timestamp(start)}'")
            if end is not None:
                clauses.append(f"\"{time_column}\" <= TIMESTAMP '{_as_timestamp(end)}'")
        if where:
            clauses.append(f"({where})")

        sql = f"SELECT {projection} FROM {relation}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by:
            if not is_safe_identifier(order_by):
                raise LakeError(f"非法排序列名: {order_by}")
            sql += f' ORDER BY "{order_by}" {"DESC" if descending else "ASC"}'
        if limit is not None:
            sql += f" LIMIT {int(limit)}"

        with duckdb_connection(self.settings) as connection:
            try:
                return connection.execute(sql).fetch_df()
            except duckdb.Error as exc:
                raise LakeError(f"查询资产 {asset_key} 失败: {exc}") from exc

    def execute(self, sql: str) -> pd.DataFrame:
        """执行自定义 SQL（供内部统计使用，不对外暴露）。"""
        with duckdb_connection(self.settings) as connection:
            try:
                return connection.execute(sql).fetch_df()
            except duckdb.Error as exc:
                raise LakeError(f"SQL 执行失败: {exc}") from exc

    def scalar(self, sql: str) -> Any:
        """执行 SQL 并返回首行首列。"""
        frame = self.execute(sql)
        if frame.empty:
            return None
        return frame.iloc[0, 0]

    # ---------- 便捷查询 ----------
    def latest_rows(
        self, asset_key: str, *, city_slug: str | None = None, limit: int = 24
    ) -> pd.DataFrame:
        """最近若干条记录（按时间倒序）。"""
        return self.fetch(
            asset_key,
            city_slug=city_slug,
            time_column="time" if self._has_time_column(asset_key, "time") else "date",
            order_by="time" if self._has_time_column(asset_key, "time") else "date",
            limit=limit,
            descending=True,
        )

    def _has_time_column(self, asset_key: str, name: str) -> bool:
        relation = self.relation_sql(asset_key)
        if relation is None:
            return name == "time"
        with duckdb_connection(self.settings) as connection:
            try:
                described = connection.execute(f"DESCRIBE SELECT * FROM {relation}").fetch_df()
            except duckdb.Error:
                return name == "time"
        return name in set(described["column_name"].tolist())

    def column_names(self, asset_key: str) -> list[str]:
        """读取资产的物理列名（用于与字段字典比对）。"""
        relation = self.relation_sql(asset_key)
        if relation is None:
            return []
        with duckdb_connection(self.settings) as connection:
            described = connection.execute(f"DESCRIBE SELECT * FROM {relation}").fetch_df()
        return described["column_name"].tolist()

    def schema(self, asset_key: str) -> dict[str, str]:
        """读取资产的物理列类型映射 ``{列名: DuckDB 类型}``。"""
        relation = self.relation_sql(asset_key)
        if relation is None:
            return {}
        with duckdb_connection(self.settings) as connection:
            described = connection.execute(f"DESCRIBE SELECT * FROM {relation}").fetch_df()
        return dict(zip(described["column_name"], described["column_type"], strict=False))

    def resolve_time_column(self, asset_key: str, relation: str | None = None) -> str | None:
        """判定资产的时间列名：优先 ``time``，其次 ``date``，都没有则返回 None。"""
        relation = relation or self.relation_sql(asset_key)
        if relation is None:
            return None
        with duckdb_connection(self.settings) as connection:
            described = connection.execute(f"DESCRIBE SELECT * FROM {relation}").fetch_df()
        names = set(described["column_name"].tolist())
        for candidate in ("time", "date"):
            if candidate in names:
                return candidate
        return None

    def columns_by_city(self, asset_key: str, *, time_column: str) -> dict[str, pd.DataFrame]:
        """按城市分别取数，供逐城市质量评测使用。"""
        relation = self.relation_sql(asset_key)
        if relation is None:
            return {}
        cities = [item["city"] for item in layout.list_partitions(asset_key, self.settings)]
        unique_cities = sorted({str(city) for city in cities})
        result: dict[str, pd.DataFrame] = {}
        for city in unique_cities:
            result[city] = self.fetch(asset_key, city_slug=city, time_column=time_column or None)
        return result

    def aggregate(
        self,
        asset_key: str,
        *,
        city_slug: str | None = None,
        time_column: str,
        aggregations: dict[str, str],
        group_by: str | None = None,
    ) -> pd.DataFrame:
        """按列生成聚合查询。``aggregations`` 形如 ``{'temperature_2m': 'avg'}``。"""
        relation = self.relation_sql(asset_key, city_slug)
        if relation is None:
            return pd.DataFrame()
        parts: list[str] = []
        if group_by:
            if not is_safe_identifier(group_by):
                raise LakeError(f"非法分组列: {group_by}")
            parts.append(f'"{group_by}"')
        for column, func_name in aggregations.items():
            if not is_safe_identifier(column) or not is_safe_identifier(func_name):
                raise LakeError(f"非法聚合表达式: {func_name}({column})")
            parts.append(f'{func_name}("{column}") AS "{func_name}_{column}"')
        sql = f"SELECT {', '.join(parts)} FROM {relation}"
        if city_slug:
            sql += f" WHERE city_id = '{city_slug}'"
        if group_by:
            sql += f' GROUP BY "{group_by}" ORDER BY "{group_by}"'
        return self.execute(sql)

    def environments(
        self,
        asset_key: str,
        *,
        city_slugs: Iterable[str] | None = None,
        columns: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        order_by: str | None = None,
        limit: int | None = None,
    ) -> pd.DataFrame:
        """跨城市取数（用于多城市对比与排名）。"""
        relation = self.relation_sql(asset_key)
        if relation is None:
            return pd.DataFrame()
        if columns:
            invalid = [name for name in columns if not is_safe_identifier(name)]
            if invalid:
                raise LakeError(f"非法列名: {invalid}")
            projection = ", ".join(f'"{name}"' for name in columns)
        else:
            projection = "*"

        clauses: list[str] = []
        if city_slugs:
            escaped = ", ".join(f"'{slug}'" for slug in city_slugs)
            clauses.append(f"city_id IN ({escaped})")
        # 时间列必须按**实际物理列**判定：逐日资产是 date，逐小时资产是 time。
        # 早期实现按投影列表猜测，未传 columns 时会把逐日资产当成 time 而报错。
        if start is not None or end is not None:
            time_column = self.resolve_time_column(asset_key, relation)
            if time_column is None:
                raise LakeError(f"资产 {asset_key} 没有可用于时间过滤的列（time / date）")
            if start is not None:
                clauses.append(f'"{time_column}" >= TIMESTAMP \'{_as_timestamp(start)}\'')
            if end is not None:
                clauses.append(f'"{time_column}" <= TIMESTAMP \'{_as_timestamp(end)}\'')

        sql = f"SELECT {projection} FROM {relation}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if order_by and is_safe_identifier(order_by):
            sql += f' ORDER BY "{order_by}"'
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.execute(sql)

    def dataset_row_count(self, asset_key: str, city_slug: str | None = None) -> int:
        """快速行数统计（city_slug 由分区 glob 限定）。"""
        relation = self.relation_sql(asset_key, city_slug)
        if relation is None:
            return 0
        value = self.scalar(f"SELECT COUNT(*) AS n FROM {relation}")
        return int(value or 0)


def _as_timestamp(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


__all__ = ["LakeQuery", "duckdb_connection", "is_safe_identifier"]
