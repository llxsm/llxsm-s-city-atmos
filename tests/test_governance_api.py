"""治理平台 REST 接口冒烟测试。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atmos.sources.specs import ASSET_FORECAST_HOURLY


@pytest.fixture()
def client(governance_client: TestClient) -> TestClient:
    return governance_client


# ==================================================================
# 元信息
# ==================================================================
def test_health(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["portal"] == "governance"
    assert payload["registry"]["assets"] == 7
    assert payload["registry"]["cities"] == 15
    assert payload["registry"]["lineage_edges"] == 8
    assert payload["registry"]["quality_checks"] == 14


def test_meta_exposes_enums_and_quality_model(client: TestClient) -> None:
    payload = client.get("/api/meta").json()
    assert payload["platform"]["portal"] == "governance"
    assert {item["value"] for item in payload["enums"]["asset_layer"]} == {
        "reference",
        "raw",
        "refined",
        "serving",
    }
    model = payload["quality_model"]
    assert len(model["dimensions"]) == 5
    assert abs(sum(item["weight"] for item in model["dimensions"]) - 1.0) < 1e-9
    assert len(model["checks"]) == 14
    # 治理平台不需要采集策略与环境指标字典
    assert "collect" not in payload


def test_overview_reports_governance_kpis(client: TestClient) -> None:
    payload = client.get("/api/overview").json()
    assert payload["asset_count"] == 7
    assert payload["city_count"] == 15
    assert payload["column_count"] == 171
    assert payload["total_rows"] == 0
    # 治理体检不再单独成页，因此不应作为独立接口暴露
    assert payload["meta"]["rules_version"]


# ==================================================================
# 资产目录
# ==================================================================
def test_assets_list_with_facets(client: TestClient) -> None:
    payload = client.get("/api/assets").json()
    assert payload["total"] == 7
    assert payload["facets"]["layer"]["raw"] == 4
    assert payload["facets"]["domain"]["weather"] == 3


def test_assets_filter_by_search(client: TestClient) -> None:
    keys = {item["key"] for item in client.get("/api/assets", params={"search": "空气质量"}).json()["items"]}
    assert "openmeteo.air_quality.hourly" in keys


def test_asset_detail_exposes_field_dictionary(client: TestClient) -> None:
    payload = client.get(f"/api/assets/{ASSET_FORECAST_HOURLY}").json()
    assert payload["key"] == ASSET_FORECAST_HOURLY
    assert payload["owner"] and payload["steward"]
    assert payload["primary_key"] == ["city_id", "time"]
    assert payload["checks"]
    assert payload["freshness"]["breached"] is True
    temperature = next(item for item in payload["columns"] if item["name"] == "temperature_2m")
    assert temperature["unit"] == "°C"
    assert temperature["value_range"] == [-90.0, 60.0]


def test_asset_detail_404(client: TestClient) -> None:
    assert client.get("/api/assets/nope.nope").status_code == 404


def test_asset_sample_and_profiling_empty(client: TestClient) -> None:
    assert client.get(f"/api/assets/{ASSET_FORECAST_HOURLY}/sample").json()["rows"] == []
    assert client.get(f"/api/assets/{ASSET_FORECAST_HOURLY}/profiling").json()["columns"] == []


# ==================================================================
# 城市主数据
# ==================================================================
def test_cities_are_domestic_only(client: TestClient) -> None:
    payload = client.get("/api/cities").json()
    assert payload["total"] == 15
    assert payload["active"] == 15
    assert payload["countries"] == {"中国": 15}
    assert "wuhan" in {item["slug"] for item in payload["items"]}


def test_create_and_update_city(client: TestClient) -> None:
    body = {
        "slug": "testville",
        "name_zh": "测试城",
        "name_en": "Testville",
        "country": "中国",
        "country_code": "CN",
        "latitude": 30.0,
        "longitude": 120.0,
        "timezone": "Asia/Shanghai",
    }
    created = client.post("/api/cities", json=body)
    assert created.status_code == 201
    assert created.json()["ok"] is True
    assert client.post("/api/cities", json=body).status_code == 409

    updated = client.patch("/api/cities/testville", json={"is_active": False, "population": 12345})
    assert updated.status_code == 200
    assert updated.json()["city"]["is_active"] is False
    assert client.patch("/api/cities/ghost", json={"is_active": False}).status_code == 404


def test_create_city_validates_coordinates(client: TestClient) -> None:
    response = client.post(
        "/api/cities",
        json={
            "slug": "badcity",
            "name_zh": "坏城",
            "name_en": "Bad",
            "country": "X",
            "country_code": "XX",
            "latitude": 120.0,
            "longitude": 999.0,
            "timezone": "UTC",
        },
    )
    assert response.status_code == 422


# ==================================================================
# 数据血缘
# ==================================================================
def test_lineage_graph(client: TestClient) -> None:
    payload = client.get("/api/lineage/graph").json()
    assert len(payload["nodes"]) == 7
    assert len(payload["edges"]) == 8
    assert payload["stats"]["max_depth"] == 3
    assert payload["stats"]["isolated_count"] == 0
    assert payload["layer_labels"]["raw"] == "原始层"


def test_lineage_impact_both_directions(client: TestClient) -> None:
    downstream = client.get(
        f"/api/lineage/{ASSET_FORECAST_HOURLY}/impact", params={"direction": "downstream"}
    ).json()
    assert "atmos.city_environment.daily" in downstream["summary"]["related_assets"]

    upstream = client.get(
        "/api/lineage/atmos.city_environment.daily/impact", params={"direction": "upstream"}
    ).json()
    assert "openmeteo.geocoding.places" in upstream["summary"]["related_assets"]


def test_lineage_impact_404(client: TestClient) -> None:
    assert client.get("/api/lineage/ghost/impact").status_code == 404


def test_lineage_orphans(client: TestClient) -> None:
    payload = client.get("/api/lineage/orphans").json()
    assert payload["healthy"] is True


# ==================================================================
# 数据质量
# ==================================================================
def test_quality_summary_zero_state(client: TestClient) -> None:
    payload = client.get("/api/quality/summary").json()
    assert payload["evaluated_assets"] == 0
    assert payload["average_overall"] is None
    assert payload["worst_assets"] == []


def test_quality_checks_catalog(client: TestClient) -> None:
    assert client.get("/api/quality/checks").json()["total"] > 7


def test_quality_evaluate_single_without_data(client: TestClient) -> None:
    payload = client.get("/api/quality/evaluate/atmos.city_environment.daily").json()
    assert payload["overall"] == 0.0
    assert payload["grade"] == "E"
    assert len(payload["dimensions"]) == 5
    assert payload["checks"]


def test_quality_evaluate_single_bad_asset(client: TestClient) -> None:
    assert client.get("/api/quality/evaluate/nope").status_code == 400


def test_quality_evaluate_persists_scores(client: TestClient) -> None:
    response = client.post("/api/quality/evaluate", json={"wait": True})
    assert response.status_code == 200
    payload = response.json()
    assert payload["evaluated"] == 7
    summary = client.get("/api/quality/summary").json()
    assert summary["evaluated_assets"] == 7


# ==================================================================
# 运维审计
# ==================================================================
def test_ops_jobs_and_specs(client: TestClient) -> None:
    assert client.get("/api/ops/jobs").json()["total"] == 7
    specs = client.get("/api/ops/specs").json()
    assert specs["total"] >= 4
    forecast = next(item for item in specs["items"] if item["asset_key"] == ASSET_FORECAST_HOURLY)
    assert "temperature_2m" in forecast["variables"]


def test_ops_bootstrap_is_idempotent(client: TestClient) -> None:
    payload = client.post("/api/ops/bootstrap").json()
    assert payload["status"] == "success"
    assert payload["assets_created"] == 0
    assert payload["cities_created"] == 0


def test_ops_refresh(client: TestClient) -> None:
    payload = client.post("/api/ops/refresh").json()
    assert payload["status"] == "success"
    assert payload["assets_refreshed"] == 7


def test_ops_runs_and_summary_empty(client: TestClient) -> None:
    assert client.get("/api/ops/runs").json()["total"] == 0
    summary = client.get("/api/ops/summary").json()
    assert summary["total"] == 0
    assert summary["success_rate"] is None


# ==================================================================
# 元数据导出
# ==================================================================
def test_export_metadata(client: TestClient) -> None:
    payload = client.get("/api/export/metadata").json()
    assert payload["platform"] == "city-atmos-governance"
    assert len(payload["assets"]) == 7
    assert len(payload["lineage"]) == 8
    assert len(payload["cities"]) == 15
    assert len(payload["quality_model"]["dimensions"]) == 5
    assert all(asset["columns"] for asset in payload["assets"])


# ==================================================================
# 边界：治理平台不提供环境数据与数据刷新触发
# ==================================================================
@pytest.mark.parametrize(
    "path",
    [
        "/api/env/current",
        "/api/env/hourly",
        "/api/insights/catalog",
        "/api/insights/episodes",
        "/api/export/openmeteo.weather.forecast.hourly.csv",
    ],
)
def test_analysis_endpoints_are_not_on_governance_portal(
    client: TestClient, path: str
) -> None:
    assert client.get(path).status_code == 404, path


def test_governance_cannot_trigger_collection(client: TestClient) -> None:
    """触发采集是分析平台的职责，治理平台不应提供该入口。"""
    assert client.post("/api/ops/collect", json={"scope": "all"}).status_code == 404
