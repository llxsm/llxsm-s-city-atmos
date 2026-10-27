"""应用内 HTTP Basic 认证。

关注三件事：默认不启用（不影响本地开发与既有测试）、凭据正确才放行、
以及"只配一半"这种配置事故不能被静默放行。
"""

from __future__ import annotations

import base64
import logging

import pytest
from fastapi.testclient import TestClient


def _basic(user: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """按当前环境变量重建分析平台的应用（配置是 lru_cache 单例，改环境后必须重载）。"""
    from atmos.apps.analysis.app import create_app
    from atmos.settings import reload_settings

    reload_settings()
    return TestClient(create_app())


# ==================================================================
# 默认行为
# ==================================================================
def test_auth_is_disabled_by_default(analysis_client: TestClient) -> None:
    """零配置下平台无鉴权 —— 本地开发与既有集成测试都依赖这一点。"""
    assert analysis_client.get("/api/meta").status_code == 200


def test_health_is_exempt_when_auth_enabled(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """健康检查免认证：它是 systemd / 监控系统的探针，不该要求每个探针配密码。"""
    monkeypatch.setenv("ATMOS_AUTH_USER", "atmos")
    monkeypatch.setenv("ATMOS_AUTH_PASSWORD", "s3cret")
    with _client(monkeypatch) as client:
        response = client.get("/api/health")
    assert response.status_code == 200


# ==================================================================
# 开启之后
# ==================================================================
def test_requires_credentials(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATMOS_AUTH_USER", "atmos")
    monkeypatch.setenv("ATMOS_AUTH_PASSWORD", "s3cret")
    with _client(monkeypatch) as client:
        response = client.get("/api/meta")

    assert response.status_code == 401
    # 没有这个头浏览器不会弹登录框，页面只会白屏 + 一个看不出原因的 401
    assert response.headers["www-authenticate"].startswith("Basic realm=")
    assert response.json()["ok"] is False
    assert "认证" in response.json()["message"]


def test_accepts_correct_credentials(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATMOS_AUTH_USER", "atmos")
    monkeypatch.setenv("ATMOS_AUTH_PASSWORD", "s3cret")
    with _client(monkeypatch) as client:
        response = client.get("/api/meta", headers=_basic("atmos", "s3cret"))
    assert response.status_code == 200
    assert response.json()["platform"]["portal"] == "analysis"


@pytest.mark.parametrize(
    "header",
    [
        _basic("atmos", "wrong"),
        _basic("someone", "s3cret"),
        {"Authorization": "Bearer s3cret"},
        {"Authorization": "Basic "},
    ],
)
def test_rejects_wrong_or_malformed(
    workspace, monkeypatch: pytest.MonkeyPatch, header: dict[str, str]
) -> None:
    monkeypatch.setenv("ATMOS_AUTH_USER", "atmos")
    monkeypatch.setenv("ATMOS_AUTH_PASSWORD", "s3cret")
    with _client(monkeypatch) as client:
        assert client.get("/api/meta", headers=header).status_code == 401


def test_protects_page_and_runtime_config(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """整站受保护，不只是 API：页面本身要 401，才能触发浏览器的登录框。

    ``/portal-config.js`` 尤其重要 —— 它带两个后端地址，不该对外匿名可见。
    """
    monkeypatch.setenv("ATMOS_AUTH_USER", "atmos")
    monkeypatch.setenv("ATMOS_AUTH_PASSWORD", "s3cret")
    with _client(monkeypatch) as client:
        assert client.get("/").status_code == 401
        assert client.get("/portal-config.js").status_code == 401
        assert client.get("/api/docs").status_code == 401
        assert client.get("/", headers=_basic("atmos", "s3cret")).status_code == 200


def test_preflight_is_not_blocked(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    """预检请求按规范不带凭据，拦下它会让跨源部署直接失效。"""
    monkeypatch.setenv("ATMOS_AUTH_USER", "atmos")
    monkeypatch.setenv("ATMOS_AUTH_PASSWORD", "s3cret")
    monkeypatch.setenv("ATMOS_CORS_ORIGINS", "https://a.example.com")
    with _client(monkeypatch) as client:
        response = client.options(
            "/api/meta",
            headers={
                "Origin": "https://a.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.status_code != 401


# ==================================================================
# 配置事故
# ==================================================================
def test_half_configured_is_disabled_and_warns(
    workspace, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """只配用户名或只配密码，几乎总是配置写漏 —— 不能静默放行也不该静默拦死。

    这里的选择是"按未启用处理 + 告警"：把平台锁定在外是更难排查的故障。
    """
    monkeypatch.setenv("ATMOS_AUTH_USER", "atmos")
    monkeypatch.delenv("ATMOS_AUTH_PASSWORD", raising=False)

    caplog.set_level(logging.WARNING, logger="atmos.apps.auth")
    with _client(monkeypatch) as client:
        assert client.get("/api/meta").status_code == 200

    assert any("认证配置不完整" in record.message for record in caplog.records)


# ==================================================================
# CORS 白名单
# ==================================================================
def test_cors_origins_are_parsed_and_trimmed(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ATMOS_CORS_ORIGINS", " https://a.example.com , https://b.example.com/ ,,"
    )
    from atmos.settings import reload_settings

    settings = reload_settings()
    # Origin 头从不带尾斜杠，配置里的尾斜杠必须被去掉，否则永远匹配不上
    assert settings.cors_origin_list() == ["https://a.example.com", "https://b.example.com"]


def test_prod_without_origins_sends_no_cors_header(
    workspace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同源部署（推荐形态）下不发 CORS 头才是对的，不是"忘了配"。"""
    monkeypatch.setenv("ATMOS_ENV", "prod")
    monkeypatch.delenv("ATMOS_CORS_ORIGINS", raising=False)
    with _client(monkeypatch) as client:
        response = client.get("/api/meta", headers={"Origin": "https://a.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_prod_with_origins_allows_only_listed(workspace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATMOS_ENV", "prod")
    monkeypatch.setenv("ATMOS_CORS_ORIGINS", "https://a.example.com")
    with _client(monkeypatch) as client:
        allowed = client.get("/api/meta", headers={"Origin": "https://a.example.com"})
        denied = client.get("/api/meta", headers={"Origin": "https://evil.example.com"})

    assert allowed.headers["access-control-allow-origin"] == "https://a.example.com"
    assert "access-control-allow-origin" not in denied.headers
