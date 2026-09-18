"""测试夹具。

隔离策略
--------
每个测试用 ``tmp_path`` 作为数据湖与资产目录根目录，通过环境变量注入
并清空全部 ``lru_cache``，确保测试之间不共享任何持久化或内存状态。
配置文件仍使用仓库内的 ``config/``（元数据本身即是被测对象）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from atmos.settings import PROJECT_ROOT, reload_settings

CONFIG_DIR = PROJECT_ROOT / "config"
WEB_DIR = PROJECT_ROOT / "web"

# 调用 node 的超时上限：前端检查都是本地小脚本，正常在秒级完成
NODE_TIMEOUT_SECONDS = 120


# ==================================================================
# 前端检查辅助（需要 node，见 tests/test_frontend_syntax.py / test_frontend_theme.py）
# ==================================================================
def _node_prefix() -> list[str] | None:
    """定位 node。Windows 上可能是 ``node.cmd`` 垫片，需经 cmd.exe 执行。"""
    executable = shutil.which("node")
    if not executable:
        return None
    if executable.lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", executable]
    return [executable]


NODE = _node_prefix()


@pytest.fixture(scope="session")
def node_cmd() -> list[str]:
    """node 命令前缀；未安装 node 时跳过依赖它的测试。

    前端无构建步骤，node 不是本项目的运行依赖，因此这里只跳过而不失败。
    """
    if NODE is None:
        pytest.skip("未找到 node，跳过前端检查")
    return NODE


def run_node(
    node_cmd: list[str],
    script: Path,
    args: list[str] | None = None,
    env_extra: dict[str, str] | None = None,
) -> dict:
    """执行 node 脚本，取回它打印的 JSON 结果（最后一行有效输出）。

    约定：被测脚本把结论以 ``console.log(JSON.stringify(...))`` 输出到 stdout，
    其余诊断信息走 stderr，不受 ``--no-warnings`` 之外的干扰。
    """
    env = dict(os.environ)
    env.update(env_extra or {})
    completed = subprocess.run(
        [*node_cmd, "--no-warnings", "--experimental-vm-modules", str(script), *(args or [])],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=NODE_TIMEOUT_SECONDS,
        check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        pytest.fail(f"node 未输出结果（退出码 {completed.returncode}）：{completed.stderr.strip()[:800]}")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        pytest.fail(f"node 输出不是 JSON：{lines[-1][:400]}\nstderr: {completed.stderr.strip()[:400]}")


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """独立的数据湖 + 资产目录工作区，并完成一次目录引导。"""
    monkeypatch.setenv("ATMOS_LAKE_DIR", str(tmp_path / "lake"))
    monkeypatch.setenv("ATMOS_CATALOG_DB", str(tmp_path / "catalog.db"))
    monkeypatch.setenv("ATMOS_CONFIG_DIR", str(CONFIG_DIR))
    monkeypatch.setenv("ATMOS_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("ATMOS_COLLECT_MAX_CITIES", "5")

    _reset()
    from atmos.catalog.bootstrap import bootstrap_catalog
    from atmos.db import init_db

    init_db()
    bootstrap_catalog()

    yield tmp_path

    _reset()


def _reset() -> None:
    """清空配置与依赖缓存，并释放数据库连接。"""
    from atmos.apps.common.deps import reset_caches
    from atmos.catalog.definitions import load_registry
    from atmos.db import reset_engine

    reset_engine()
    reset_caches()
    load_registry.cache_clear()
    reload_settings()


@pytest.fixture()
def analysis_client(workspace: Path):
    """指向临时工作区的**分析平台** API 客户端。"""
    from fastapi.testclient import TestClient

    from atmos.apps.analysis.app import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture()
def governance_client(workspace: Path):
    """指向临时工作区的**治理平台** API 客户端。"""
    from fastapi.testclient import TestClient

    from atmos.apps.governance.app import create_app

    with TestClient(create_app()) as client:
        yield client


@pytest.fixture()
def registry():
    """平台元数据注册表。"""
    from atmos.catalog.definitions import load_registry

    return load_registry()


@pytest.fixture()
def settings(workspace: Path):
    """指向临时工作区的配置对象。"""
    from atmos.settings import get_settings

    return get_settings()


# ==================================================================
# 造数辅助
# ==================================================================
def forecast_rows(count: int = 6, start: str = "2026-01-01T00:00") -> list[dict]:
    """构造若干条合法的逐小时天气记录。"""
    from datetime import datetime, timedelta

    base = datetime.fromisoformat(start)
    rows: list[dict] = []
    for index in range(count):
        stamp = (base + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M")
        rows.append(
            {
                "time": stamp,
                "temperature_2m": 20.0 + index * 0.5,
                "relative_humidity_2m": 55.0,
                "dew_point_2m": 10.0,
                "apparent_temperature": 21.0 + index * 0.5,
                "precipitation": 0.0,
                "rain": 0.0,
                "snowfall": 0.0,
                "precipitation_probability": 10.0,
                "weather_code": 1,
                "cloud_cover": 30.0,
                "pressure_msl": 1012.0,
                "surface_pressure": 1005.0,
                "wind_speed_10m": 12.0,
                "wind_direction_10m": 180.0,
                "wind_gusts_10m": 25.0,
                "is_day": 1,
                "visibility": 20000.0,
                "uv_index": 3.0,
                "is_forecast": 0,
            }
        )
    return rows


def air_rows(count: int = 6, start: str = "2026-01-01T00:00") -> list[dict]:
    """构造若干条合法的逐小时空气质量记录。"""
    from datetime import datetime, timedelta

    base = datetime.fromisoformat(start)
    rows: list[dict] = []
    for index in range(count):
        stamp = (base + timedelta(hours=index)).strftime("%Y-%m-%dT%H:%M")
        rows.append(
            {
                "time": stamp,
                "pm2_5": 20.0 + index,
                "pm10": 35.0 + index * 2,
                "carbon_monoxide": 300.0,
                "nitrogen_dioxide": 25.0,
                "sulphur_dioxide": 8.0,
                "ozone": 60.0,
                "ammonia": 4.0,
                "dust": 5.0,
                "aerosol_optical_depth": 0.2,
                "uv_index": 3.0,
                "uv_index_clear_sky": 3.5,
                "european_aqi": 25.0 + index,
                "european_aqi_pm2_5": 25.0,
                "european_aqi_pm10": 20.0,
                "european_aqi_nitrogen_dioxide": 15.0,
                "european_aqi_ozone": 18.0,
                "european_aqi_sulphur_dioxide": 5.0,
                "us_aqi": 60.0,
                "us_aqi_pm2_5": 60.0,
                "us_aqi_pm10": 40.0,
                "us_aqi_nitrogen_dioxide": 30.0,
                "us_aqi_ozone": 35.0,
                "us_aqi_sulphur_dioxide": 10.0,
                "us_aqi_carbon_monoxide": 12.0,
                "alder_pollen": 0.0,
                "birch_pollen": 0.0,
                "grass_pollen": 0.0,
                "mugwort_pollen": 0.0,
                "olive_pollen": 0.0,
                "ragweed_pollen": 0.0,
            }
        )
    return rows
