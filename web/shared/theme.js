/**
 * theme.js —— 主题（深/浅状态 + 强调色）与图表配色。
 *
 * 两个正交的维度
 * --------------
 * 1. **状态（mode）**：``dark`` / ``light``。决定背景、面板、边框、文字、网格、
 *    提示框，以及语义色在白底上是否还看得清；
 * 2. **强调色（accent）**：6 个预设。决定按钮、导航高亮、图表序列配色。
 *
 * 为什么令牌只写在这里
 * --------------------
 * 界面颜色有两条消费路径：样式表（CSS 变量）与图表（ECharts 的 option）。
 * 若两者各自维护一套取值，浅色模式下必然出现"面板变白了、图表还是深色轴"的错配。
 * 因此这里做**唯一来源**：``applyTheme`` / ``applyMode`` 把令牌写到
 * ``document.documentElement`` 的 CSS 变量上；图表侧通过 ``ink()`` / ``palette()``
 * 读同一份内存副本（不读 DOM，因此在没有 DOM 的 Node 环境里也能求值，测试会用到）。
 *
 * ``style.css`` 的 ``:root`` 保留一套与 ``MODES.dark`` 相同的取值，作用只是
 * **脚本执行前的首屏**不闪色；两处必须一致，测试里有逐项断言守着。
 *
 * 有意不随主题变化的颜色
 * ----------------------
 * 评级 A–E、空气质量分级、血缘分层的**底色**，以及城市/聚类这类分类色：它们承载
 * 含义、或用于区分实体，必须在任何主题下保持稳定，因此在各自视图里硬编码是有意为之。
 * 会变的是这些语义色对应的**文字色**（``--ok-ink`` 之类）—— 为深色底挑的浅色文字
 * 直接放到白底上不可读，这类必须换。
 */

const THEME_KEY = 'atmos.theme';
const MODE_KEY = 'atmos.mode';

// ==================================================================
// 状态主题（深 / 浅）
// ==================================================================

/**
 * 两套结构色与语义色。键就是 CSS 变量名。
 * 新增令牌时**必须两边都加**，否则浅色模式下会漏掉一处（测试会检查两边键一致）。
 */
export const MODES = {
  dark: {
    // 背景与表面
    '--bg': '#0b1120',
    '--bg-soft': '#0e1628',
    '--surface': '#151d2e',
    '--surface-2': '#1a2337',
    '--surface-3': '#202b41',
    '--surface-hover': '#1e2942',
    '--border': '#263148',
    '--border-strong': '#33415c',
    // 文本
    '--text': '#e6ebf4',
    '--text-dim': '#a9b5c9',
    '--text-mute': '#7b889e',
    // 半透明表面（顶栏、分段控件、表格容器等）
    '--glass': 'rgba(21, 29, 46, 0.8)',
    '--glass-soft': 'rgba(21, 29, 46, 0.7)',
    '--bg-veil': 'rgba(11, 17, 32, 0.96)',
    '--panel-veil': 'rgba(14, 22, 40, 0.55)',
    '--sidebar-bg': 'linear-gradient(180deg, #0f1729 0%, #0c1322 100%)',
    '--table-head': '#162033',
    // 分隔线
    '--line': 'rgba(38, 49, 72, 0.6)',
    '--grid-line': 'rgba(38, 49, 72, 0.35)',
    '--lineage-bg': 'rgba(11, 17, 32, 0.6)',
    '--overlay': 'rgba(4, 8, 18, 0.62)',
    // 阴影
    '--shadow': '0 8px 24px rgba(2, 6, 20, 0.45)',
    '--shadow-sm': '0 2px 8px rgba(2, 6, 20, 0.35)',
    // 控件细节
    '--btn-hover-bg': '#26334d',
    '--btn-hover-border': '#40506f',
    '--placeholder': '#5c6a82',
    '--scroll-thumb': '#2b3752',
    '--scroll-thumb-hover': '#3a4a6b',
    '--skeleton-a': '#16203a',
    '--skeleton-b': '#1d2842',
    '--progress-track': '#22304a',
    '--spinner-track': 'rgba(148, 163, 184, 0.25)',
    '--swatch-line': 'rgba(255, 255, 255, 0.22)',
    // 彩色底上的深色文字（等级徽标、AQI 药丸）：底色是中间调，两套模式通用
    '--grade-ink': '#06131f',
    '--aqi-ink': '#08131f',
    // 语义色：soft 作底、ink 作文、line 作边
    '--ok-soft': 'rgba(34, 197, 94, 0.14)',
    '--ok-ink': '#86efac',
    '--ok-line': 'rgba(34, 197, 94, 0.4)',
    '--warn-soft': 'rgba(245, 158, 11, 0.14)',
    '--warn-ink': '#fcd34d',
    '--warn-line': 'rgba(245, 158, 11, 0.4)',
    '--danger-soft': 'rgba(239, 68, 68, 0.12)',
    '--danger-ink': '#fca5a5',
    '--danger-line': 'rgba(239, 68, 68, 0.4)',
    '--info-soft': 'rgba(56, 189, 248, 0.12)',
    '--info-ink': '#a5e8ff',
    '--info-line': 'rgba(56, 189, 248, 0.4)',
    '--purple-soft': 'rgba(167, 139, 250, 0.16)',
    '--purple-ink': '#ddd0ff',
    '--purple-line': 'rgba(167, 139, 250, 0.4)',
    '--neutral-soft': 'rgba(139, 156, 182, 0.16)',
    '--neutral-ink': '#cbd5e1',
    '--neutral-line': 'rgba(139, 156, 182, 0.4)',
    '--mute-soft': 'rgba(123, 136, 158, 0.14)',
    '--mute-line': 'rgba(123, 136, 158, 0.3)',
    // 图表专用（CSS 不消费，仅 ECharts 读取）
    '--chart-tooltip-bg': 'rgba(15, 22, 38, 0.96)',
    '--chart-axis-pointer': '#1a2337',
    '--chart-split-line': 'rgba(38, 49, 72, 0.92)',
    '--chart-radar-a': 'rgba(21, 29, 46, 0.35)',
    '--chart-radar-b': 'rgba(11, 17, 32, 0.35)',
    '--chart-zoom-bg': 'rgba(21, 29, 46, 0.7)',
    '--chart-donut-border': '#101828',
    '--chart-cell-border': 'rgba(11, 17, 32, 0.6)',
  },

  light: {
    // 背景与表面
    '--bg': '#f5f7fb',
    '--bg-soft': '#eef1f7',
    '--surface': '#ffffff',
    '--surface-2': '#fbfcfe',
    '--surface-3': '#f1f4fa',
    '--surface-hover': '#eef2f9',
    '--border': '#dce2ec',
    '--border-strong': '#c2cad9',
    // 文本
    '--text': '#1a2333',
    '--text-dim': '#47536b',
    '--text-mute': '#6b7688',
    // 半透明表面
    '--glass': 'rgba(255, 255, 255, 0.86)',
    '--glass-soft': 'rgba(255, 255, 255, 0.72)',
    '--bg-veil': 'rgba(245, 247, 251, 0.94)',
    '--panel-veil': 'rgba(255, 255, 255, 0.6)',
    '--sidebar-bg': 'linear-gradient(180deg, #ffffff 0%, #f7f9fc 100%)',
    '--table-head': '#eef1f7',
    // 分隔线
    '--line': '#e6ebf3',
    '--grid-line': '#eef1f7',
    '--lineage-bg': '#f8fafd',
    '--overlay': 'rgba(23, 37, 68, 0.32)',
    // 阴影
    '--shadow': '0 10px 28px rgba(23, 37, 68, 0.12)',
    '--shadow-sm': '0 1px 3px rgba(23, 37, 68, 0.08)',
    // 控件细节
    '--btn-hover-bg': '#eef1f7',
    '--btn-hover-border': '#c2cad9',
    '--placeholder': '#98a3b5',
    '--scroll-thumb': '#c8d0de',
    '--scroll-thumb-hover': '#aab5c8',
    '--skeleton-a': '#eef1f7',
    '--skeleton-b': '#e2e7f0',
    '--progress-track': '#e2e7f0',
    '--spinner-track': '#d5dce8',
    '--swatch-line': 'rgba(0, 0, 0, 0.16)',
    '--grade-ink': '#06131f',
    '--aqi-ink': '#08131f',
    // 语义色：浅色模式下 ink 必须加深，否则白底上不可读
    '--ok-soft': 'rgba(21, 128, 61, 0.10)',
    '--ok-ink': '#15803d',
    '--ok-line': 'rgba(21, 128, 61, 0.35)',
    '--warn-soft': 'rgba(180, 83, 9, 0.10)',
    '--warn-ink': '#b45309',
    '--warn-line': 'rgba(180, 83, 9, 0.35)',
    '--danger-soft': 'rgba(185, 28, 28, 0.09)',
    '--danger-ink': '#b91c1c',
    '--danger-line': 'rgba(185, 28, 28, 0.35)',
    '--info-soft': 'rgba(3, 105, 161, 0.09)',
    '--info-ink': '#0369a1',
    '--info-line': 'rgba(3, 105, 161, 0.35)',
    '--purple-soft': 'rgba(109, 40, 217, 0.10)',
    '--purple-ink': '#6d28d9',
    '--purple-line': 'rgba(109, 40, 217, 0.32)',
    '--neutral-soft': 'rgba(100, 116, 139, 0.12)',
    '--neutral-ink': '#475569',
    '--neutral-line': 'rgba(100, 116, 139, 0.3)',
    '--mute-soft': 'rgba(100, 116, 139, 0.10)',
    '--mute-line': 'rgba(100, 116, 139, 0.25)',
    // 图表专用
    '--chart-tooltip-bg': 'rgba(255, 255, 255, 0.97)',
    '--chart-axis-pointer': '#e8edf5',
    '--chart-split-line': '#e2e7f0',
    '--chart-radar-a': 'rgba(238, 242, 249, 0.9)',
    '--chart-radar-b': 'rgba(255, 255, 255, 0.9)',
    '--chart-zoom-bg': 'rgba(226, 232, 242, 0.9)',
    '--chart-donut-border': '#ffffff',
    '--chart-cell-border': 'rgba(255, 255, 255, 0.85)',
  },
};

export const DEFAULT_MODE = 'dark';

// ==================================================================
// 强调色
// ==================================================================

/** 可选强调色。``accent`` 之外的取值都由它推导，见 ``tokensFor``。 */
export const THEMES = [
  { id: 'blue', label: '科技蓝', accent: '#3b82f6' },
  { id: 'cyan', label: '湖青', accent: '#22d3ee' },
  { id: 'violet', label: '紫罗兰', accent: '#a78bfa' },
  { id: 'green', label: '翠绿', accent: '#22c55e' },
  { id: 'amber', label: '琥珀', accent: '#f59e0b' },
  { id: 'rose', label: '玫红', accent: '#f472b6' },
];

export const DEFAULT_THEME_ID = 'blue';

/**
 * 分类色底表（与 style.css 的 ``--series-*`` 默认值一致）。
 * 实际序列配色 = 当前强调色排第一 + 这张表里去掉同色后的其余颜色。
 */
const BASE_PALETTE = [
  '#3b82f6',
  '#22d3ee',
  '#a78bfa',
  '#22c55e',
  '#f59e0b',
  '#f472b6',
  '#38bdf8',
  '#fb923c',
  '#4ade80',
  '#e879f9',
];

// ==================================================================
// 颜色推导
// ==================================================================

function clampChannel(value) {
  return Math.max(0, Math.min(255, Math.round(value)));
}

/** ``#abc`` / ``#aabbcc`` → ``{r,g,b}``；无法解析时返回 null。 */
export function hexToRgb(hex) {
  const text = String(hex ?? '').trim().replace(/^#/, '');
  const full = text.length === 3 ? text.split('').map((char) => char + char).join('') : text;
  if (!/^[0-9a-fA-F]{6}$/.test(full)) return null;
  return {
    r: parseInt(full.slice(0, 2), 16),
    g: parseInt(full.slice(2, 4), 16),
    b: parseInt(full.slice(4, 6), 16),
  };
}

/** 按比例加深（``0.88`` ≈ 暗 12%），用于悬停态。 */
export function shade(hex, factor) {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  const part = (value) => clampChannel(value * factor).toString(16).padStart(2, '0');
  return `#${part(rgb.r)}${part(rgb.g)}${part(rgb.b)}`;
}

/** 同色加透明度，用于选中底纹与柔和高亮。 */
export function withAlpha(hex, alpha) {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})`;
}

/** 向白色混合，得到同色的浅色调（用于柔和底纹上的文字）。 */
export function tint(hex, amount) {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  const part = (value) => clampChannel(value + (255 - value) * amount).toString(16).padStart(2, '0');
  return `#${part(rgb.r)}${part(rgb.g)}${part(rgb.b)}`;
}

// ==================================================================
// 主题查询
// ==================================================================

/** 按 id 取强调色；未命中时回落到默认（不抛错，避免存储脏值导致白屏）。 */
export function themeById(id) {
  return (
    THEMES.find((item) => item.id === id) ||
    THEMES.find((item) => item.id === DEFAULT_THEME_ID) ||
    THEMES[0]
  );
}

/** 按 id 取状态主题名；未命中时回落到默认。 */
export function modeById(id) {
  return Object.prototype.hasOwnProperty.call(MODES, id) ? id : DEFAULT_MODE;
}

/** 某强调色的图表序列配色：强调色打头，其余为分类色，且保证不重复。 */
export function seriesPaletteFor(theme) {
  const accent = theme?.accent || THEMES[0].accent;
  const rest = BASE_PALETTE.filter((color) => color.toLowerCase() !== accent.toLowerCase());
  return [accent, ...rest].slice(0, BASE_PALETTE.length);
}

/**
 * 某套主题对应的全部 CSS 变量 = 状态色 + 强调色 + 序列配色。
 * 形状是扁平的 ``变量名 → 取值``，便于测试逐项比对 ``:root``。
 */
export function tokensFor(theme, mode = DEFAULT_MODE) {
  const resolved = modeById(mode);
  const tokens = { ...MODES[resolved] };
  tokens['--accent'] = theme.accent;
  tokens['--accent-hover'] = shade(theme.accent, 0.88);
  tokens['--accent-soft'] = withAlpha(theme.accent, 0.14);
  tokens['--accent-faint'] = withAlpha(theme.accent, 0.06);
  // "柔和底纹上的文字"必须朝背景的反方向走：深色底用浅色调，白底用深色调。
  // 若两种状态都用浅色调，浅色模式下就会变成浅底浅字，直接不可读。
  tokens['--accent-ink'] = resolved === 'light' ? shade(theme.accent, 0.72) : tint(theme.accent, 0.55);
  seriesPaletteFor(theme).forEach((color, index) => {
    tokens[`--series-${index + 1}`] = color;
  });
  return tokens;
}

// ==================================================================
// 当前主题
// ==================================================================

let active = themeById(DEFAULT_THEME_ID);
let activeMode = DEFAULT_MODE;

/** 当前强调色主题对象。 */
export function activeTheme() {
  return active;
}

/** 当前状态主题：``'dark'`` 或 ``'light'``。 */
export function activeModeId() {
  return activeMode;
}

/** 当前强调色。 */
export function accent() {
  return active.accent;
}

/** 当前强调色的柔和高亮色（选中底纹）。 */
export function accentSoft() {
  return withAlpha(active.accent, 0.14);
}

/** 当前强调色的图表序列配色。 */
export function seriesPalette() {
  return seriesPaletteFor(active);
}

/**
 * 图表用的中性色（轴、网格、图例、提示框等）。
 *
 * 这些值必须随状态主题变化：为深色底挑的轴标签放到白底上就是一团糊。
 * 返回对象而不是模块级常量，正是为了避免"加载时取一次、切主题后不跟着变"。
 * 图表一次渲染会取色几十次，因此按状态缓存一份，切换状态时自动失效。
 */
let inkCache = null;
let inkCacheMode = null;

export function ink() {
  if (!inkCache || inkCacheMode !== activeMode) {
    const tokens = MODES[activeMode];
    inkCache = {
      text: tokens['--text'],
      dim: tokens['--text-dim'],
      mute: tokens['--text-mute'],
      grid: tokens['--border'],
      border: tokens['--border-strong'],
      tooltipBg: tokens['--chart-tooltip-bg'],
      axisPointer: tokens['--chart-axis-pointer'],
      splitLine: tokens['--chart-split-line'],
      radarA: tokens['--chart-radar-a'],
      radarB: tokens['--chart-radar-b'],
      zoomBg: tokens['--chart-zoom-bg'],
      donutBorder: tokens['--chart-donut-border'],
      cellBorder: tokens['--chart-cell-border'],
      warnInk: tokens['--warn-ink'],
    };
    inkCacheMode = activeMode;
  }
  return inkCache;
}

// ==================================================================
// 应用与持久化
// ==================================================================

function storageGet(key) {
  try {
    if (typeof window === 'undefined') return null;
    return window.localStorage?.getItem(key) ?? null;
  } catch {
    // 隐私模式等场景下访问 localStorage 会抛错：退化为"不记忆"
    return null;
  }
}

function storageSet(key, value) {
  try {
    if (typeof window === 'undefined') return;
    window.localStorage?.setItem(key, String(value));
  } catch {
    /* 忽略写入失败 */
  }
}

/** 把当前主题的令牌写到 ``:root``（无 DOM 时只更新内存状态）。 */
function flush() {
  if (typeof document === 'undefined' || !document.documentElement) return;
  const root = document.documentElement;
  for (const [name, value] of Object.entries(tokensFor(active, activeMode))) {
    root.style.setProperty(name, value);
  }
}

/**
 * 应用强调色：更新内存状态与 CSS 变量，并按需记住选择。
 * @returns 应用后的强调色主题对象
 */
export function applyTheme(id, { persist = true } = {}) {
  active = themeById(id);
  flush();
  if (persist) storageSet(THEME_KEY, active.id);
  return active;
}

/**
 * 应用状态主题（深 / 浅）。
 *
 * 除了写 CSS 变量，还设置 ``<html data-mode>`` 与 ``color-scheme``：
 * 前者供样式表里少量需要按状态区分的规则使用，后者让浏览器原生控件
 * （滚动条、下拉箭头、表单）跟随深浅，不然浅色界面里会留一条深色滚动条。
 *
 * @returns 应用后的状态名
 */
export function applyMode(mode, { persist = true } = {}) {
  activeMode = modeById(mode);
  flush();
  if (typeof document !== 'undefined' && document.documentElement) {
    document.documentElement.dataset.mode = activeMode;
    document.documentElement.style.colorScheme = activeMode;
  }
  if (persist) storageSet(MODE_KEY, activeMode);
  return activeMode;
}

/** 启动时读取记住的状态与强调色并应用（不写回，避免污染存储）。 */
export function initTheme() {
  applyMode(storageGet(MODE_KEY) || DEFAULT_MODE, { persist: false });
  return applyTheme(storageGet(THEME_KEY) || DEFAULT_THEME_ID, { persist: false });
}
