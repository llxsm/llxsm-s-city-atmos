"""主题色（accent）与图表配色的测试。

这个功能横跨"样式表默认值 → theme.js 运行时覆盖 → 图表取色"三层，任何一层脱节
都会表现为"界面换了色、图表没换"或"首屏闪一下别的颜色"这类只在浏览器里看得出的
问题。因此这里分两段验证：

1. **静态接线**：预设是否齐备、CSS 令牌是否在、默认值是否与 theme.js 的默认主题
   一致、外壳是否真的把取色器渲染出来；
2. **行为**（用 node 真跑一遍 ``theme.js``）：推导出的令牌、序列配色是否满足
   "主色打头、不重复、非法主题回落"这些约定。

node 不是本项目的运行依赖，未安装时行为部分整体跳过。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import run_node

WEB = Path(__file__).resolve().parent.parent / "web"
STYLE = WEB / "shared" / "style.css"
THEME = WEB / "shared" / "theme.js"
CHARTS = WEB / "shared" / "charts.js"
PORTAL = WEB / "shared" / "portal.js"
INDEX = WEB / "app" / "index.html"

MIN_THEMES = 4

# { id: 'blue', label: '科技蓝', accent: '#3b82f6' }
PRESET = re.compile(r"""id:\s*'([a-z0-9-]+)',\s*label:\s*'([^']+)',\s*accent:\s*'(#[0-9a-fA-F]{6})'""")
DEFAULT_ID = re.compile(r"""DEFAULT_THEME_ID\s*=\s*'([a-z0-9-]+)'""")
CSS_VAR = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


def _strip_comments(text: str) -> str:
    """剥掉 JS 注释：文档里会提到标识符，不能据此判断代码里是否存在。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?<![:\w])//[^\n]*", "", text)
    return text


def _presets() -> list[tuple[str, str, str]]:
    found = PRESET.findall(THEME.read_text(encoding="utf-8"))
    assert len(found) >= MIN_THEMES, f"theme.js 只声明了 {len(found)} 个主题，预期至少 {MIN_THEMES} 个"
    return found


def _root_tokens() -> dict[str, str]:
    """取 ``:root`` 块里的自定义属性。"""
    css = STYLE.read_text(encoding="utf-8")
    match = re.search(r":root\s*\{(.*?)\n\}", css, flags=re.S)
    assert match, "style.css 未找到 :root 块"
    return {name: value.strip() for name, value in CSS_VAR.findall(match.group(1))}


# ==================================================================
# 1. 静态接线
# ==================================================================
def test_theme_presets_are_declared_and_unique() -> None:
    """预设必须齐备、id 与主色都不重复。"""
    presets = _presets()
    ids = [item[0] for item in presets]
    accents = [item[2].lower() for item in presets]

    assert len(set(ids)) == len(ids), f"主题 id 重复：{ids}"
    assert len(set(accents)) == len(accents), f"主题主色重复：{accents}"
    assert all(label.strip() for _, label, _ in presets), "存在没有名称的主题"


def test_default_theme_matches_css_root() -> None:
    """默认主题必须与 ``:root`` 的取值一致。

    ``:root`` 是脚本执行前的首屏取值，theme.js 的默认值在脚本执行后覆盖它。
    两者不一致时页面会先闪一下另一种颜色，因此这条必须对齐。
    """
    default_id = DEFAULT_ID.search(THEME.read_text(encoding="utf-8"))
    assert default_id, "theme.js 未声明 DEFAULT_THEME_ID"

    presets = {item[0]: item for item in _presets()}
    assert default_id.group(1) in presets, f"默认主题 {default_id.group(1)} 不在预设清单里"

    tokens = _root_tokens()
    expected = presets[default_id.group(1)][2].lower()
    assert tokens.get("--accent", "").lower() == expected, "--accent 与默认主题主色不一致"
    assert tokens.get("--series-1", "").lower() == expected, "--series-1 应为默认主题主色"

    for name in ("--accent-hover", "--accent-soft", "--accent-faint", "--accent-ink"):
        assert tokens.get(name), f"style.css 缺少 {name}"


def test_css_declares_full_series_palette() -> None:
    """CSS 必须声明完整的 ``--series-N``，供图表在脚本执行前也有配色可用。"""
    tokens = _root_tokens()
    series = [tokens.get(f"--series-{index}") for index in range(1, 11)]
    assert all(series), f"--series-1..10 不完整：{series}"
    assert len({item.lower() for item in series}) == len(series), f"序列配色有重复：{series}"


def test_shell_renders_the_theme_picker() -> None:
    """外壳必须真的把取色器挂出来，否则功能等于没接。"""
    markup = re.sub(r"<!--.*?-->", "", INDEX.read_text(encoding="utf-8"), flags=re.S)
    assert 'id="themePicker"' in markup, "index.html 缺少取色器容器"
    assert "theme-picker" in markup, "取色器容器缺少样式类"
    # 必须位于顶栏，避免插到内容区里
    assert markup.index("topbar-right") < markup.index('id="themePicker"')

    portal = PORTAL.read_text(encoding="utf-8")
    assert "/shared/theme.js" in portal, "portal.js 未引入主题模块"
    assert "initTheme()" in portal, "portal.js 未在启动时应用已记住的主题"
    assert "#themePicker" in portal, "portal.js 未渲染取色器"
    assert "applyTheme(" in portal, "portal.js 未应用主题切换"


def test_chart_colors_come_from_the_theme() -> None:
    """图表配色必须来源于主题，不能再有第二套静态调色板。

    ``charts.js`` 里出现硬编码的主色，就意味着换主题后图表颜色不会跟着变 ——
    这正是这个功能最容易退化的地方。视图侧有意保留的分类色（城市/聚类/等级）
    不受此约束。
    """
    charts = _strip_comments(CHARTS.read_text(encoding="utf-8"))
    assert "#3b82f6" not in charts, "charts.js 仍硬编码主色，应改为从 theme.js 取色"
    assert re.search(r"\bPALETTE\b", charts) is None, "charts.js 仍引用静态 PALETTE"

    # 只有 theme.js 拥有调色板定义
    for path in sorted((WEB / "shared").glob("*.js")):
        if path.name == "theme.js":
            continue
        source = _strip_comments(path.read_text(encoding="utf-8"))
        assert re.search(r"\bPALETTE\b", source) is None, f"{path.name} 仍引用静态 PALETTE"

    for path in sorted((WEB / "app" / "static" / "views").glob("*.js")):
        source = _strip_comments(path.read_text(encoding="utf-8"))
        assert re.search(r"\bPALETTE\b", source) is None, f"{path.name} 仍引用静态 PALETTE"


# ==================================================================
# 2. 行为（node 真跑一遍 theme.js）
# ==================================================================
THEME_SCRIPT = r"""
import { pathToFileURL } from 'node:url';

const WEB = process.env.ATMOS_WEB_ROOT.replace(/\\/g, '/');
const theme = await import(pathToFileURL(`${WEB}/shared/theme.js`).href);

const checks = [];
const check = (name, ok, detail = '') => checks.push({ name, ok: Boolean(ok), detail: String(detail) });

const ids = theme.THEMES.map((item) => item.id);
check('预设数量 >= 4', theme.THEMES.length >= 4, theme.THEMES.length);
check('预设 id 唯一', new Set(ids).size === ids.length, ids.join(','));
check('预设主色唯一', new Set(theme.THEMES.map((t) => t.accent)).size === theme.THEMES.length);
check('默认主题在预设内', ids.includes(theme.DEFAULT_THEME_ID), theme.DEFAULT_THEME_ID);

const HEX = /^#[0-9a-f]{6}$/i;
const RGBA = /^rgba\(\d+, \d+, \d+, 0\.\d+\)$/;

for (const item of theme.THEMES) {
  const tokens = theme.tokensFor(theme.themeById(item.id));
  const series = Object.keys(tokens)
    .filter((key) => key.startsWith('--series-'))
    .sort((a, b) => Number(a.slice(9)) - Number(b.slice(9)))
    .map((key) => tokens[key]);

  check(`[${item.id}] --accent 等于预设主色`, tokens['--accent'] === item.accent, tokens['--accent']);
  check(`[${item.id}] 序列色以主色打头`, series[0] === item.accent, series[0]);
  check(`[${item.id}] 序列色共 10 个`, series.length === 10, series.length);
  check(
    `[${item.id}] 序列色不重复`,
    new Set(series.map((color) => color.toLowerCase())).size === series.length,
    series.join(','),
  );
  check(`[${item.id}] 悬停色为合法 hex`, HEX.test(tokens['--accent-hover']), tokens['--accent-hover']);
  check(`[${item.id}] 底纹为同色半透明`, RGBA.test(tokens['--accent-soft']), tokens['--accent-soft']);
  check(`[${item.id}] 浅色文字为合法 hex`, HEX.test(tokens['--accent-ink']), tokens['--accent-ink']);
}

check(
  '不同主题的首色确实不同',
  theme.seriesPaletteFor(theme.themeById('blue'))[0] !== theme.seriesPaletteFor(theme.themeById('violet'))[0],
  `${theme.seriesPaletteFor(theme.themeById('blue'))[0]} vs ${theme.seriesPaletteFor(theme.themeById('violet'))[0]}`,
);

// 默认状态下不应依赖 DOM
check('默认主题可用（无 DOM）', HEX.test(theme.accent()), theme.accent());
check('默认序列配色可用（无 DOM）', theme.seriesPalette().length === 10, theme.seriesPalette().length);

theme.applyTheme('violet');
check('applyTheme 更新当前主题', theme.activeTheme().id === 'violet', theme.activeTheme().id);
check('主色跟随主题', theme.accent() === theme.themeById('violet').accent, theme.accent());
check('序列配色跟随主题', theme.seriesPalette()[0] === theme.themeById('violet').accent, theme.seriesPalette()[0]);

theme.applyTheme('不存在的主题');
check('非法主题回落到默认且不抛错', theme.activeTheme().id === theme.DEFAULT_THEME_ID, theme.activeTheme().id);

check('hexToRgb 支持三位缩写', JSON.stringify(theme.hexToRgb('#abc')) === '{"r":170,"g":187,"b":204}', JSON.stringify(theme.hexToRgb('#abc')));
check('hexToRgb 拒绝非法输入', theme.hexToRgb('nope') === null, String(theme.hexToRgb('nope')));
check('shade 会变暗', theme.shade('#3b82f6', 0.88) !== '#3b82f6', theme.shade('#3b82f6', 0.88));
check('tint 会变亮', theme.tint('#3b82f6', 0.55) !== '#3b82f6', theme.tint('#3b82f6', 0.55));
check('withAlpha 保留通道', theme.withAlpha('#3b82f6', 0.5) === 'rgba(59, 130, 246, 0.5)', theme.withAlpha('#3b82f6', 0.5));

console.log(JSON.stringify({ checks }));
"""


@pytest.fixture(scope="module")
def theme_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("frontend-theme") / "theme_check.mjs"
    path.write_text(THEME_SCRIPT, encoding="utf-8", newline="\n")
    return path


def test_theme_module_behaviour(theme_script: Path, node_cmd: list[str]) -> None:
    """真跑一遍 theme.js，验证令牌推导、序列配色与非法输入回落。"""
    result = run_node(node_cmd, theme_script, env_extra={"ATMOS_WEB_ROOT": str(WEB)})
    failures = [item for item in result["checks"] if not item["ok"]]
    if failures:
        detail = "\n".join(f"  {item['name']} → {item['detail']}" for item in failures)
        pytest.fail(f"{len(failures)} 项主题行为不符合预期：\n{detail}")
    assert len(result["checks"]) >= 20, f"只执行了 {len(result['checks'])} 项检查，脚本可能被裁剪"
