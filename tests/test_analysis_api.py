"""分析平台 REST 接口冒烟测试。

只覆盖读接口与不触网的写接口；真实采集涉及外部数据源，
由 ``test_collect.py`` 用假的 HTTP 传输层覆盖。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atmos.sources.specs import ASSET_FORECAST_HOURLY


@pytest.fixture()
def client(analysis_client: TestClient) -> TestClient:
    return analysis_client


# ==================================================================
# 元信息
# ==================================================================
def test_health(client: TestClient) -> None:
    payload = client.get("/api/health").json()
    assert payload["status"] == "ok"
    assert payload["portal"] == "analysis"
    assert payload["catalog"]["status"] == "ok"


def test_meta_declares_analysis_portal(client: TestClient) -> None:
    payload = client.get("/api/meta").json()
    assert payload["platform"]["portal"] == "analysis"
    assert payload["platform"]["name"] == "城市环境数据分析平台"
    assert payload["sources"][0]["name"] == "Open-Meteo"
    # 分析平台不需要治理枚举与质量模型
    assert "enums" not in payload
    assert "quality_model" not in payload


def test_overview_is_coverage_oriented(client: TestClient) -> None:
    payload = client.get("/api/overview").json()
    assert payload["city_count"] == 15
    assert payload["total_rows"] == 0
    assert payload["has_data"] is False
    assert "采集" in (payload["hint"] or "")
    assert payload["analysis_windows"]["hourly_days"] >= 30
    assert payload["availability"]["has_environment"] is False
    # 治理视角的字段不应出现在分析平台
    assert "sla_breaches" not in payload


def test_availability(client: TestClient) -> None:
    payload = client.get("/api/availability").json()
    assert payload["has_environment"] is False
    assert len(payload["cities"]) == 15


# ==================================================================
# 环境数据
# ==================================================================
def test_env_cities_and_metrics(client: TestClient) -> None:
    cities = client.get("/api/env/cities").json()
    assert cities["total"] == 15
    assert {item["slug"] for item in cities["items"]} >= {"beijing", "wuhan"}
    assert {item["country"] for item in cities["items"]} == {"中国"}

    metrics = client.get("/api/env/metrics").json()
    names = {item["name"] for item in metrics["items"]}
    assert {"temperature_2m", "european_aqi", "pm2_5", "comfort_index"} <= names


def test_env_current_empty_state(client: TestClient) -> None:
    payload = client.get("/api/env/current").json()
    assert payload["has_data"] is False
    assert "采集" in (payload["note"] or "")


def test_env_unknown_city_rejected(client: TestClient) -> None:
    assert client.get("/api/env/hourly", params={"city": "ghost"}).status_code == 404
    assert client.get("/api/env/daily", params={"city": "ghost"}).status_code == 404
    assert client.get("/api/env/trend", params={"city": "ghost"}).status_code == 404
    assert client.get("/api/env/current", params={"cities": "ghost"}).status_code == 400


def test_env_compare_requires_two_cities(client: TestClient) -> None:
    assert client.get("/api/env/compare", params={"cities": "beijing"}).status_code == 400
    assert client.get("/api/env/compare", params={"cities": "beijing,ghost"}).status_code == 400


def test_env_removed_international_city_rejected(client: TestClient) -> None:
    assert client.get("/api/env/hourly", params={"city": "tokyo"}).status_code == 404


def test_env_ranking_and_alerts_empty(client: TestClient) -> None:
    assert client.get("/api/env/ranking").json()["worst_air"] == []
    assert client.get("/api/env/alerts").json()["total"] == 0


# ==================================================================
# 深度分析（空库时应给出可读说明而不是报错）
# ==================================================================
def test_insight_catalog(client: TestClient) -> None:
    payload = client.get("/api/insights/catalog").json()
    assert payload["total"] == 11
    categories = {item["category"] for item in payload["categories"]}
    assert {"描述性分析", "时段规律", "异常与过程", "归因分析", "考核与报表", "横向对比"} <= categories
    assert payload["availability"]["has_environment"] is False
    assert any(item["name"] == "污染过程识别" for group in payload["categories"] for item in group["items"])


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("/api/insights/digest", {}),
        ("/api/insights/distribution", {}),
        ("/api/insights/correlation", {}),
        ("/api/insights/hourly-profile", {}),
        ("/api/insights/weekly-profile", {}),
        ("/api/insights/monthly-trend", {}),
        ("/api/insights/outliers", {}),
        ("/api/insights/episodes", {}),
        ("/api/insights/relationship", {}),
        ("/api/insights/compliance", {}),
        ("/api/insights/comparison", {}),
        ("/api/insights/clustering", {}),
    ],
)
def test_insight_endpoints_degrade_gracefully(
    client: TestClient, path: str, params: dict
) -> None:
    """空库时每项分析都应返回 200 与可读的中文说明。"""
    response = client.get(path, params=params)
    assert response.status_code == 200, path
    payload = response.json()
    if "available" in payload:
        assert payload["available"] is False, path
    # 必须给出某种提示信息，不能是静默的空结果
    text = str(payload.get("message") or payload.get("messages") or "")
    assert text, f"{path} 未给出任何说明"


def test_relationship_pairs(client: TestClient) -> None:
    payload = client.get("/api/insights/relationship/pairs").json()
    assert payload["total"] >= 5
    keys = {item["x"] for item in payload["items"]}
    assert "wind_speed_10m" in keys
    assert all(item["rationale"] for item in payload["items"])


def test_insight_export_rejects_unknown_analysis(client: TestClient) -> None:
    assert client.get("/api/insights/nonsense/export.csv").status_code == 400


@pytest.mark.parametrize("analysis", ["compliance", "episodes", "distribution"])
def test_insight_export_headers_are_ascii_safe(client: TestClient, analysis: str) -> None:
    """CSV 导出必须在空库下也能安全返回。

    这里专门覆盖一个易犯的错误：把中文报表名直接放进 HTTP 头。HTTP 头只能是
    latin-1，Starlette 会在编码响应头时抛 UnicodeEncodeError（500）。
    """
    response = client.get(f"/api/insights/{analysis}/export.csv")
    # 空库时前两者会因无数据返回 400；关键是不能 500，且头必须可编码
    assert response.status_code in {200, 400}, response.text
    for key, value in response.headers.items():
        value.encode("latin-1")  # 不能抛异常
    if response.status_code == 200:
        assert response.headers["content-type"].startswith("text/csv")


def test_insight_export_compliance_without_data(client: TestClient) -> None:
    """无数据时导出应报 400 并给出中文原因，而不是产出空文件。"""
    response = client.get("/api/insights/compliance/export.csv")
    assert response.status_code == 400
    assert "采集" in response.json()["detail"]


# ==================================================================
# 数据下载
# ==================================================================
def test_export_dataset_csv_empty(client: TestClient) -> None:
    response = client.get(f"/api/export/{ASSET_FORECAST_HOURLY}.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert response.headers["x-row-count"] == "0"
    assert response.text.startswith("\ufeff")


def test_export_unknown_dataset(client: TestClient) -> None:
    assert client.get("/api/export/nope.nope.csv").status_code == 404
    assert client.get("/api/export/nope.nope.json").status_code == 404


def test_export_dataset_json_declares_schema(client: TestClient) -> None:
    payload = client.get(f"/api/export/{ASSET_FORECAST_HOURLY}.json").json()
    assert payload["dataset"]["key"] == ASSET_FORECAST_HOURLY
    assert payload["columns"]
    assert payload["row_count"] == 0


# ==================================================================
# 数据刷新
# ==================================================================
def test_ops_jobs(client: TestClient) -> None:
    payload = client.get("/api/ops/jobs").json()
    assert payload["total"] == 7
    assert "derive" in payload["scopes"]


def test_ops_tasks_empty(client: TestClient) -> None:
    payload = client.get("/api/ops/tasks").json()
    assert payload["total"] == 0
    assert payload["active"] == 0


def test_collect_request_validation(client: TestClient) -> None:
    response = client.post("/api/ops/collect", json={"scope": "everything", "wait": True})
    assert response.status_code == 422


# ==================================================================
# 边界：分析平台不提供治理能力
# ==================================================================
@pytest.mark.parametrize(
    "path",
    [
        "/api/assets",
        "/api/cities",
        "/api/lineage/graph",
        "/api/quality/summary",
        "/api/ops/runs",
        "/api/export/metadata",
    ],
)
def test_governance_endpoints_are_not_on_analysis_portal(
    client: TestClient, path: str
) -> None:
    """治理能力不应出现在分析平台 —— 这是职责边界的硬约束。"""
    assert client.get(path).status_code == 404, path


def test_openapi_covers_analysis_surface(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    paths = set(schema["paths"])
    assert {
        "/api/health",
        "/api/overview",
        "/api/env/current",
        "/api/insights/catalog",
        "/api/insights/episodes",
        "/api/ops/collect",
    } <= paths
