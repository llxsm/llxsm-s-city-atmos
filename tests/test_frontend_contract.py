"""前端与后端契约一致性测试（统一前端）。

前端是无构建步骤的静态资源，最容易出现的回归是"调用了不存在的端点"
或"引用了不存在的共享模块"——这两类问题后端测试都捕获不到。

两个平台的前端已合并为一个入口（``web/app``），由 8000 与 8001 两个服务
托管同一份代码，靠 ``/portal-config.js`` 注入身份区分。因此校验重点变成：

1. **每个门户的视图只能调用该门户自己那台服务上存在的接口** ——
   合并前端后一个页面同时能访问两个后端，"调错门户"成为最需要防住的错误；
2. 门户 → 视图的映射从 ``portal.config.js`` 解析，而不是在测试里硬编码文件名；
3. 服务端的职责边界不因前端合并而放宽（见 ``test_portals.py``）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parent.parent / "web"
APP = WEB / "app"
PORTALS = ("analysis", "governance")
MIN_VIEWS = 16

REQUEST_CALL = re.compile(r"request\(\s*[`'\"](/[A-Za-z0-9_\-/.${}]*)[`'\"]")
API_LITERAL = re.compile(r"[`'\"](/api/[A-Za-z0-9_\-/.${}]*)[`'\"]")
SHARED_IMPORT = re.compile(r"""(?:from|import)\s*\(?\s*['"](/shared/[A-Za-z0-9_\-/.]+\.js)['"]""")
MODULE_DECL = re.compile(r"""module\s*:\s*['"](/static/views/[A-Za-z0-9_\-/.]+\.js)['"]""")

PLACEHOLDER = "<v>"


# ==================================================================
# 工具
# ==================================================================
def _strip_comments(text: str) -> str:
    """去掉注释，避免把文档里提到的路径误判成真实调用。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = text.replace("://", "\x00//")
    text = re.sub(r"//[^\n]*", "", text)
    return text.replace("\x00//", "://")


def _templatize(path: str) -> str:
    path = path.split("?")[0]
    path = re.sub(r"\$\{[^}]*\}", PLACEHOLDER, path)
    path = re.sub(r"\{[^}]*\}", PLACEHOLDER, path)
    return path.rstrip("/") or "/"


def _head(templated: str) -> str:
    index = templated.find(PLACEHOLDER)
    return templated if index < 0 else templated[:index]


def _pattern(templated: str) -> re.Pattern[str]:
    parts = [re.escape(item) for item in templated.split(PLACEHOLDER)]
    return re.compile("^" + "[^/]+".join(parts) + "$")


def _matches(reference: str, registered: set[str]) -> bool:
    """精确 → 前缀 → 模板，三层判定一个引用是否命中注册路径。"""
    head = _head(reference)
    if head.endswith("/"):
        if any(item.startswith(head) for item in registered):
            return True
    elif any(item == head or item.startswith(head + "/") for item in registered):
        return True
    return any(_pattern(item).match(reference) for item in registered if PLACEHOLDER in item)


def _scan(files: list[Path], base: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for file in files:
        text = _strip_comments(file.read_text(encoding="utf-8"))
        paths: set[str] = set()
        for item in REQUEST_CALL.findall(text):
            paths.add(_templatize(item if item.startswith("/api") else f"/api{item}"))
        for item in API_LITERAL.findall(text):
            if item.startswith("/api/"):
                paths.add(_templatize(item))
        if paths:
            found[str(file.relative_to(base))] = paths
    return found


def _shared_sources() -> list[Path]:
    return sorted((WEB / "shared").rglob("*.js"))


def _view_sources() -> list[Path]:
    return sorted((APP / "static" / "views").glob("*.js"))


# ==================================================================
# 夹具
# ==================================================================
@pytest.fixture(scope="module")
def portal_paths() -> dict[str, set[str]]:
    """每个门户 OpenAPI 中注册的路径（模板化后）。"""
    from atmos.apps.analysis.app import create_app as create_analysis
    from atmos.apps.governance.app import create_app as create_governance

    return {
        "analysis": {_templatize(item) for item in create_analysis().openapi()["paths"]},
        "governance": {_templatize(item) for item in create_governance().openapi()["paths"]},
    }


@pytest.fixture(scope="module")
def portal_views() -> dict[str, set[str]]:
    """从 ``portal.config.js`` 解析出 门户 → 视图文件名集合。"""
    text = (APP / "static" / "portal.config.js").read_text(encoding="utf-8")
    anchors = [
        (match.start(), match.group(1))
        for match in re.finditer(r"\b(analysis|governance)\s*:", text)
    ]
    assert anchors, "portal.config.js 未找到 analysis/governance 门户定义"

    mapping: dict[str, set[str]] = {portal: set() for portal in PORTALS}
    for index, (position, portal) in enumerate(anchors):
        end = anchors[index + 1][0] if index + 1 < len(anchors) else len(text)
        for module in MODULE_DECL.findall(text[position:end]):
            mapping[portal].add(Path(module).name)
    return mapping


@pytest.fixture(scope="module")
def view_references() -> dict[str, set[str]]:
    return _scan(_view_sources(), WEB)


@pytest.fixture(scope="module")
def shared_references() -> dict[str, set[str]]:
    return _scan(_shared_sources(), WEB)


# ==================================================================
# 结构
# ==================================================================
def test_unified_app_exists() -> None:
    """统一前端的骨架必须在位。"""
    assert (APP / "index.html").exists(), "缺少统一前端外壳 web/app/index.html"
    assert (APP / "static" / "portal.config.js").exists(), "缺少门户导航配置"
    assert len(_view_sources()) >= MIN_VIEWS, [item.name for item in _view_sources()]

    for name in ("api.js", "ui.js", "charts.js", "tasks.js", "portal.js", "style.css"):
        assert (WEB / "shared" / name).exists(), f"缺少共享模块 {name}"


def test_legacy_portal_directories_are_gone() -> None:
    """旧的按门户分目录结构必须已删除，否则会出现两份前端各自漂移。"""
    assert not (WEB / "analysis").exists(), "web/analysis 应已合并进 web/app"
    assert not (WEB / "governance").exists(), "web/governance 应已合并进 web/app"
    assert not (WEB / "static").exists(), "web/static 应已合并进 web/shared"


def test_index_html_boot_order() -> None:
    """外壳必须按 图表库 → 门户配置 → 应用 的顺序加载。

    判定前先剥掉 HTML 注释：注释里会提到脚本名（"标题由 portal.js 改写"之类），
    那不是加载顺序，据此判断会得到假阳性。
    """
    html = (APP / "index.html").read_text(encoding="utf-8")
    assert 'type="module"' in html
    for token in ("/shared/portal.js", "/portal-config.js", "echarts"):
        assert token in html, f"index.html 缺少 {token}"

    markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    assert markup.index("/portal-config.js") < markup.index("/shared/portal.js"), (
        "门户配置必须在应用脚本之前加载，否则 api.js 读不到后端地址"
    )


def test_index_html_declares_portal_switcher() -> None:
    """顶栏必须有门户切换器容器 —— 这是"一个页面一键切换"的落点。"""
    markup = re.sub(r"<!--.*?-->", "", (APP / "index.html").read_text(encoding="utf-8"), flags=re.S)
    assert 'id="portalSwitch"' in markup, "顶栏缺少门户切换器容器"


def test_portal_config_registers_both_portals(portal_views: dict[str, set[str]]) -> None:
    """两个门户都必须注册视图，且每个 module 路径都真实存在。"""
    for portal in PORTALS:
        assert portal_views[portal], f"{portal} 未注册任何视图"

    missing = [
        f"{portal}: {name}"
        for portal, names in portal_views.items()
        for name in sorted(names)
        if not (APP / "static" / "views" / name).exists()
    ]
    assert missing == [], "portal.config.js 指向了不存在的视图:\n  " + "\n  ".join(missing)


def test_no_orphan_view_files(portal_views: dict[str, set[str]]) -> None:
    """磁盘上的每个视图文件都应在某个门户的导航里注册，不能有孤儿页面。"""
    registered = {name for names in portal_views.values() for name in names}
    on_disk = {item.name for item in _view_sources()}
    assert on_disk == registered, f"未注册的视图: {sorted(on_disk - registered)}"


def test_only_one_frontend_entry_exists() -> None:
    """前端只有一个入口 —— 两个服务托管同一份代码，不存在第二套外壳。"""
    indexes = sorted(WEB.rglob("index.html"))
    assert indexes == [APP / "index.html"], [str(item.relative_to(WEB)) for item in indexes]


# ==================================================================
# 契约
# ==================================================================
def test_each_view_only_calls_its_own_portal(
    portal_paths: dict[str, set[str]],
    portal_views: dict[str, set[str]],
    view_references: dict[str, set[str]],
) -> None:
    """核心断言：某个门户的视图不得调用另一个门户专属的接口。

    合并前端后一个页面能同时访问两个后端，"调错门户"是最需要防住的错误 ——
    这类问题在浏览器里表现为 404 或空图，很难定位。
    """
    problems: list[str] = []
    for portal, names in portal_views.items():
        other_portal = "governance" if portal == "analysis" else "analysis"
        for name in sorted(names):
            key = f"app/static/views/{name}"
            for path in sorted(view_references.get(key, set())):
                if _matches(path, portal_paths[portal]):
                    continue
                where = (
                    "调用到了另一个门户的接口"
                    if _matches(path, portal_paths[other_portal])
                    else "两个门户都没有这个接口"
                )
                problems.append(f"{name} [{portal}] -> {path}  ← {where}")
    assert problems == [], "视图调用了不可用的接口:\n  " + "\n  ".join(problems)


def test_shared_api_client_is_valid_against_both_portals(
    portal_paths: dict[str, set[str]], shared_references: dict[str, set[str]]
) -> None:
    """共享 api.js 覆盖两个门户的全部端点，因此对并集校验。"""
    union = portal_paths["analysis"] | portal_paths["governance"]
    unknown: list[str] = []
    for file, paths in shared_references.items():
        for path in sorted(paths):
            if not _matches(path, union):
                unknown.append(f"{file}: {path}")
    assert unknown == [], "共享库引用了两个平台都没有的端点:\n  " + "\n  ".join(unknown)


def test_shared_imports_resolve() -> None:
    """所有 ``/shared/...`` 导入都必须指向真实存在的文件。"""
    missing: list[str] = []
    candidates = [*_shared_sources(), *_view_sources(), APP / "static" / "portal.config.js"]
    for file in candidates:
        if not file.exists():
            continue
        for specifier in SHARED_IMPORT.findall(file.read_text(encoding="utf-8")):
            if not (WEB / specifier.lstrip("/")).exists():
                missing.append(f"{file.relative_to(WEB)} -> {specifier}")
    assert missing == [], "共享模块引用缺失:\n  " + "\n  ".join(missing)


def test_api_client_is_the_only_network_egress() -> None:
    """所有网络请求必须经过共享 api.js，不得绕过统一错误处理。

    只匹配**全局** ``fetch(...)``；``tasks.js`` 允许调用方注入名为 fetch 的
    数据源，那是依赖注入而非直接使用全局网络 API。
    """
    global_fetch = re.compile(r"(?<![.\w$])fetch\s*\(")
    offenders: list[str] = []
    for file in [*_shared_sources(), *_view_sources()]:
        if file.name == "api.js":
            continue
        if global_fetch.search(_strip_comments(file.read_text(encoding="utf-8"))):
            offenders.append(str(file.relative_to(WEB)))
    assert offenders == [], f"以下模块直接调用全局 fetch: {offenders}"


def test_api_client_reads_injected_endpoints() -> None:
    """api.js 必须从注入的 ``__ATMOS_PORTALS__`` 取后端地址，而不是写死。"""
    text = (WEB / "shared" / "api.js").read_text(encoding="utf-8")
    assert "__ATMOS_PORTALS__" in text, "api.js 未读取注入的门户配置"
    assert "endpoints" in text


def test_portal_shell_handles_a_down_backend() -> None:
    """外壳必须对"另一个后端没启动"有降级处理，而不是白屏。"""
    text = (WEB / "shared" / "portal.js").read_text(encoding="utf-8")
    assert "未启动" in text or "无法连接" in text or "不可用" in text, (
        "portal.js 未见后端不可用时的中文提示"
    )


def test_core_endpoints_are_reachable_from_the_frontend(
    view_references: dict[str, set[str]],
    shared_references: dict[str, set[str]],
) -> None:
    """核心页面所需的端点都应能从统一前端到达。"""
    referenced = {path for paths in view_references.values() for path in paths}
    referenced |= {path for paths in shared_references.values() for path in paths}

    for required in (
        # 分析侧
        "/api/env/current",
        "/api/env/hourly",
        "/api/env/compare",
        "/api/insights/digest",
        "/api/insights/compliance",
        "/api/insights/episodes",
        "/api/insights/relationship",
        "/api/insights/clustering",
        "/api/insights/distribution",
        "/api/ops/collect",
        # 治理侧
        "/api/assets",
        "/api/lineage/graph",
        "/api/quality/summary",
        "/api/ops/runs",
        "/api/export/metadata",
    ):
        assert _matches(_templatize(required), referenced), f"统一前端未调用 {required}"
