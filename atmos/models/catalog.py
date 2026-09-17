"""资产目录（Catalog）关系模型。

设计要点
--------
1. **元数据与数据分离**：本模块只描述"资产是什么、归谁管、质量如何"，
   真实数据存放在 Parquet 数据湖中，通过 ``storage_uri`` 关联。
2. **字段字典一等公民**：``asset_columns`` 记录每个字段的类型、单位、值域、
   业务口径，质量引擎的有效性校验直接消费该表，避免口径散落在代码里。
3. **血缘显式建模**：``lineage_edges`` 为有向图边，支持任意深度的上下游追溯。
4. **时间统一**：全部审计时间戳以 **朴素 UTC** 存储（SQLite 不保留时区），
   序列化时由 ``atmos.utils.iso_z`` 补 ``Z`` 后缀。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """当前 UTC 时间（朴素，秒级截断以便比较与展示）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


class Base(DeclarativeBase):
    """SQLAlchemy 声明式基类。"""

    type_annotation_map = {dict[str, Any]: JSON, list[str]: JSON}


# ============================================================
# 城市主数据
# ============================================================
class City(Base):
    """城市主数据 —— 平台监测点位的权威来源。"""

    __tablename__ = "cities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name_zh: Mapped[str] = mapped_column(String(64))
    name_en: Mapped[str] = mapped_column(String(96))
    country: Mapped[str] = mapped_column(String(64))
    country_code: Mapped[str] = mapped_column(String(8))
    admin1: Mapped[str | None] = mapped_column(String(96), nullable=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    timezone: Mapped[str] = mapped_column(String(64))
    elevation: Mapped[float | None] = mapped_column(Float, nullable=True)
    population: Mapped[int | None] = mapped_column(Integer, nullable=True)
    climate_zone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    # 地理编码原始响应的摘要，保留血缘证据
    geocoding_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    runs: Mapped[list["CollectionRun"]] = relationship(back_populates="city")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<City {self.slug} {self.name_zh}>"

    @property
    def coordinates(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


# ============================================================
# 数据资产
# ============================================================
class DataAsset(Base):
    """数据资产 —— 平台治理的基本单元。"""

    __tablename__ = "data_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    domain: Mapped[str] = mapped_column(String(32), index=True)
    layer: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    business_definition: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 归属与责任
    owner: Mapped[str] = mapped_column(String(96))
    steward: Mapped[str | None] = mapped_column(String(96), nullable=True)

    # 来源
    source_system: Mapped[str] = mapped_column(String(64))
    source_product: Mapped[str | None] = mapped_column(String(96), nullable=True)
    source_endpoint: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_doc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    license: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 存储与结构
    storage_uri: Mapped[str] = mapped_column(String(512), default="")
    storage_format: Mapped[str] = mapped_column(String(32), default="parquet")
    partition_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    primary_key: Mapped[list[str]] = mapped_column(JSON, default=list)
    time_column: Mapped[str | None] = mapped_column(String(64), nullable=True)
    schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 治理契约
    granularity: Mapped[str] = mapped_column(String(32))
    update_frequency: Mapped[str] = mapped_column(String(32))
    sla_freshness_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sla_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)
    sensitivity: Mapped[str] = mapped_column(String(32), default="public")
    status: Mapped[str] = mapped_column(String(32), default="active", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)

    # 统计（由采集与质量作业滚动维护，供目录列表页快速展示）
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    city_count: Mapped[int] = mapped_column(Integer, default=0)
    latest_data_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latest_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    latest_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_grade: Mapped[str | None] = mapped_column(String(4), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    columns: Mapped[list["AssetColumn"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan", order_by="AssetColumn.ordinal"
    )
    versions: Mapped[list["AssetVersion"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )
    runs: Mapped[list["CollectionRun"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )
    checks: Mapped[list["QualityCheck"]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DataAsset {self.asset_key}>"


class AssetColumn(Base):
    """字段字典 —— 资产的列级元数据与数据标准。"""

    __tablename__ = "asset_columns"
    __table_args__ = (UniqueConstraint("asset_id", "name", name="uq_asset_column"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(96))
    data_type: Mapped[str] = mapped_column(String(32))
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    # 数据标准：值域与枚举约束
    value_range: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    enum_values: Mapped[list[Any] | None] = mapped_column(JSON, nullable=True)
    is_nullable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_primary_key: Mapped[bool] = mapped_column(Boolean, default=False)
    is_pii: Mapped[bool] = mapped_column(Boolean, default=False)
    is_derived: Mapped[bool] = mapped_column(Boolean, default=False)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)

    asset: Mapped[DataAsset] = relationship(back_populates="columns")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AssetColumn {self.name}:{self.data_type}>"


class AssetVersion(Base):
    """资产版本快照 —— 每次落湖产生的分区统计与体积记录。"""

    __tablename__ = "asset_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    version: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    city_count: Mapped[int] = mapped_column(Integer, default=0)
    schema_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    partition_stats: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    asset: Mapped[DataAsset] = relationship(back_populates="versions")


class LineageEdge(Base):
    """血缘边 —— 上游资产到下游资产的转换关系。"""

    __tablename__ = "lineage_edges"
    __table_args__ = (
        UniqueConstraint("upstream_asset_id", "downstream_asset_id", "transform_ref", name="uq_lineage_edge"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    upstream_asset_id: Mapped[int] = mapped_column(
        ForeignKey("data_assets.id", ondelete="CASCADE"), index=True
    )
    downstream_asset_id: Mapped[int] = mapped_column(
        ForeignKey("data_assets.id", ondelete="CASCADE"), index=True
    )
    transform_type: Mapped[str] = mapped_column(String(32))
    transform_ref: Mapped[str | None] = mapped_column(String(96), nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    upstream: Mapped[DataAsset] = relationship(foreign_keys=[upstream_asset_id])
    downstream: Mapped[DataAsset] = relationship(foreign_keys=[downstream_asset_id])


class CollectionRun(Base):
    """采集运行记录 —— 数据可观测性的最小单元。"""

    __tablename__ = "collection_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    job: Mapped[str] = mapped_column(String(96), index=True)
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("data_assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    trigger: Mapped[str] = mapped_column(String(24), default="manual")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_written: Mapped[int] = mapped_column(Integer, default=0)
    rows_read: Mapped[int] = mapped_column(Integer, default=0)
    bytes_written: Mapped[int] = mapped_column(Integer, default=0)
    api_calls: Mapped[int] = mapped_column(Integer, default=0)
    partitions_written: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(96), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    asset: Mapped[DataAsset | None] = relationship(back_populates="runs")
    city: Mapped[City | None] = relationship(back_populates="runs")

    @property
    def duration_seconds(self) -> float | None:
        return None if self.duration_ms is None else round(self.duration_ms / 1000.0, 3)


class QualityCheck(Base):
    """质量检查项定义 —— 从 quality_rules.yaml 同步而来。"""

    __tablename__ = "quality_checks"
    __table_args__ = (UniqueConstraint("asset_id", "check_key", name="uq_asset_check"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    check_key: Mapped[str] = mapped_column(String(96), index=True)
    name: Mapped[str] = mapped_column(String(160))
    dimension: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    description: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    asset: Mapped[DataAsset] = relationship(back_populates="checks")


class QualityResult(Base):
    """质量检查结果 —— 单次评测的逐项明细。"""

    __tablename__ = "quality_results"
    __table_args__ = (
        Index("ix_quality_result_lookup", "asset_id", "city_id", "evaluated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    check_id: Mapped[int | None] = mapped_column(
        ForeignKey("quality_checks.id", ondelete="CASCADE"), nullable=True, index=True
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    check_key: Mapped[str] = mapped_column(String(96), index=True)
    dimension: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    observed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    expected_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    records_checked: Mapped[int] = mapped_column(Integer, default=0)
    records_failed: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class AssetQualityScore(Base):
    """资产质量综合评分 —— 五维得分与总评等级。"""

    __tablename__ = "asset_quality_scores"
    __table_args__ = (
        Index("ix_quality_score_lookup", "asset_id", "city_id", "scored_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("data_assets.id", ondelete="CASCADE"), index=True)
    city_id: Mapped[int | None] = mapped_column(
        ForeignKey("cities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    completeness: Mapped[float] = mapped_column(Float, default=0.0)
    timeliness: Mapped[float] = mapped_column(Float, default=0.0)
    validity: Mapped[float] = mapped_column(Float, default=0.0)
    consistency: Mapped[float] = mapped_column(Float, default=0.0)
    uniqueness: Mapped[float] = mapped_column(Float, default=0.0)
    overall: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    grade: Mapped[str] = mapped_column(String(4), default="E")
    rules_version: Mapped[str] = mapped_column(String(16), default="1.0")
    checks_total: Mapped[int] = mapped_column(Integer, default=0)
    checks_failed: Mapped[int] = mapped_column(Integer, default=0)
    checks_warned: Mapped[int] = mapped_column(Integer, default=0)
    window_hours: Mapped[int] = mapped_column(Integer, default=168)
    scored_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    asset: Mapped[DataAsset] = relationship()
    city: Mapped[City | None] = relationship()


# ============================================================
# 平台运行元信息
# ============================================================
class CatalogMeta(Base):
    """目录自身的元信息（引导时间、规则版本等）。"""

    __tablename__ = "catalog_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


__all__ = [
    "Base",
    "utcnow",
    "City",
    "DataAsset",
    "AssetColumn",
    "AssetVersion",
    "LineageEdge",
    "CollectionRun",
    "QualityCheck",
    "QualityResult",
    "AssetQualityScore",
    "CatalogMeta",
]
