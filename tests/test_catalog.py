"""资产目录与血缘测试。"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from atmos.catalog.bootstrap import bootstrap_catalog
from atmos.catalog.definitions import CityDefinition
from atmos.catalog.lineage import build_graph, orphan_report, trace
from atmos.catalog.service import (
    get_asset_detail,
    governance_report,
    list_assets,
    list_cities,
    overview,
)
from atmos.db import session_scope
from atmos.lake.writer import LakeWriter
from atmos.models import AssetColumn, City, DataAsset, LineageEdge, QualityCheck
from atmos.sources.specs import (
    ASSET_AIR_QUALITY_HOURLY,
    ASSET_ENVIRONMENT_HOURLY,
    ASSET_FORECAST_HOURLY,
    ASSET_GEOCODING_PLACES,
)
from tests.conftest import air_rows, forecast_rows


# ==================================================================
# 引导
# ==================================================================
def test_bootstrap_populates_catalog(workspace, registry) -> None:
    """引导后目录中应包含全部资产、字段、血缘与检查项。"""
    with session_scope() as session:
        assert session.scalar(select(func.count(DataAsset.id))) == len(registry.assets)
        assert session.scalar(select(func.count(City.id))) == len(registry.cities)
        assert session.scalar(select(func.count(LineageEdge.id))) == len(registry.lineage)
        expected_columns = sum(len(asset.columns) for asset in registry.assets)
        assert session.scalar(select(func.count(AssetColumn.id))) == expected_columns
        assert session.scalar(select(func.count(QualityCheck.id))) > 0


def test_bootstrap_is_idempotent(workspace, registry) -> None:
    """重复引导不应产生重复记录。"""
    with session_scope() as session:
        before = (
            session.scalar(select(func.count(DataAsset.id))),
            session.scalar(select(func.count(AssetColumn.id))),
            session.scalar(select(func.count(LineageEdge.id))),
            session.scalar(select(func.count(QualityCheck.id))),
        )

    for _ in range(2):
        report = bootstrap_catalog(registry)

    assert report.assets_created == 0
    assert report.cities_created == 0
    assert report.edges_created == 0
    assert report.checks_created == 0

    with session_scope() as session:
        after = (
            session.scalar(select(func.count(DataAsset.id))),
            session.scalar(select(func.count(AssetColumn.id))),
            session.scalar(select(func.count(LineageEdge.id))),
            session.scalar(select(func.count(QualityCheck.id))),
        )
    assert before == after


def test_checks_are_expanded_per_asset(workspace, registry) -> None:
    """通配检查项应展开到每一项资产，且带具体参数的检查项正确归属。"""
    with session_scope() as session:
        asset = session.scalar(
            select(DataAsset).where(DataAsset.asset_key == ASSET_AIR_QUALITY_HOURLY)
        )
        assert asset is not None
        keys = {check.check_key for check in asset.checks}
    assert "completeness.time_coverage" in keys
    assert "uniqueness.primary_key" in keys
    assert "consistency.pm25_le_pm10" in keys
    # 不适用于空气质量资产的检查不应出现
    assert "consistency.rain_le_precipitation" not in keys


# ==================================================================
# 城市主数据的配置投影
# ==================================================================
def _extra_city(slug: str) -> CityDefinition:
    """构造一个不在 config/cities.yaml 中的城市定义（模拟被移除的城市）。"""
    return CityDefinition(
        slug=slug,
        name_zh="东京",
        name_en="Tokyo",
        country="日本",
        country_code="JP",
        latitude=35.6762,
        longitude=139.6503,
        timezone="Asia/Tokyo",
    )


def _with_city(registry, *definitions):
    """返回追加了若干城市定义的注册表副本。"""
    from atmos.catalog.definitions import AssetRegistry

    return AssetRegistry(
        registry.assets, registry.lineage, registry.cities + tuple(definitions), registry.quality
    )


def test_bootstrap_deactivates_cities_removed_from_config(workspace, registry) -> None:
    """配置中移除的城市应被停用而非删除，以保持引用完整性。"""
    extra = _extra_city("tokyo")
    bootstrap_catalog(_with_city(registry, extra))
    with session_scope() as session:
        assert session.scalar(select(City).where(City.slug == "tokyo")) is not None

    report = bootstrap_catalog(registry)
    assert report.cities_deactivated == 1

    with session_scope() as session:
        row = session.scalar(select(City).where(City.slug == "tokyo"))
    assert row is not None, "停用不应删除主数据行"
    assert row.is_active is False


def test_readding_city_reactivates_it(workspace, registry) -> None:
    """重新加入配置的城市应自动恢复启用，无需人工干预。"""
    extra = _extra_city("tokyo")
    extended = _with_city(registry, extra)

    bootstrap_catalog(extended)
    assert bootstrap_catalog(registry).cities_deactivated == 1

    report = bootstrap_catalog(extended)
    assert report.cities_created == 0
    assert report.cities_updated >= 1
    with session_scope() as session:
        row = session.scalar(select(City).where(City.slug == "tokyo"))
    assert row is not None
    assert row.is_active is True


def test_bootstrap_is_stable_after_deactivation(workspace, registry) -> None:
    """停用是幂等的：再次引导不应反复计数或改变状态。"""
    bootstrap_catalog(_with_city(registry, _extra_city("tokyo")))
    assert bootstrap_catalog(registry).cities_deactivated == 1
    assert bootstrap_catalog(registry).cities_deactivated == 0


# ==================================================================
# 查询服务
# ==================================================================
def test_list_assets_and_filters(workspace) -> None:
    """资产列表应支持按分层与关键字过滤。"""
    with session_scope() as session:
        all_items = list_assets(session)
        raw_items = list_assets(session, layer="raw")
        searched = list_assets(session, search="pm2_5")

    assert len(all_items) == 7
    assert len(raw_items) == 4
    assert all(item["layer"] == "raw" for item in raw_items)
    assert searched, "按字段名搜索应能命中资产"
    assert ASSET_AIR_QUALITY_HOURLY in {item["key"] for item in searched}


def test_asset_detail_exposes_field_dictionary(workspace) -> None:
    """资产详情应包含完整字段字典与治理契约。"""
    with session_scope() as session:
        detail = get_asset_detail(session, ASSET_FORECAST_HOURLY)

    assert detail is not None
    assert detail["owner"]
    assert detail["sla_freshness_minutes"] == 120
    assert detail["primary_key"] == ["city_id", "time"]
    assert detail["time_column"] == "time"
    assert detail["partition_keys"] == ["city_id", "year", "month"]
    assert len(detail["columns"]) >= 20

    temperature = next(item for item in detail["columns"] if item["name"] == "temperature_2m")
    assert temperature["unit"] == "°C"
    assert temperature["value_range"] == [-90.0, 60.0]
    assert temperature["description"]

    weather_code = next(item for item in detail["columns"] if item["name"] == "weather_code")
    assert 61 in weather_code["enum_values"]


def test_asset_detail_unknown_key(workspace) -> None:
    with session_scope() as session:
        assert get_asset_detail(session, "does.not.exist") is None


def test_cities_are_listed_with_coordinates(workspace) -> None:
    with session_scope() as session:
        items = list_cities(session)

    assert len(items) == 15
    beijing = next(item for item in items if item["slug"] == "beijing")
    assert beijing["timezone"] == "Asia/Shanghai"
    assert 39 < beijing["latitude"] < 41
    wuhan = next(item for item in items if item["slug"] == "wuhan")
    assert wuhan["name_zh"] == "武汉"
    assert wuhan["admin1"] == "湖北省"
    assert all(item["has_data"] is False for item in items)


# ==================================================================
# 总览与治理
# ==================================================================
def test_overview_reports_zero_state(workspace) -> None:
    """尚无数据时总览应给出合理的零值且不报错。"""
    with session_scope() as session:
        data = overview(session)

    assert data["asset_count"] == 7
    assert data["city_count"] == 15
    assert data["active_city_count"] == 15
    assert data["total_rows"] == 0
    assert data["avg_quality_score"] is None
    assert data["sla_breach_count"] >= 7  # 全部资产尚无数据，均视为超期
    assert data["runs_24h"]["total"] == 0
    assert data["meta"]["rules_version"]


def test_governance_report_flags_missing_steward_and_sla(workspace) -> None:
    """治理体检应能识别元数据缺口。"""
    with session_scope() as session:
        report = governance_report(session)

    assert 0 <= report["score"] <= 100
    assert report["total_checks"] == len(report["checks"])
    keys = {item["key"] for item in report["checks"]}
    assert "responsibility.owner" in keys
    assert "contract.sla" in keys
    assert "documentation.columns" in keys
    # 平台自带配置的责任人与 SLA 应当齐备
    owner_check = next(item for item in report["checks"] if item["key"] == "responsibility.owner")
    assert owner_check["passed"], owner_check["affected"]


# ==================================================================
# 血缘
# ==================================================================
def test_graph_structure(workspace, registry) -> None:
    """血缘图应包含全部节点与边，并计算正确的拓扑深度。"""
    with session_scope() as session:
        graph = build_graph(session)

    assert len(graph["nodes"]) == len(registry.assets)
    assert len(graph["edges"]) == len(registry.lineage)
    depths = {node["key"]: node["depth"] for node in graph["nodes"]}
    assert depths[ASSET_GEOCODING_PLACES] == 0
    assert depths[ASSET_FORECAST_HOURLY] == 1
    assert depths[ASSET_ENVIRONMENT_HOURLY] == 2
    assert depths["atmos.city_environment.daily"] == 3
    assert graph["stats"]["max_depth"] == 3
    assert graph["stats"]["isolated_count"] == 0
    assert graph["stats"]["edge_count"] == 8


def test_trace_downstream_impact(workspace) -> None:
    """影响分析：天气预报出问题会波及哪些下游资产。"""
    with session_scope() as session:
        result = trace(session, ASSET_FORECAST_HOURLY, direction="downstream")

    assert result["found"]
    impacted = set(result["summary"]["related_assets"])
    assert ASSET_ENVIRONMENT_HOURLY in impacted
    assert "atmos.city_environment.daily" in impacted
    assert result["summary"]["depth_reached"] == 2


def test_trace_upstream_root_cause(workspace) -> None:
    """根因分析：日汇总资产的完整上游链路。"""
    with session_scope() as session:
        result = trace(session, "atmos.city_environment.daily", direction="upstream")

    assert result["found"]
    ancestors = set(result["summary"]["related_assets"])
    assert ancestors == {
        ASSET_ENVIRONMENT_HOURLY,
        ASSET_FORECAST_HOURLY,
        ASSET_AIR_QUALITY_HOURLY,
        "openmeteo.weather.archive.hourly",
        ASSET_GEOCODING_PLACES,
    }
    assert result["summary"]["depth_reached"] == 3


def test_trace_unknown_asset(workspace) -> None:
    with session_scope() as session:
        result = trace(session, "nope")
    assert result["found"] is False


def test_orphan_report_is_clean(workspace) -> None:
    """平台自带血缘应当完整且有说明。"""
    with session_scope() as session:
        report = orphan_report(session)
    assert report["healthy"], report
    assert report["isolated_assets"] == []
    assert report["edges_without_documentation"] == []


def test_graph_reflects_loaded_data(workspace, registry) -> None:
    """落湖并刷新统计后，血缘节点应带出真实行数。"""
    from atmos.catalog.stats import refresh_asset_stats
    from atmos.sources.specs import ASSET_ENVIRONMENT_DAILY

    writer = LakeWriter()
    writer.write(registry.asset(ASSET_FORECAST_HOURLY), "beijing", forecast_rows(6), run_id="run-1")
    writer.write(registry.asset(ASSET_AIR_QUALITY_HOURLY), "beijing", air_rows(6), run_id="run-1")
    refresh_asset_stats(ASSET_FORECAST_HOURLY, run_id="run-1", registry=registry)
    refresh_asset_stats(ASSET_AIR_QUALITY_HOURLY, run_id="run-1", registry=registry)

    with session_scope() as session:
        graph = build_graph(session)
        versions_ok = session.scalar(select(func.count(DataAsset.id))) == len(registry.assets)

    forecast_node = next(node for node in graph["nodes"] if node["key"] == ASSET_FORECAST_HOURLY)
    assert forecast_node["row_count"] == 6
    assert forecast_node["city_count"] == 1
    assert forecast_node["latest_data_time"] is not None
    assert versions_ok

    # 未落湖的资产仍应有节点，只是行数为 0
    daily_node = next(node for node in graph["nodes"] if node["key"] == ASSET_ENVIRONMENT_DAILY)
    assert daily_node["row_count"] == 0


@pytest.mark.parametrize("direction", ["upstream", "downstream", "both"])
def test_trace_directions_are_valid(workspace, direction: str) -> None:
    with session_scope() as session:
        result = trace(session, ASSET_ENVIRONMENT_HOURLY, direction=direction)  # type: ignore[arg-type]
    assert result["found"]
    assert result["levels"][0]["nodes"][0]["key"] == ASSET_ENVIRONMENT_HOURLY
