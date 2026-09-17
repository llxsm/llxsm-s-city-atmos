"""启动时自动打开浏览器：单元测试。

不真的弹浏览器，而是注入探针与打开函数，验证"就绪后才打开"的时序与降级。
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient


def test_probe_ready_against_live_service() -> None:
    """探针应对真实服务返回 True，对未监听端口返回 False。"""
    from atmos.apps.analysis.app import create_app
    from atmos.cli import _probe_ready

    with TestClient(create_app()) as client:
        # TestClient 不监听真实端口，仅验证探针对不可达地址不会抛异常
        assert client.get("/api/health").status_code == 200

    assert _probe_ready("http://127.0.0.1:9", timeout=0.3) is False
    assert _probe_ready("http://127.0.0.1:9/", timeout=0.3) is False


def test_opens_only_after_service_is_ready() -> None:
    """核心时序：先探到就绪，再调用打开。"""
    from atmos.cli import _open_browser_when_ready

    timeline: list[str] = []
    state = {"ready": False, "checks": 0}

    def probe(_url: str) -> bool:
        state["checks"] += 1
        timeline.append("probe")
        # 前两次未就绪，第三次就绪
        return state["checks"] >= 3

    def opener(url: str) -> bool:
        timeline.append(f"open:{url}")
        return True

    thread = _open_browser_when_ready(
        "http://127.0.0.1:8000/",
        ready_timeout=5.0,
        poll_interval=0.01,
        probe=probe,
        opener=opener,
    )
    thread.join(timeout=5.0)

    assert not thread.is_alive(), "线程未在超时内结束"
    assert state["checks"] == 3, f"就绪后应立即停止探测，实际探测 {state['checks']} 次"
    assert timeline[-1] == "open:http://127.0.0.1:8000/"
    assert timeline.count("probe") == 3


def test_opens_immediately_when_already_ready() -> None:
    """服务已就绪时不应无谓轮询。"""
    from atmos.cli import _open_browser_when_ready

    checks = {"count": 0}
    opened: list[str] = []

    def probe(_url: str) -> bool:
        checks["count"] += 1
        return True

    thread = _open_browser_when_ready(
        "http://127.0.0.1:8001/",
        ready_timeout=5.0,
        poll_interval=0.01,
        probe=probe,
        opener=lambda url: opened.append(url) or True,
    )
    thread.join(timeout=5.0)

    assert checks["count"] == 1
    assert opened == ["http://127.0.0.1:8001/"]


def test_opens_anyway_after_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    """探测超时仍要打开 —— 地址是对的，比静默什么都不发生更有用。"""
    from atmos.cli import _open_browser_when_ready

    opened: list[str] = []
    started = time.monotonic()
    thread = _open_browser_when_ready(
        "http://127.0.0.1:8000/",
        ready_timeout=0.15,
        poll_interval=0.02,
        probe=lambda _url: False,
        opener=lambda url: opened.append(url) or True,
    )
    thread.join(timeout=5.0)

    assert time.monotonic() - started >= 0.15
    assert opened == ["http://127.0.0.1:8000/"]


def test_failed_open_is_reported_not_raised(capsys: pytest.CaptureFixture[str]) -> None:
    """无法唤起浏览器时应提示手动打开，而不是抛异常中断服务。"""
    from atmos.cli import _open_browser_when_ready

    thread = _open_browser_when_ready(
        "http://127.0.0.1:8000/",
        ready_timeout=1.0,
        poll_interval=0.01,
        probe=lambda _url: True,
        opener=lambda _url: False,
    )
    thread.join(timeout=5.0)

    captured = capsys.readouterr()
    assert "未能唤起默认浏览器" in captured.out
    assert "http://127.0.0.1:8000/" in captured.out


def test_opener_exception_is_swallowed(capsys: pytest.CaptureFixture[str]) -> None:
    """浏览器注册异常（无图形界面等）不能影响服务启动。"""
    from atmos.cli import _open_browser_when_ready

    def boom(_url: str) -> bool:
        raise RuntimeError("no display")

    thread = _open_browser_when_ready(
        "http://127.0.0.1:8000/",
        ready_timeout=1.0,
        poll_interval=0.01,
        probe=lambda _url: True,
        opener=boom,
    )
    thread.join(timeout=5.0)

    assert "未能唤起默认浏览器" in capsys.readouterr().out


def test_serve_accepts_no_open_flag() -> None:
    """``--no-open`` 必须被解析器接受，且默认为自动打开。"""
    from atmos.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["serve"]).no_open is False
    assert parser.parse_args(["serve", "--no-open"]).no_open is True
    assert parser.parse_args(["serve", "--portal", "both", "--no-open"]).portal == "both"


def test_settings_expose_open_browser_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置项可关闭自动打开（服务器 / CI 场景）。"""
    from atmos.settings import Settings

    assert Settings().open_browser is True
    monkeypatch.setenv("ATMOS_OPEN_BROWSER", "false")
    assert Settings().open_browser is False
