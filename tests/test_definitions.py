"""元数据定义加载与校验测试。"""

from __future__ import annotations

import pytest

from atmos.catalog.definitions import load_registry
from atmos.errors import ConfigError
from atmos.sources.specs import ALL_ASSET_KEYS


def test_registry_loads_all_assets(registry) -> None:
    """配置中声明的资产应全部加载，且键与代码常量一致。"""
    keys = {asset.asset_key for asset in registry.assets}
    assert keys == set(ALL_ASSET_KEYS)


def test_platform_covers_domestic_cities_only(registry) -> None:
    """平台只覆盖国内城市，且武汉必须在可选城市中。"""
    countries = {city.country for city in registry.cities}
    assert countries == {"中国"}, f"出现境外城市: {sorted(countries)}"
    assert {city.country_code for city in registry.cities} == {"CN"}

    slugs = {city.slug for city in registry.cities}
    assert "wuhan" in slugs, "武汉必须包含在可选城市中"
    wuhan = registry.city("wuhan")
    assert wuhan.name_zh == "武汉"
    assert wuhan.admin1 == "湖北省"
    assert 29 < wuhan.latitude < 32 and 113 < wuhan.longitude < 116
    assert wuhan.timezone == "Asia/Shanghai"
    assert wuhan.is_active


def test_city_timezones_are_valid_iana_names(registry) -> None:
    """国内城市的 IANA 时区必须可解析，否则本地时间窗口会失真。"""
    from zoneinfo import ZoneInfo

    for city in registry.cities:
        ZoneInfo(city.timezone)  # 非法时区会抛 ZoneInfoNotFoundError


def test_every_asset_has_governance_metadata(registry) -> None:
    """每项资产都必须具备责任、契约与字段说明 —— 这是"资产化"的底线。"""
    for asset in registry.assets:
        assert asset.name, asset.asset_key
        assert asset.owner, asset.asset_key
        assert asset.steward, asset.asset_key
        assert asset.description, asset.asset_key
        assert asset.business_definition, asset.asset_key
        assert asset.source_system, asset.asset_key
        assert asset.sla_freshness_minutes, asset.asset_key
        assert asset.tags, asset.asset_key


def test_primary_keys_are_declared_columns(registry) -> None:
    """主键字段必须存在于字段字典中，且被标记为 pk。"""
    for asset in registry.assets:
        for name in asset.primary_key:
            column = asset.column(name)
            assert column is not None, f"{asset.asset_key}.{name}"
            assert column.is_primary_key, f"{asset.asset_key}.{name} 未标记 pk"


def test_every_column_has_description_and_unit(registry) -> None:
    """字段字典必须写明业务含义；数值列必须带单位。"""
    for asset in registry.assets:
        for column in asset.columns:
            assert column.description, f"{asset.asset_key}.{column.name} 缺少字段说明"
            if column.data_type in {"double", "integer"} and column.name not in {"is_day"}:
                assert column.unit, f"{asset.asset_key}.{column.name} 缺少单位"


def test_value_ranges_are_ordered(registry) -> None:
    """值域下界不得大于上界。"""
    for asset in registry.assets:
        for column in asset.ranged_columns:
            low, high = column.value_range or (0, 0)
            assert low <= high, f"{asset.asset_key}.{column.name}"


def test_quality_dimension_weights_sum_to_one(registry) -> None:
    """五维权重必须归一。"""
    total = sum(item.weight for item in registry.quality.dimensions)
    assert abs(total - 1.0) < 1e-9
    assert len(registry.quality.dimensions) == 5


def test_quality_grading_is_descending(registry) -> None:
    """等级阈值必须按降序排列，且覆盖 0 分。"""
    scores = [float(item["min_score"]) for item in registry.quality.grading]
    assert scores == sorted(scores, reverse=True)
    assert scores[-1] == 0
    assert registry.quality.grade_for(100)["grade"] == "A"
    assert registry.quality.grade_for(0)["grade"] == "E"


def test_lineage_is_acyclic_and_references_known_assets(registry) -> None:
    """血缘必须是已知资产之间的有向无环图。"""
    known = {asset.asset_key for asset in registry.assets}
    adjacency: dict[str, list[str]] = {}
    for edge in registry.lineage:
        assert edge.upstream in known, edge.upstream
        assert edge.downstream in known, edge.downstream
        assert edge.description, f"{edge.upstream} → {edge.downstream} 缺少转换说明"
        adjacency.setdefault(edge.upstream, []).append(edge.downstream)

    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> None:
        assert node not in visiting, f"血缘存在环: {node}"
        if node in visited:
            return
        visiting.add(node)
        for child in adjacency.get(node, []):
            walk(child)
        visiting.discard(node)
        visited.add(node)

    for key in known:
        walk(key)


def test_every_check_key_has_an_implementation(registry) -> None:
    """规则目录中声明的检查项必须都有代码实现，避免静默失效。"""
    from atmos.quality.checks import registered_keys

    implemented = set(registered_keys())
    declared = {check.key for check in registry.quality.checks}
    assert declared <= implemented, f"缺少实现: {sorted(declared - implemented)}"
    assert implemented <= declared, f"存在无规则定义的实现: {sorted(implemented - declared)}"


def test_invalid_layer_is_rejected(tmp_path) -> None:
    """非法的资产分层必须在加载期报错，而不是带病运行。"""
    import yaml

    config = tmp_path / "cfg"
    config.mkdir()
    (config / "assets.yaml").write_text(
        yaml.safe_dump(
            {
                "assets": [
                    {
                        "key": "x.y",
                        "name": "X",
                        "domain": "weather",
                        "layer": "silver",
                        "owner": "o",
                        "source_system": "s",
                        "granularity": "hourly",
                        "update_frequency": "hourly",
                        "columns": [{"name": "time", "type": "timestamp", "nullable": False}],
                    }
                ]
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (config / "cities.yaml").write_text("[]", encoding="utf-8")
    (config / "quality_rules.yaml").write_text(
        yaml.safe_dump(
            {
                "model": {"version": "1.0"},
                "dimensions": [{"key": "validity", "name": "有效性", "weight": 1.0}],
                "grading": [{"min_score": 0, "grade": "E", "label": "待治理", "color": "#000"}],
                "checks": [],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    import os

    previous = os.environ.get("ATMOS_CONFIG_DIR")
    os.environ["ATMOS_CONFIG_DIR"] = str(config)
    try:
        from atmos.catalog.definitions import load_registry as reload
        from atmos.settings import reload_settings

        reload_settings()
        reload.cache_clear()
        with pytest.raises(ConfigError, match="分层"):
            reload()
    finally:
        if previous is None:
            os.environ.pop("ATMOS_CONFIG_DIR", None)
        else:
            os.environ["ATMOS_CONFIG_DIR"] = previous
        from atmos.settings import reload_settings

        reload_settings()
        load_registry.cache_clear()
