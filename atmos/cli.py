"""平台命令行入口。

用法::

    python -m atmos bootstrap          # 同步配置元数据到资产目录
    python -m atmos collect --scope all
    python -m atmos quality
    python -m atmos status
    python -m atmos serve
    python -m atmos assets
    python -m atmos lineage
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from atmos.logging_setup import get_logger, setup_logging
from atmos.settings import get_settings

logger = get_logger("cli")


def _print(payload: Any, *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
    else:
        print(payload)


def _ensure_catalog(*, reload: bool = False):
    """建表并把 ``config/*.yaml`` 幂等同步进资产目录，返回引导报告。

    采集、评测、查询都依赖目录中的资产与城市记录，因此每个命令入口都先确保
    目录已就绪 —— 否则在新环境上首次执行 ``atmos collect`` 会因为资产尚未登记
    而静默跳过统计刷新与运行记录的关联。
    """
    from atmos.catalog.bootstrap import BootstrapReport, bootstrap_catalog
    from atmos.catalog.definitions import reload_registry
    from atmos.db import init_db

    init_db()
    if reload:
        reload_registry()
    report: BootstrapReport = bootstrap_catalog()
    return report


# ==================================================================
# 子命令实现
# ==================================================================
def cmd_bootstrap(args: argparse.Namespace) -> int:
    """同步 config/*.yaml 到资产目录。"""
    report = _ensure_catalog(reload=True)
    _print({"引导结果": report.to_payload()}, as_json=args.json)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """执行采集管道。"""
    from atmos.collect.runner import Collector

    _ensure_catalog(reload=True)

    async def _run() -> int:
        report = await Collector().run(
            scope=args.scope,
            city_slugs=args.cities.split(",") if args.cities else None,
            trigger="cli",
            include_archive=not args.skip_archive,
        )
        payload = report.to_payload()
        if args.json:
            _print(payload, as_json=True)
        else:
            print(f"\n采集批次 {payload['run_id']}  状态={payload['status']}")
            print(
                f"{'作业':<34}{'状态':<10}{'城市':>6}{'收到':>10}{'写入':>10}{'API':>6}{'用时(s)':>10}"
            )
            print("-" * 90)
            for job in payload["jobs"]:
                print(
                    f"{job['job']:<34}{job['status']:<10}{job['city_total']:>6}"
                    f"{job['rows_received']:>10,}{job['rows_written']:>10,}"
                    f"{job['api_calls']:>6}{(job['duration_ms'] or 0) / 1000:>10.1f}"
                )
            print("-" * 90)
            print(
                f"合计写入 {payload['total_rows_written']:,} 行，"
                f"API 调用 {payload['total_api_calls']} 次"
            )
        return 0

    return asyncio.run(_run())


def cmd_quality(args: argparse.Namespace) -> int:
    """执行质量评测。"""
    from atmos.db import session_scope
    from atmos.quality.engine import QualityEngine

    _ensure_catalog()
    engine = QualityEngine()
    with session_scope() as session:
        reports = engine.evaluate_all(
            session,
            window_hours=args.window,
            asset_keys=args.assets.split(",") if args.assets else None,
        )

    if args.json:
        _print([item.to_payload() for item in reports], as_json=True)
        return 0

    print(
        f"\n{'数据资产':<38}{'总分':>8}{'等级':>6}{'检查':>6}{'失败':>6}{'预警':>6}{'跳过':>6}{'城市':>6}"
    )
    print("-" * 88)
    for report in reports:
        print(
            f"{report.asset_key:<38}{report.overall:>8.2f}{report.grade:>6}"
            f"{report.checks_total:>6}{report.checks_failed:>6}{report.checks_warned:>6}"
            f"{report.checks_skipped:>6}{report.cities_evaluated:>6}"
        )
    print("-" * 88)
    if reports:
        average = sum(item.overall for item in reports) / len(reports)
        print(f"平均质量分 {average:.2f}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """打印平台状态概览。"""
    from atmos.catalog.service import overview
    from atmos.collect.runner import run_summary
    from atmos.db import session_scope

    _ensure_catalog()
    with session_scope() as session:
        data = overview(session)
    data["runs"] = run_summary(24)

    if args.json:
        _print(data, as_json=True)
        return 0

    print("\n===== 平台总览 =====")
    print(f"数据资产：{data['asset_count']} 项    字段：{data['column_count']} 个")
    print(f"城市：{data['active_city_count']} / {data['city_count']} 个启用")
    print(f"数据总量：{data['total_rows']:,} 行 / {data['total_bytes'] / 1024 / 1024:.2f} MB")
    print(f"平均质量分：{data['avg_quality_score']}    等级分布：{data['grade_distribution']}")
    print(f"SLA 超期资产：{data['sla_breach_count']} 项")
    for item in data["sla_breaches"]:
        delay = item["delay_minutes"]
        delay_text = "无数据" if delay is None else f"延迟 {delay:.0f} 分钟"
        print(f"  - {item['key']}：{delay_text} / SLA {item['sla_minutes']} 分钟")
    runs = data["runs"]
    print(
        f"近 24h 运行：{runs['total']} 次，成功率 {runs['success_rate']}%，"
        f"写入 {runs['total_rows_written']:,} 行"
    )
    return 0


def cmd_assets(args: argparse.Namespace) -> int:
    """列出数据资产。"""
    from atmos.catalog.service import list_assets
    from atmos.db import session_scope

    _ensure_catalog()
    with session_scope() as session:
        items = list_assets(session, domain=args.domain, layer=args.layer, search=args.search)

    if args.json:
        _print(items, as_json=True)
        return 0

    print(
        f"\n{'资产 Key':<38}{'分层':<10}{'域':<12}{'行数':>12}{'城市':>6}{'等级':>6}{'责任人':<12}"
    )
    print("-" * 100)
    for item in items:
        print(
            f"{item['key']:<38}{item['layer_label']:<10}{item['domain_label']:<12}"
            f"{item['row_count']:>12,}{item['city_count']:>6}{item['latest_grade'] or '-':>6}"
            f"{item['owner']:<12}"
        )
    print("-" * 100)
    print(f"共 {len(items)} 项数据资产")
    return 0


def cmd_lineage(args: argparse.Namespace) -> int:
    """打印血缘图谱。"""
    from atmos.catalog.lineage import build_graph
    from atmos.db import session_scope

    _ensure_catalog()
    with session_scope() as session:
        graph = build_graph(session)

    if args.json:
        _print(graph, as_json=True)
        return 0

    print("\n===== 数据血缘 =====")
    by_key = {node["key"]: node for node in graph["nodes"]}
    for edge in graph["edges"]:
        source = by_key.get(edge["source"], {})
        target = by_key.get(edge["target"], {})
        print(
            f"[{source.get('layer_label', '?'):<6}] {edge['source']}\n"
            f"    └─ {edge['transform_label']}（{edge['transform_ref']}）→ "
            f"[{target.get('layer_label', '?'):<6}] {edge['target']}"
        )
    stats = graph["stats"]
    print(
        f"\n节点 {stats['node_count']} 个 / 边 {stats['edge_count']} 条 / "
        f"最大深度 {stats['max_depth']} / 孤立资产 {stats['isolated_count']} 个"
    )
    return 0


def cmd_cities(args: argparse.Namespace) -> int:
    """列出城市主数据。"""
    from atmos.catalog.service import list_cities
    from atmos.db import session_scope

    _ensure_catalog()
    with session_scope() as session:
        items = list_cities(session)

    if args.json:
        _print(items, as_json=True)
        return 0

    print(f"\n{'slug':<12}{'城市':<10}{'国家':<8}{'纬度':>10}{'经度':>11}  {'时区':<20}{'有数据':>8}")
    print("-" * 84)
    for item in items:
        print(
            f"{item['slug']:<12}{item['name_zh']:<10}{item['country']:<8}"
            f"{item['latitude']:>10.4f}{item['longitude']:>11.4f}  {item['timezone']:<20}"
            f"{'是' if item['has_data'] else '否':>8}"
        )
    print("-" * 84)
    print(f"共 {len(items)} 个城市")
    return 0


def _probe_ready(url: str, *, timeout: float = 1.5) -> bool:
    """探测服务是否已经能响应健康检查。"""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/api/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _open_browser_when_ready(
    url: str,
    *,
    ready_timeout: float = 30.0,
    poll_interval: float = 0.3,
    probe: Callable[[str], bool] | None = None,
    opener: Callable[[str], bool] | None = None,
) -> threading.Thread:
    """等服务真正可访问后再用默认浏览器打开。

    不采用"启动后 sleep N 秒"的做法：机器快慢不一，睡短了会打开一个连接被拒的
    页面，睡长了纯粹是干等。这里轮询健康检查，一旦通过立刻打开；
    超时后仍然打开（此时页面可能显示连接失败，但至少地址是对的，
    而不是静默什么都不发生）。

    ``probe`` 与 ``opener`` 可注入，便于测试。
    """
    import webbrowser

    check = probe or _probe_ready
    launch = opener or webbrowser.open

    def worker() -> None:
        deadline = time.monotonic() + ready_timeout
        while time.monotonic() < deadline:
            if check(url):
                break
            time.sleep(poll_interval)
        try:
            opened = bool(launch(url))
        except Exception as exc:  # pragma: no cover - 取决于本机浏览器注册情况
            logger.warning("调用默认浏览器失败: %s", exc)
            opened = False
        if opened:
            print(f"  已用默认浏览器打开 {url}\n")
        else:
            print(f"  未能唤起默认浏览器，请手动打开 {url}\n")

    thread = threading.Thread(target=worker, daemon=True, name="atmos-open-browser")
    thread.start()
    return thread


def cmd_serve(args: argparse.Namespace) -> int:
    """启动分析与治理平台。

    支持三种门户：``analysis``（默认 8000）、``governance``（默认 8001）、
    ``both``（同时启动两个服务，便于本地一并查看）。

    默认在服务就绪后自动打开默认浏览器；``--no-open`` 或 ``ATMOS_OPEN_BROWSER=false``
    可关闭（服务器与 CI 环境建议关闭）。
    """
    import uvicorn

    settings = get_settings()
    host = args.host or settings.host
    analysis_port = args.port or settings.port
    governance_port = args.governance_port or settings.governance_port
    portal = args.portal
    level = settings.log_level.lower()

    # 浏览器要用 127.0.0.1 而不是 0.0.0.0
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    landing_url = (
        f"http://{browser_host}:{governance_port}/"
        if portal == "governance"
        else f"http://{browser_host}:{analysis_port}/"
    )

    banner: list[str] = []
    if portal in ("analysis", "both"):
        banner.append(f"  分析平台：http://{browser_host}:{analysis_port}/      接口文档 /api/docs")
    if portal in ("governance", "both"):
        banner.append(f"  治理平台：http://{browser_host}:{governance_port}/      接口文档 /api/docs")

    print("\n  城市天气与空气质量数据资产管理平台")
    print("\n".join(banner))
    print("\n  Ctrl+C 停止服务\n")

    should_open = settings.open_browser and not args.no_open
    if should_open:
        _open_browser_when_ready(landing_url)
    elif portal == "both":
        print("  已关闭自动打开浏览器（--no-open）\n")

    if portal == "both":
        # 两个服务各自独立监听端口，共用同一份数据湖与资产目录。
        # 单进程内并发运行两个 uvicorn Server，避免用户手动开两个窗口。
        import asyncio

        import uvicorn as uvicorn_module

        async def _serve_both() -> None:
            configs = [
                uvicorn_module.Config(
                    "atmos.apps.analysis.app:app",
                    host=host,
                    port=analysis_port,
                    log_level=level,
                ),
                uvicorn_module.Config(
                    "atmos.apps.governance.app:app",
                    host=host,
                    port=governance_port,
                    log_level=level,
                ),
            ]
            servers = [uvicorn_module.Server(config) for config in configs]
            await asyncio.gather(*(server.serve() for server in servers))

        asyncio.run(_serve_both())
        return 0

    target = (
        "atmos.apps.analysis.app:app" if portal == "analysis" else "atmos.apps.governance.app:app"
    )
    uvicorn.run(
        target,
        host=host,
        port=analysis_port if portal == "analysis" else governance_port,
        reload=args.reload,
        log_level=level,
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    """一键初始化：建表 → 引导 → 采集 → 质量评测。"""
    from atmos.collect.runner import Collector
    from atmos.db import session_scope
    from atmos.quality.engine import QualityEngine

    _ensure_catalog(reload=True)

    async def _run() -> int:
        outcome = await Collector().run(
            scope="all", trigger="cli", include_archive=not args.skip_archive
        )
        payload = outcome.to_payload()
        print(f"采集完成：状态={payload['status']}，写入 {payload['total_rows_written']:,} 行")
        return 0

    code = asyncio.run(_run())

    engine = QualityEngine()
    with session_scope() as session:
        reports = engine.evaluate_all(session)
    print(f"\n{'数据资产':<38}{'总分':>8}{'等级':>6}  说明")
    print("-" * 70)
    for item in reports:
        print(f"{item.asset_key:<38}{item.overall:>8.2f}{item.grade:>6}  {item.grade_label}")
    print("\n初始化完成，执行 `python -m atmos serve` 启动看板。")
    return code


# ==================================================================
# 参数解析
# ==================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="atmos",
        description="城市天气与空气质量数据资产管理平台（Open-Meteo 数据源）",
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    parser.add_argument("--log-level", default=None, help="日志级别，默认取配置")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="一键初始化：建表 → 引导 → 采集 → 质量评测")
    init.add_argument("--skip-archive", action="store_true", help="跳过历史归档回补（更快）")

    subparsers.add_parser("bootstrap", help="同步 config/*.yaml 到资产目录")

    collect = subparsers.add_parser("collect", help="执行采集管道")
    collect.add_argument(
        "--scope",
        default="all",
        choices=["all", "collect", "derive", "reference"],
        help="采集范围",
    )
    collect.add_argument("--cities", default=None, help="逗号分隔的城市 slug")
    collect.add_argument("--skip-archive", action="store_true", help="跳过历史归档回补")

    quality = subparsers.add_parser("quality", help="执行五维质量评测")
    quality.add_argument("--window", type=int, default=None, help="评测回溯窗口（小时）")
    quality.add_argument("--assets", default=None, help="逗号分隔的资产 key")

    subparsers.add_parser("status", help="打印平台状态概览")

    assets = subparsers.add_parser("assets", help="列出数据资产")
    assets.add_argument("--domain", default=None)
    assets.add_argument("--layer", default=None)
    assets.add_argument("--search", default=None)

    subparsers.add_parser("lineage", help="打印数据血缘")
    subparsers.add_parser("cities", help="列出城市主数据")

    serve = subparsers.add_parser("serve", help="启动分析与治理平台")
    serve.add_argument(
        "--portal",
        default="analysis",
        choices=["analysis", "governance", "both"],
        help="启动哪个门户：analysis（默认 8000）/ governance（默认 8001）/ both",
    )
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None, help="分析平台端口，默认 8000")
    serve.add_argument(
        "--governance-port", type=int, default=None, help="治理平台端口，默认 8001"
    )
    serve.add_argument("--reload", action="store_true", help="开发模式自动重载")
    serve.add_argument(
        "--no-open",
        action="store_true",
        help="不自动打开默认浏览器（服务器与 CI 环境建议加上）",
    )

    return parser


COMMANDS = {
    "init": cmd_init,
    "bootstrap": cmd_bootstrap,
    "collect": cmd_collect,
    "quality": cmd_quality,
    "status": cmd_status,
    "assets": cmd_assets,
    "lineage": cmd_lineage,
    "cities": cmd_cities,
    "serve": cmd_serve,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging(args.log_level or settings.log_level)

    handler = COMMANDS.get(args.command)
    if handler is None:  # pragma: no cover - argparse 已保证
        parser.error(f"未知命令: {args.command}")
        return 2
    try:
        return handler(args)
    except KeyboardInterrupt:  # pragma: no cover
        print("\n已中断")
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
