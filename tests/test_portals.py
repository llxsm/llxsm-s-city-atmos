"""两个平台的协作与边界测试。

拆分不只要"各自能跑"，还要保证"合起来仍然一致"：

* 两个服务共享同一份数据湖与资产目录；
* 在治理平台停用城市后，分析平台的城市选择器要同步消失（同一份目录）；
* 两个平台的接口集合互不重叠（职责边界）；
* 两个平台同时启动时目录引导不会互相破坏。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def portals(workspace):
    """同时启动两个门户客户端（对应 --portal both）。"""
    from atmos.apps.analysis.app import create_app as create_analysis
    from atmos.apps.governance.app import create_app as create_governance

    with TestClient(create_analysis()) as analysis, TestClient(create_governance()) as governance:
        yield analysis, governance


def test_both_portals_start_and_share_state(portals) -> None:
    """两个平台同时启动应各自健康，并看到同一份目录。"""
    analysis, governance = portals
    assert analysis.get("/api/health").json()["portal"] == "analysis"
    assert governance.get("/api/health").json()["portal"] == "governance"
    assert governance.get("/api/health").json()["registry"]["assets"] == 7
    # 分析平台的总览城市数应与治理平台的城市数一致
    assert analysis.get("/api/overview").json()["city_count"] == 15
    assert governance.get("/api/overview").json()["city_count"] == 15


def test_city_deactivation_propagates_to_analysis(portals) -> None:
    """治理平台停用城市后，分析平台的城市选择器应立即同步。"""
    analysis, governance = portals
    before = analysis.get("/api/env/cities").json()
    assert "guangzhou" in {item["slug"] for item in before["items"]}

    assert governance.patch("/api/cities/guangzhou", json={"is_active": False}).status_code == 200

    after = analysis.get("/api/env/cities").json()
    assert "guangzhou" not in {item["slug"] for item in after["items"]}
    assert after["total"] == before["total"] - 1
    assert analysis.get("/api/env/hourly", params={"city": "guangzhou"}).status_code == 404


def test_endpoint_surfaces_do_not_overlap_beyond_shared_meta(portals) -> None:
    """两个平台的业务接口集合不应重叠，只共享 health/meta/overview/jobs 这类框架接口。"""
    analysis, governance = portals
    analysis_paths = set(analysis.get("/api/openapi.json").json()["paths"])
    governance_paths = set(governance.get("/api/openapi.json").json()["paths"])

    shared = analysis_paths & governance_paths
    assert shared == {"/api/health", "/api/meta", "/api/overview", "/api/ops/jobs"}

    # 分析侧独有
    assert "/api/insights/compliance" in analysis_paths - governance_paths
    assert "/api/env/current" in analysis_paths - governance_paths
    # 治理侧独有
    assert "/api/lineage/graph" in governance_paths - analysis_paths
    assert "/api/quality/summary" in governance_paths - analysis_paths


# ==================================================================
# 统一前端：两个服务托管同一套前端，靠注入脚本区分身份
# ==================================================================
def test_both_services_inject_their_own_portal_identity(portals) -> None:
    """``/portal-config.js`` 必须告诉前端"我是谁"与"另一个在哪"。

    这是统一前端能同时服务两个平台的关键：前端代码只有一份，
    差异全部来自这个运行时注入。
    """
    analysis, governance = portals
    for client, expected in ((analysis, "analysis"), (governance, "governance")):
        response = client.get("/portal-config.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/javascript")
        assert "no-store" in response.headers.get("cache-control", "")

        body = response.text
        assert body.startswith("window.__ATMOS_PORTALS__ = ")
        payload = json.loads(body.split("=", 1)[1].strip().rstrip(";"))
        assert payload["current"] == expected
        assert set(payload["endpoints"]) == {"analysis", "governance"}
        for url in payload["endpoints"].values():
            assert url.startswith("http://")
            assert not url.endswith("/")


def test_portal_config_declares_browser_reachable_urls(portals) -> None:
    """注入的地址必须是浏览器可用的（不能是 0.0.0.0）。"""
    analysis, _ = portals
    payload = json.loads(
        analysis.get("/portal-config.js").text.split("=", 1)[1].strip().rstrip(";")
    )
    for url in payload["endpoints"].values():
        assert "0.0.0.0" not in url
        assert "://" in url


def test_frontend_assets_must_be_revalidated(portals) -> None:
    """前端模块必须带 ``Cache-Control: no-cache``。

    前端文件名没有内容哈希，缺了这条头浏览器就会按启发式规则复用旧模块，
    出现"代码已修好、页面仍报旧错"的假象；动态 ``import()`` 又不吃强制刷新，
    用户很难自行摆脱。
    """
    analysis, _ = portals
    for path in ("/", "/index.html", "/shared/portal.js", "/static/views/relationship.js"):
        response = analysis.get(path)
        assert response.status_code == 200, path
        assert "no-cache" in response.headers.get("cache-control", ""), path


def test_unified_frontend_does_not_loosen_the_server_side_boundary(portals) -> None:
    """统一前端能同时看到两个平台，但**服务端**边界不能因此放宽。"""
    analysis, _ = portals
    for path in ("/api/assets", "/api/lineage/graph", "/api/quality/summary"):
        assert analysis.get(path).status_code == 404, path


def test_governance_quality_score_visible_after_evaluation(portals) -> None:
    """治理平台评测出的质量分，分析平台的总览接口应能读到（同一份目录库）。"""
    analysis, governance = portals
    assert governance.post("/api/quality/evaluate", json={"wait": True}).status_code == 200
    scores = governance.get("/api/quality/scores").json()
    assert scores["total"] == 7
    # 分析平台的总览不展示质量分（职责分离），但仍应能正常返回
    assert analysis.get("/api/overview").status_code == 200


def test_bootstrap_from_governance_keeps_analysis_consistent(portals) -> None:
    """治理平台重新引导不应改变分析平台看到的数据集范围。"""
    analysis, governance = portals
    before = analysis.get("/api/insights/catalog").json()["availability"]
    assert governance.post("/api/ops/bootstrap").status_code == 200
    after = analysis.get("/api/insights/catalog").json()["availability"]
    assert before == after
