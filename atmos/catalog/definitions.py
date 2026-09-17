"""资产定义的加载与校验。

把 ``config/assets.yaml``、``config/cities.yaml``、``config/quality_rules.yaml``
三份 YAML 解析为强类型 dataclass，并在加载时做完整性校验（引用是否存在、
主键是否在字段字典中、血缘两端是否已注册）。配置错误在启动期即暴露，
不留给运行期。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from atmos.errors import ConfigError
from atmos.logging_setup import get_logger
from atmos.settings import get_settings

logger = get_logger("catalog.definitions")

# ------------------------------------------------------------------
# 数据类型（数据标准中的允许取值）
# ------------------------------------------------------------------
DATA_TYPES = frozenset({"string", "double", "integer", "timestamp", "date", "boolean"})

_LAYERS = frozenset({"reference", "raw", "refined", "serving"})
_DOMAINS = frozenset({"weather", "air_quality", "integrated", "reference"})
_TRANSFORMS = frozenset({"reference", "derive", "join", "aggregate", "enrich"})


@dataclass(frozen=True)
class ColumnDefinition:
    """字段字典中的一列。"""

    name: str
    data_type: str
    unit: str | None = None
    description: str = ""
    value_range: tuple[float, float] | None = None
    enum_values: tuple[Any, ...] | None = None
    is_nullable: bool = True
    is_primary_key: bool = False
    is_pii: bool = False
    is_derived: bool = False
    ordinal: int = 0

    @property
    def arrow_type(self) -> str:
        return {
            "string": "string",
            "double": "double",
            "integer": "int64",
            "timestamp": "timestamp[us]",
            "date": "date32",
            "boolean": "bool",
        }[self.data_type]

    def within_range(self, value: float) -> bool:
        if self.value_range is None:
            return True
        low, high = self.value_range
        return low <= value <= high

    def to_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "data_type": self.data_type,
            "unit": self.unit,
            "description": self.description,
            "value_range": list(self.value_range) if self.value_range else None,
            "enum_values": list(self.enum_values) if self.enum_values else None,
            "is_nullable": self.is_nullable,
            "is_primary_key": self.is_primary_key,
            "is_pii": self.is_pii,
            "is_derived": self.is_derived,
            "ordinal": self.ordinal,
        }


@dataclass(frozen=True)
class AssetDefinition:
    """一项数据资产的完整定义。"""

    asset_key: str
    name: str
    domain: str
    layer: str
    description: str
    owner: str
    steward: str | None
    source_system: str
    granularity: str
    update_frequency: str
    columns: tuple[ColumnDefinition, ...]
    business_definition: str | None = None
    source_product: str | None = None
    source_endpoint: str | None = None
    source_doc: str | None = None
    license: str | None = None
    sla_freshness_minutes: int | None = None
    sla_completeness: float | None = None
    sensitivity: str = "public"
    status: str = "active"
    tags: tuple[str, ...] = ()
    primary_key: tuple[str, ...] = ()
    time_column: str | None = None
    partition_keys: tuple[str, ...] = ()

    def column(self, name: str) -> ColumnDefinition | None:
        for item in self.columns:
            if item.name == name:
                return item
        return None

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.columns)

    @property
    def ranged_columns(self) -> tuple[ColumnDefinition, ...]:
        return tuple(item for item in self.columns if item.value_range)

    @property
    def enum_columns(self) -> tuple[ColumnDefinition, ...]:
        return tuple(item for item in self.columns if item.enum_values)

    @property
    def data_columns(self) -> tuple[ColumnDefinition, ...]:
        """非平台审计列。"""
        internal = {"ingested_at", "source_run_id"}
        return tuple(item for item in self.columns if item.name not in internal)

    @property
    def storage_uri(self) -> str:
        return f"lake/{self.asset_key}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "asset_key": self.asset_key,
            "name": self.name,
            "domain": self.domain,
            "layer": self.layer,
            "description": self.description,
            "business_definition": self.business_definition,
            "owner": self.owner,
            "steward": self.steward,
            "source_system": self.source_system,
            "source_product": self.source_product,
            "source_endpoint": self.source_endpoint,
            "source_doc": self.source_doc,
            "license": self.license,
            "storage_uri": self.storage_uri,
            "granularity": self.granularity,
            "update_frequency": self.update_frequency,
            "sla_freshness_minutes": self.sla_freshness_minutes,
            "sla_completeness": self.sla_completeness,
            "sensitivity": self.sensitivity,
            "status": self.status,
            "tags": list(self.tags),
            "primary_key": list(self.primary_key),
            "time_column": self.time_column,
            "partition_keys": list(self.partition_keys),
        }


@dataclass(frozen=True)
class LineageDefinition:
    """一条血缘边定义。"""

    upstream: str
    downstream: str
    transform_type: str
    transform_ref: str | None = None
    description: str = ""


@dataclass(frozen=True)
class CityDefinition:
    """城市主数据定义。"""

    slug: str
    name_zh: str
    name_en: str
    country: str
    country_code: str
    latitude: float
    longitude: float
    timezone: str
    admin1: str | None = None
    elevation: float | None = None
    population: int | None = None
    climate_zone: str | None = None
    is_active: bool = True
    tags: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "name_zh": self.name_zh,
            "name_en": self.name_en,
            "country": self.country,
            "country_code": self.country_code,
            "admin1": self.admin1,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "timezone": self.timezone,
            "elevation": self.elevation,
            "population": self.population,
            "climate_zone": self.climate_zone,
            "is_active": self.is_active,
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class DimensionDefinition:
    """质量维度定义。"""

    key: str
    name: str
    weight: float
    description: str = ""


@dataclass(frozen=True)
class CheckDefinition:
    """质量检查项定义。"""

    key: str
    name: str
    dimension: str
    severity: str
    description: str = ""
    applies_to: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)

    def applies(self, asset_key: str) -> bool:
        return "*" in self.applies_to or asset_key in self.applies_to


@dataclass(frozen=True)
class QualityModel:
    """五维质量模型全量定义。"""

    version: str
    description: str
    dimensions: tuple[DimensionDefinition, ...]
    grading: tuple[dict[str, Any], ...]
    defaults: dict[str, Any]
    checks: tuple[CheckDefinition, ...]

    def weight_of(self, dimension: str) -> float:
        for item in self.dimensions:
            if item.key == dimension:
                return item.weight
        return 0.0

    def grade_for(self, score: float) -> dict[str, Any]:
        for band in self.grading:
            if score >= float(band["min_score"]):
                return band
        return {"grade": "E", "label": "待治理", "color": "#dc2626", "min_score": 0}

    def checks_for(self, asset_key: str) -> tuple[CheckDefinition, ...]:
        return tuple(check for check in self.checks if check.applies(asset_key))


# ==================================================================
# 加载
# ==================================================================
def _read_yaml(path: Path) -> Any:
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件 YAML 解析失败 {path}: {exc}") from exc


def _parse_column(raw: dict[str, Any], ordinal: int) -> ColumnDefinition:
    name = raw.get("name")
    if not name:
        raise ConfigError(f"字段字典缺少 name: {raw!r}")
    data_type = raw.get("type", "string")
    if data_type not in DATA_TYPES:
        raise ConfigError(f"字段 {name} 声明了不支持的类型 {data_type!r}")

    raw_range = raw.get("range")
    value_range: tuple[float, float] | None = None
    if raw_range is not None:
        if not isinstance(raw_range, (list, tuple)) or len(raw_range) != 2:
            raise ConfigError(f"字段 {name} 的 range 必须是 [min, max]")
        low, high = float(raw_range[0]), float(raw_range[1])
        if low > high:
            raise ConfigError(f"字段 {name} 的 range 下界大于上界: {raw_range!r}")
        value_range = (low, high)

    raw_enum = raw.get("enum")
    enum_values = tuple(raw_enum) if raw_enum else None

    return ColumnDefinition(
        name=str(name),
        data_type=data_type,
        unit=raw.get("unit"),
        description=str(raw.get("desc") or raw.get("description") or ""),
        value_range=value_range,
        enum_values=enum_values,
        is_nullable=bool(raw.get("nullable", True)),
        is_primary_key=bool(raw.get("pk", False)),
        is_pii=bool(raw.get("pii", False)),
        is_derived=bool(raw.get("is_derived", False)),
        ordinal=ordinal,
    )


def _parse_asset(raw: dict[str, Any], seen_pks: dict[str, str]) -> AssetDefinition:
    key = raw.get("key")
    if not key:
        raise ConfigError(f"资产定义缺少 key: {raw!r}")

    columns = tuple(
        _parse_column(item, index) for index, item in enumerate(raw.get("columns") or [])
    )
    if not columns:
        raise ConfigError(f"资产 {key} 未定义任何字段")

    duplicates = _duplicates([item.name for item in columns])
    if duplicates:
        raise ConfigError(f"资产 {key} 字段名重复: {duplicates}")

    layer = raw.get("layer", "raw")
    if layer not in _LAYERS:
        raise ConfigError(f"资产 {key} 的分层 {layer!r} 非法，应为 {sorted(_LAYERS)}")

    domain = raw.get("domain", "reference")
    if domain not in _DOMAINS:
        raise ConfigError(f"资产 {key} 的业务域 {domain!r} 非法，应为 {sorted(_DOMAINS)}")

    primary_key = tuple(raw.get("primary_key") or [])
    column_names = {item.name for item in columns}
    unknown_pk = [name for name in primary_key if name not in column_names]
    if unknown_pk:
        raise ConfigError(f"资产 {key} 的主键字段未在字段字典中定义: {unknown_pk}")

    time_column = raw.get("time_column")
    if time_column and time_column not in column_names:
        raise ConfigError(f"资产 {key} 的时间列 {time_column!r} 未在字段字典中定义")

    if primary_key:
        signature = tuple(sorted(primary_key))
        if signature in seen_pks:
            # 同一粒度上的多个资产共用主键是正常设计（如预报、归档、融合宽表
            # 都是 city × time），因此只作为设计提示记录在调试日志中。
            logger.debug(
                "资产 %s 与 %s 使用相同主键组合 %s",
                key,
                seen_pks[signature],
                list(signature),
            )
        seen_pks[signature] = str(key)

    return AssetDefinition(
        asset_key=str(key),
        name=str(raw.get("name") or key),
        domain=domain,
        layer=layer,
        description=str(raw.get("description") or "").strip(),
        business_definition=(raw.get("business_definition") or "").strip() or None,
        owner=str(raw.get("owner") or "未指派"),
        steward=raw.get("steward"),
        source_system=str(raw.get("source_system") or "未知"),
        source_product=raw.get("source_product"),
        source_endpoint=raw.get("source_endpoint"),
        source_doc=raw.get("source_doc"),
        license=raw.get("license"),
        granularity=str(raw.get("granularity") or "hourly"),
        update_frequency=str(raw.get("update_frequency") or "hourly"),
        sla_freshness_minutes=(
            int(raw["sla_freshness_minutes"]) if raw.get("sla_freshness_minutes") else None
        ),
        sla_completeness=(
            float(raw["sla_completeness"]) if raw.get("sla_completeness") is not None else None
        ),
        sensitivity=str(raw.get("sensitivity") or "public"),
        status=str(raw.get("status") or "active"),
        tags=tuple(raw.get("tags") or ()),
        primary_key=primary_key,
        time_column=time_column,
        partition_keys=tuple(raw.get("partition_keys") or ()),
        columns=columns,
    )


def _duplicates(items: list[str]) -> list[str]:
    seen: set[str] = set()
    dupes: list[str] = []
    for item in items:
        if item in seen and item not in dupes:
            dupes.append(item)
        seen.add(item)
    return dupes


def _parse_city(raw: dict[str, Any]) -> CityDefinition:
    slug = raw.get("slug")
    if not slug:
        raise ConfigError(f"城市定义缺少 slug: {raw!r}")
    try:
        latitude = float(raw["latitude"])
        longitude = float(raw["longitude"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ConfigError(f"城市 {slug} 的经纬度非法: {exc}") from exc
    if not -90 <= latitude <= 90:
        raise ConfigError(f"城市 {slug} 纬度越界: {latitude}")
    if not -180 <= longitude <= 180:
        raise ConfigError(f"城市 {slug} 经度越界: {longitude}")
    return CityDefinition(
        slug=str(slug),
        name_zh=str(raw.get("name_zh") or slug),
        name_en=str(raw.get("name_en") or slug),
        country=str(raw.get("country") or "未知"),
        country_code=str(raw.get("country_code") or "XX"),
        admin1=raw.get("admin1"),
        latitude=latitude,
        longitude=longitude,
        timezone=str(raw.get("timezone") or "UTC"),
        elevation=float(raw["elevation"]) if raw.get("elevation") is not None else None,
        population=int(raw["population"]) if raw.get("population") is not None else None,
        climate_zone=raw.get("climate_zone"),
        is_active=bool(raw.get("is_active", True)),
        tags=tuple(raw.get("tags") or ()),
    )


class AssetRegistry:
    """已加载并交叉校验的全部平台元数据。"""

    def __init__(
        self,
        assets: tuple[AssetDefinition, ...],
        lineage: tuple[LineageDefinition, ...],
        cities: tuple[CityDefinition, ...],
        quality: QualityModel,
    ) -> None:
        self.assets = assets
        self.lineage = lineage
        self.cities = cities
        self.quality = quality
        self._by_key = {asset.asset_key: asset for asset in assets}
        self._city_by_slug = {city.slug: city for city in cities}

    # ---------- 查询 ----------
    def asset(self, asset_key: str) -> AssetDefinition:
        try:
            return self._by_key[asset_key]
        except KeyError as exc:
            raise ConfigError(f"未注册的数据资产: {asset_key}") from exc

    def has_asset(self, asset_key: str) -> bool:
        return asset_key in self._by_key

    def city(self, slug: str) -> CityDefinition:
        try:
            return self._city_by_slug[slug]
        except KeyError as exc:
            raise ConfigError(f"未注册的城市: {slug}") from exc

    @property
    def active_cities(self) -> tuple[CityDefinition, ...]:
        return tuple(city for city in self.cities if city.is_active)

    def upstream_of(self, asset_key: str) -> tuple[str, ...]:
        return tuple(edge.upstream for edge in self.lineage if edge.downstream == asset_key)

    def downstream_of(self, asset_key: str) -> tuple[str, ...]:
        return tuple(edge.downstream for edge in self.lineage if edge.upstream == asset_key)

    def assets_in_layer(self, layer: str) -> tuple[AssetDefinition, ...]:
        return tuple(asset for asset in self.assets if asset.layer == layer)


def _validate_lineage(
    lineage: tuple[LineageDefinition, ...], assets: tuple[AssetDefinition, ...]
) -> None:
    known = {asset.asset_key for asset in assets}
    problems: list[str] = []
    for edge in lineage:
        if edge.upstream not in known:
            problems.append(f"血缘上游资产未注册: {edge.upstream}")
        if edge.downstream not in known:
            problems.append(f"血缘下游资产未注册: {edge.downstream}")
        if edge.transform_type not in _TRANSFORMS:
            problems.append(f"血缘转换类型非法: {edge.transform_type}")
    if problems:
        raise ConfigError("血缘定义校验失败:\n  - " + "\n  - ".join(problems))


def _parse_quality(raw: dict[str, Any]) -> QualityModel:
    model_raw = raw.get("model") or {}
    dimensions = tuple(
        DimensionDefinition(
            key=str(item["key"]),
            name=str(item.get("name") or item["key"]),
            weight=float(item.get("weight") or 0.0),
            description=str(item.get("description") or ""),
        )
        for item in raw.get("dimensions") or []
    )
    if not dimensions:
        raise ConfigError("质量模型未定义任何维度")

    total_weight = sum(item.weight for item in dimensions)
    if abs(total_weight - 1.0) > 1e-6:
        raise ConfigError(f"质量维度权重之和必须为 1.0，当前为 {total_weight}")

    checks = tuple(
        CheckDefinition(
            key=str(item["key"]),
            name=str(item.get("name") or item["key"]),
            dimension=str(item["dimension"]),
            severity=str(item.get("severity") or "minor"),
            description=str(item.get("description") or "").strip(),
            applies_to=tuple(item.get("applies_to") or ("*",)),
            params=dict(item.get("params") or {}),
        )
        for item in raw.get("checks") or []
    )

    known_dimensions = {item.key for item in dimensions}
    for check in checks:
        if check.dimension not in known_dimensions:
            raise ConfigError(f"检查项 {check.key} 引用了未定义的维度 {check.dimension}")

    grading = tuple(sorted(raw.get("grading") or [], key=lambda b: -float(b["min_score"])))
    if not grading:
        raise ConfigError("质量模型未定义等级映射")

    return QualityModel(
        version=str(model_raw.get("version") or "1.0"),
        description=str(model_raw.get("description") or ""),
        dimensions=dimensions,
        grading=grading,
        defaults=dict(raw.get("defaults") or {}),
        checks=checks,
    )


@lru_cache(maxsize=1)
def load_registry() -> AssetRegistry:
    """加载并缓存全部平台元数据定义。"""
    settings = get_settings()
    config_dir = settings.config_path

    assets_raw = _read_yaml(config_dir / "assets.yaml") or {}
    cities_raw = _read_yaml(config_dir / "cities.yaml") or []
    quality_raw = _read_yaml(config_dir / "quality_rules.yaml") or {}

    seen_pks: dict[str, str] = {}
    assets = tuple(_parse_asset(item, seen_pks) for item in assets_raw.get("assets") or [])
    if not assets:
        raise ConfigError("assets.yaml 未定义任何数据资产")

    lineage = tuple(
        LineageDefinition(
            upstream=str(item["upstream"]),
            downstream=str(item["downstream"]),
            transform_type=str(item.get("transform_type") or "derive"),
            transform_ref=item.get("transform_ref"),
            description=str(item.get("description") or "").strip(),
        )
        for item in assets_raw.get("lineage") or []
    )
    _validate_lineage(lineage, assets)

    cities = tuple(_parse_city(item) for item in cities_raw)
    city_dupes = _duplicates([city.slug for city in cities])
    if city_dupes:
        raise ConfigError(f"城市 slug 重复: {city_dupes}")

    quality = _parse_quality(quality_raw)

    registry = AssetRegistry(assets, lineage, cities, quality)
    logger.info(
        "元数据加载完成：%d 项资产 / %d 条血缘 / %d 个城市 / %d 条质量检查",
        len(assets),
        len(lineage),
        len(cities),
        len(quality.checks),
    )
    return registry


def reload_registry() -> AssetRegistry:
    """清除缓存并重新加载（供 API 热更新配置）。"""
    load_registry.cache_clear()
    return load_registry()


__all__ = [
    "ColumnDefinition",
    "AssetDefinition",
    "LineageDefinition",
    "CityDefinition",
    "DimensionDefinition",
    "CheckDefinition",
    "QualityModel",
    "AssetRegistry",
    "load_registry",
    "reload_registry",
    "DATA_TYPES",
]
