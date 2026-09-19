"""主题（深浅状态 + 强调色）与图表配色的测试。

这个功能横跨三层："样式表默认值 → theme.js 运行时覆盖 → 图表取色"。任何一层脱节
都会表现为只在浏览器里看得出的问题：界面换了色而图表没换、浅色下文字看不清、
首屏闪一下另一种颜色。因此这里分三段验证：

1. **静态接线**：预设是否齐备、外壳是否真的把取色器与状态开关渲染出来、
   图表是否已不再自带一套配色；
2. **行为，无 DOM**（用 node 真跑 ``theme.js``）：令牌推导、序列配色、
   两种状态的令牌集合是否一致、``ink()`` 是否随状态变化、非法输入回落；
3. **行为，带 DOM 桩**：``applyMode`` 是否写了 ``<html data-mode>``、``color-scheme``
   与浅色令牌 —— 覆盖"状态切了但浏览器原生控件没跟上"的问题；
4. **不漂移**：``style.css`` 的 ``:root`` 必须与 ``MODES.dark`` 逐项相等。

node 不是本项目的运行依赖，2/3 两段在未安装 node 时跳过。
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

# 与状态/强调色无关、因此不像 MODES 那样由 theme.js 生成的令牌
STATIC_TOKENS = {
    "--ok", "--warn", "--danger", "--info", "--purple",
    "--grade-a", "--grade-b", "--grade-c", "--grade-d", "--grade-e", "--grade-none",
    "--layer-reference", "--layer-raw", "--layer-refined", "--layer-serving",
    "--radius", "--radius-sm", "--radius-lg", "--sidebar-w", "--gap", "--mono",
}

# { id: 'blue', label: '科技蓝', accent: '#3b82f6' }
PRESET = re.compile(r"""id:\s*'([a-z0-9-]+)',\s*label:\s*'([^']+)',\s*accent:\s*'(#[0-9a-fA-F]{6})'""")
CSS_VAR = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")


def _strip_comments(text: str) -> str:
    """剥掉 JS 注释：文档里会提到标识符，不能据此判断代码里是否存在。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?<![:\w])//[^\n]*", "", text)
    return text


def _presets() -> list[tuple[str, str, str]]:
    found = PRESET.findall(THEME.read_text(encoding="utf-8"))
    assert len(found) >= MIN_THEMES, f"theme.js 只声明了 {len(found)} 个强调色，预期至少 {MIN_THEMES} 个"
    return found


def _root_tokens() -> dict[str, str]:
    """取样式表 ``:root`` 块里的自定义属性。"""
    css = STYLE.read_text(encoding="utf-8")
    match = re.search(r":root\s*\{(.*?)\n\}", css, flags=re.S)
    assert match, "style.css 未找到 :root 块"
    return {name: value.strip() for name, value in CSS_VAR.findall(match.group(1))}


# ==================================================================
# 1. 静态接线
# ==================================================================
def test_theme_presets_are_declared_and_unique() -> None:
    """强调色预设必须齐备、id 与取值都不重复。"""
    presets = _presets()
    ids = [item[0] for item in presets]
    accents = [item[2].lower() for item in presets]

    assert len(set(ids)) == len(ids), f"主题 id 重复：{ids}"
    assert len(set(accents)) == len(accents), f"主题主色重复：{accents}"
    assert all(label.strip() for _, label, _ in presets), "存在没有名称的主题"


def test_css_declares_full_series_palette() -> None:
    """CSS 必须声明完整的 ``--series-N``，供图表在脚本执行前也有配色可用。"""
    tokens = _root_tokens()
    series = [tokens.get(f"--series-{index}") for index in range(1, 11)]
    assert all(series), f"--series-1..10 不完整：{series}"
    assert len({item.lower() for item in series}) == len(series), f"序列配色有重复：{series}"


def test_shell_renders_theme_controls() -> None:
    """外壳必须真的把取色器与深浅开关挂出来，否则功能等于没接。"""
    markup = re.sub(r"<!--.*?-->", "", INDEX.read_text(encoding="utf-8"), flags=re.S)
    assert 'id="themePicker"' in markup, "index.html 缺少取色器容器"
    assert "theme-picker" in markup, "取色器容器缺少样式类"
    assert 'id="modeToggle"' in markup, "index.html 缺少深浅状态开关"
    # 都必须位于顶栏，避免插到内容区里
    assert markup.index("topbar-right") < markup.index('id="themePicker"')
    assert markup.index("topbar-right") < markup.index('id="modeToggle"')

    portal = PORTAL.read_text(encoding="utf-8")
    assert "/shared/theme.js" in portal, "portal.js 未引入主题模块"
    assert "initTheme()" in portal, "portal.js 未在启动时应用已记住的主题"
    assert "#themePicker" in portal, "portal.js 未渲染取色器"
    assert "applyTheme(" in portal, "portal.js 未应用强调色切换"
    assert "#modeToggle" in portal, "portal.js 未渲染状态开关"
    assert "applyMode(" in portal, "portal.js 未应用深浅状态切换"


def test_chart_colors_come_from_the_theme() -> None:
    """图表配色必须来源于主题，不能再有第二套静态调色板。

    ``charts.js`` 里出现硬编码的强调色或中性色，就意味着换主题/换状态后图表颜色
    不会跟着变 —— 这正是这个功能最容易退化的地方。视图侧有意保留的分类色与
    语义色（城市/聚类/等级）不受此约束。
    """
    charts = _strip_comments(CHARTS.read_text(encoding="utf-8"))
    assert "#3b82f6" not in charts, "charts.js 仍硬编码强调色，应改为从 theme.js 取色"
    assert "#7b889e" not in charts, "charts.js 仍硬编码中性色，应改为 ink()"
    assert re.search(r"\bINK\b", charts) is None, "charts.js 仍有 INK 常量；只有 ink() 才随状态变化"
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
# 2 & 3. 行为（node 真跑 theme.js）
# ==================================================================
THEME_SCRIPT = r"""
import { pathToFileURL } from 'node:url';

const WEB = process.env.ATMOS_WEB_ROOT.replace(/\\/g, '/');
const theme = await import(pathToFileURL(`${WEB}/shared/theme.js`).href);

const checks = [];
const check = (name, ok, detail = '') => checks.push({ name, ok: Boolean(ok), detail: String(detail) });

const HEX = /^#[0-9a-f]{6}$/i;
const RGBA = /^rgba\(\d+, \d+, \d+, 0\.\d+\)$/;

// ---------------------------------------------------------------- 强调色
const ids = theme.THEMES.map((item) => item.id);
check('预设数量 >= 4', theme.THEMES.length >= 4, theme.THEMES.length);
check('预设 id 唯一', new Set(ids).size === ids.length, ids.join(','));
check('预设主色唯一', new Set(theme.THEMES.map((t) => t.accent)).size === theme.THEMES.length);
check('默认强调色在预设内', ids.includes(theme.DEFAULT_THEME_ID), theme.DEFAULT_THEME_ID);

for (const item of theme.THEMES) {
  const tokens = theme.tokensFor(theme.themeById(item.id));
  const series = Object.keys(tokens)
    .filter((key) => key.startsWith('--series-'))
    .sort((a, b) => Number(a.slice(9)) - Number(b.slice(9)))
    .map((key) => tokens[key]);

  check(`[${item.id}] --accent 等于预设主色`, tokens['--accent'] === item.accent, tokens['--accent']);
  check(`[${item.id}] 序列色以强调色打头`, series[0] === item.accent, series[0]);
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

// ---------------------------------------------------------------- 深浅状态
const darkKeys = Object.keys(theme.MODES.dark).sort();
const lightKeys = Object.keys(theme.MODES.light).sort();
const onlyOneSide = darkKeys.filter((k) => !lightKeys.includes(k)).concat(lightKeys.filter((k) => !darkKeys.includes(k)));
check(
  '两种状态声明了完全相同的令牌集合',
  JSON.stringify(darkKeys) === JSON.stringify(lightKeys),
  `只在一侧出现的令牌：${onlyOneSide.join(',') || '无'}`,
);
check('状态默认是深色', theme.DEFAULT_MODE === 'dark', theme.DEFAULT_MODE);

// 结构性令牌必须在两态取值不同，否则等于没做浅色
for (const name of ['--bg', '--surface', '--text', '--border', '--chart-tooltip-bg', '--ok-ink']) {
  check(
    `${name} 在深浅两态取值不同`,
    theme.MODES.dark[name] !== theme.MODES.light[name],
    `${theme.MODES.dark[name]} vs ${theme.MODES.light[name]}`,
  );
}

// --accent-ink 是"柔和底纹上的文字"，两态必须朝相反方向推导：
// 深色底要浅色调，白底要深色调。两边同向就会有一边不可读。
const darkAccentInk = theme.tokensFor(theme.themeById('blue'), 'dark')['--accent-ink'];
const lightAccentInk = theme.tokensFor(theme.themeById('blue'), 'light')['--accent-ink'];
check('--accent-ink 在两态取值不同', darkAccentInk !== lightAccentInk, `${darkAccentInk} vs ${lightAccentInk}`);
check(
  '浅色态的 --accent-ink 是深色调（比强调色更暗）',
  theme.hexToRgb(lightAccentInk).r <= theme.hexToRgb('#3b82f6').r
    && theme.hexToRgb(lightAccentInk).g <= theme.hexToRgb('#3b82f6').g,
  lightAccentInk,
);

// ---------------------------------------------------------------- ink() 随状态变化
theme.applyMode('dark');
const darkInk = theme.ink();
check('深色下 ink.grid 取深色边框', darkInk.grid === theme.MODES.dark['--border'], darkInk.grid);
check('深色下 ink.text 取深色文本', darkInk.text === theme.MODES.dark['--text'], darkInk.text);
check('ink 返回 14 个字段', Object.keys(darkInk).length === 14, Object.keys(darkInk).length);

theme.applyMode('light');
const lightInk = theme.ink();
check('切浅色后 ink 缓存已失效', lightInk !== darkInk, '返回了同一个对象');
check('浅色下 ink.grid 取浅色边框', lightInk.grid === theme.MODES.light['--border'], lightInk.grid);
check('浅色下 ink.text 取浅色文本', lightInk.text === theme.MODES.light['--text'], lightInk.text);
check('浅色下 ink.warnInk 用的是加深取值', lightInk.warnInk === theme.MODES.light['--warn-ink'], lightInk.warnInk);

// ---------------------------------------------------------------- 非法输入与颜色推导
theme.applyMode('不存在');
check('非法状态回落到默认且不抛错', theme.activeModeId() === theme.DEFAULT_MODE, theme.activeModeId());
theme.applyTheme('不存在的主题');
check('非法强调色回落到默认且不抛错', theme.activeTheme().id === theme.DEFAULT_THEME_ID, theme.activeTheme().id);

check('hexToRgb 支持三位缩写', JSON.stringify(theme.hexToRgb('#abc')) === '{"r":170,"g":187,"b":204}', JSON.stringify(theme.hexToRgb('#abc')));
check('hexToRgb 拒绝非法输入', theme.hexToRgb('nope') === null, String(theme.hexToRgb('nope')));
check('shade 会变暗', theme.shade('#3b82f6', 0.88) !== '#3b82f6', theme.shade('#3b82f6', 0.88));
check('tint 会变亮', theme.tint('#3b82f6', 0.55) !== '#3b82f6', theme.tint('#3b82f6', 0.55));
check('withAlpha 保留通道', theme.withAlpha('#3b82f6', 0.5) === 'rgba(59, 130, 246, 0.5)', theme.withAlpha('#3b82f6', 0.5));

// ---------------------------------------------------------------- 带 DOM 桩：applyMode 的写入行为
const written = new Map();
globalThis.document = {
  documentElement: {
    dataset: {},
    style: {
      colorScheme: '',
      setProperty(name, value) { written.set(name, value); },
    },
  },
};

theme.applyMode('light');
check('applyMode 写入 <html data-mode>', document.documentElement.dataset.mode === 'light', document.documentElement.dataset.mode);
check('applyMode 设置 color-scheme', document.documentElement.style.colorScheme === 'light', document.documentElement.style.colorScheme);
check('applyMode 写入浅色 --bg', written.get('--bg') === theme.MODES.light['--bg'], written.get('--bg'));
check('applyMode 写入浅色 --text', written.get('--text') === theme.MODES.light['--text'], written.get('--text'));
check('applyMode 同时写入强调色令牌', HEX.test(written.get('--accent')), written.get('--accent'));
check('applyMode 写入全部序列配色', written.get('--series-10') === theme.seriesPalette()[9], written.get('--series-10'));

theme.applyMode('dark');
check('切回深色时 data-mode 同步', document.documentElement.dataset.mode === 'dark', document.documentElement.dataset.mode);
check('切回深色时 color-scheme 同步', document.documentElement.style.colorScheme === 'dark', document.documentElement.style.colorScheme);
check('切回深色时 --bg 覆盖为深色', written.get('--bg') === theme.MODES.dark['--bg'], written.get('--bg'));

// 供 Python 侧比对 style.css 的 :root，并做对比度校验
const tokens = {
  dark: theme.tokensFor(theme.themeById(theme.DEFAULT_THEME_ID), 'dark'),
  light: theme.tokensFor(theme.themeById(theme.DEFAULT_THEME_ID), 'light'),
};

console.log(JSON.stringify({ checks, tokens }));
"""


@pytest.fixture(scope="module")
def theme_result(tmp_path_factory: pytest.TempPathFactory, node_cmd: list[str]) -> dict:
    path = tmp_path_factory.mktemp("frontend-theme") / "theme_check.mjs"
    path.write_text(THEME_SCRIPT, encoding="utf-8", newline="\n")
    return run_node(node_cmd, path, env_extra={"ATMOS_WEB_ROOT": str(WEB)})


def test_theme_module_behaviour(theme_result: dict) -> None:
    """真跑一遍 theme.js：令牌推导、状态切换、ink 缓存失效、非法输入回落、DOM 写入。"""
    failures = [item for item in theme_result["checks"] if not item["ok"]]
    if failures:
        detail = "\n".join(f"  {item['name']} → {item['detail']}" for item in failures)
        pytest.fail(f"{len(failures)} 项主题行为不符合预期：\n{detail}")
    assert len(theme_result["checks"]) >= 40, f"只执行了 {len(theme_result['checks'])} 项检查，脚本可能被裁剪"


def test_css_root_matches_dark_mode_tokens(theme_result: dict) -> None:
    """``:root`` 必须与 ``MODES.dark`` 逐项相等。

    ``:root`` 是脚本执行前的首屏取值，theme.js 的默认令牌在脚本执行后被写到
    内联样式上覆盖它。两者不一致时，页面会先闪一下另一种颜色再跳成主题色 ——
    这条断言就是为了让"改了 theme.js 忘了同步样式表"必然失败。
    """
    expected = theme_result["tokens"]["dark"]
    actual = _root_tokens()

    mismatched = {
        name: (value, actual.get(name))
        for name, value in expected.items()
        if actual.get(name) != value
    }
    assert not mismatched, "\n".join(
        f"  {name}: theme.js={want!r} style.css={got!r}" for name, (want, got) in sorted(mismatched.items())
    )

    # 反向：:root 里不该有 MODES 之外的"动态"令牌（静态令牌除外）
    extra = sorted(set(actual) - set(expected) - STATIC_TOKENS)
    assert not extra, f":root 多出未由 theme.js 管理的令牌：{extra}"

    # 静态令牌必须还在（它们不随状态变化，因此不进 MODES）
    missing_static = sorted(STATIC_TOKENS - set(actual))
    assert not missing_static, f":root 缺少静态令牌：{missing_static}"


# ==================================================================
# 4. 可读性（对比度）
# ==================================================================
RGBA = re.compile(r"rgba?\(\s*(\d+),\s*(\d+),\s*(\d+)(?:,\s*([\d.]+))?\s*\)")

# (前景, 背景, 最低对比度)。正文按 WCAG AA（4.5）留余量取 7.0；
# 图例/次要文字按次级要求；语义色文字要与深色底的版本同等可读。
CONTRAST_PAIRS = [
    ("--text", "--surface", 7.0),
    ("--text-dim", "--surface", 4.5),
    ("--text-mute", "--surface", 4.0),
    ("--text", "--bg", 7.0),
    ("--text-dim", "--bg", 4.5),
    ("--accent-ink", "--accent-soft", 4.5),
    ("--ok-ink", "--ok-soft", 4.0),
    ("--warn-ink", "--warn-soft", 4.0),
    ("--danger-ink", "--danger-soft", 4.0),
    ("--info-ink", "--info-soft", 4.0),
    ("--purple-ink", "--purple-soft", 4.0),
    ("--neutral-ink", "--neutral-soft", 4.0),
]


def _parse_color(color: str) -> tuple[int, int, int, float]:
    """``#rgb`` / ``#rrggbb`` / ``rgb()`` / ``rgba()`` → (r, g, b, a)。"""
    match = RGBA.match(color.strip())
    if match:
        return (
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
            float(match.group(4) or 1.0),
        )
    text = color.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), 1.0)


def _over(top: str, bottom: str) -> str:
    """把（可能半透明的）top 合成到不透明底层 bottom 上。

    徽标类是"语义色 14% 透明叠在面板上"，只拿语义色本身比对比度会高估可读性。
    """
    top_r, top_g, top_b, alpha = _parse_color(top)
    base_r, base_g, base_b, _ = _parse_color(bottom)
    blend = lambda a, b: round(a * alpha + b * (1 - alpha))  # noqa: E731
    return "#%02x%02x%02x" % (blend(top_r, base_r), blend(top_g, base_g), blend(top_b, base_b))


def _luminance(color: str) -> float:
    red, green, blue, _ = _parse_color(color)

    def channel(value: float) -> float:
        value /= 255
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)


def _contrast(foreground: str, background: str) -> float:
    first, second = _luminance(foreground), _luminance(background)
    high, low = max(first, second), min(first, second)
    return (high + 0.05) / (low + 0.05)


def test_text_contrast_is_readable_in_both_modes(theme_result: dict) -> None:
    """两种状态下文字都要看得清。

    这是本项目里最值得自动化的"外观"检查：浅色主题最容易犯的错就是照搬深色底上
    那套浅色文字（例如把强调色向白混合后当文字色），结果在白底上几乎不可见，
    而这只有肉眼能发现。这里按 WCAG 相对亮度把关键组合逐对算一遍。
    """
    problems: list[str] = []
    for mode, tokens in theme_result["tokens"].items():
        surface = tokens["--surface"]
        for foreground, background, minimum in CONTRAST_PAIRS:
            background_value = tokens[background]
            effective = _over(background_value, surface) if background_value.startswith(("rgba", "rgb(")) else background_value
            ratio = _contrast(tokens[foreground], effective)
            if ratio < minimum:
                problems.append(
                    f"  [{mode}] {foreground} 在 {background} 上对比度只有 {ratio:.2f}（要求 ≥ {minimum}）"
                )

    assert not problems, "以下组合在对应状态下可读性不足：\n" + "\n".join(problems)

