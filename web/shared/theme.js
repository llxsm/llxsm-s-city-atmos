/**
 * theme.js —— 主题色与图表配色。
 *
 * 设计要点
 * --------
 * 1. **只声明一个主色**：每个主题只给一个 ``accent``，悬停色、选中底纹和图表序列
 *    配色全部由它推导。否则每加一个主题都要手填十几个近似色值，既容易填错，
 *    也很难保证同一主题内部色调一致。
 *
 * 2. **CSS 变量是唯一的落地方式**：应用主题时把推导结果写到
 *    ``document.documentElement`` 上（``--accent`` / ``--accent-hover`` /
 *    ``--accent-soft`` / ``--series-1..N``），样式表与图表读同一份值。
 *    ``style.css`` 的 ``:root`` 保留一套与默认主题相同的取值，这样脚本执行前
 *    首屏不会闪一下别的颜色。
 *
 * 3. **内存副本供图表取色**：图表渲染时频繁取色，读 DOM 既慢又依赖运行环境。
 *    这里把当前主题保存在模块状态里，``accent()`` / ``seriesPalette()`` 纯读内存，
 *    因此在没有 DOM 的 Node 环境里也能正常求值（测试会用到）。
 *
 * 4. **语义色不受主题影响**：评级 A–E、空气质量分级、达标/告警、血缘分层这些颜色
 *    承载含义，必须在任何主题下都保持稳定，因此它们仍在各自的视图里硬编码 ——
 *    这是有意为之，不是遗漏。
 *
 * 选择结果存在 ``localStorage['atmos.theme']``；主题是全局偏好，两个门户共用。
 */

const STORAGE_KEY = 'atmos.theme';

/** 可选主题。``accent`` 之外的取值都由它推导，见 ``tokensFor``。 */
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
 * 实际序列配色 = 当前主题主色排第一 + 这张表里去掉同色后的其余颜色。
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
  const part = (value) => clampChannel(value).toString(16).padStart(2, '0');
  return `#${part(rgb.r * factor)}${part(rgb.g * factor)}${part(rgb.b * factor)}`;
}

/** 同色加透明度，用于选中底纹与柔和高亮。 */
export function withAlpha(hex, alpha) {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  return `rgba(${rgb.r}, ${rgb.g}, ${rgb.b}, ${alpha})`;
}

/** 向白色混合，得到同色的浅色调（用于柔和底纹上的文字，保证对比度）。 */
export function tint(hex, amount) {
  const rgb = hexToRgb(hex);
  if (!rgb) return hex;
  const part = (value) => clampChannel(value + (255 - value) * amount).toString(16).padStart(2, '0');
  return `#${part(rgb.r)}${part(rgb.g)}${part(rgb.b)}`;
}

// ==================================================================
// 主题查询
// ==================================================================

/** 按 id 取主题；未命中时回落到默认主题（不抛错，避免存储脏值导致白屏）。 */
export function themeById(id) {
  return (
    THEMES.find((item) => item.id === id) ||
    THEMES.find((item) => item.id === DEFAULT_THEME_ID) ||
    THEMES[0]
  );
}

/** 某主题的图表序列配色：主色打头，其余为分类色，且保证不重复。 */
export function seriesPaletteFor(theme) {
  const accent = theme?.accent || THEMES[0].accent;
  const rest = BASE_PALETTE.filter((color) => color.toLowerCase() !== accent.toLowerCase());
  return [accent, ...rest].slice(0, BASE_PALETTE.length);
}

/** 某主题对应的全部 CSS 变量。 */
export function tokensFor(theme) {
  const tokens = {
    '--accent': theme.accent,
    '--accent-hover': shade(theme.accent, 0.88),
    '--accent-soft': withAlpha(theme.accent, 0.14),
    '--accent-faint': withAlpha(theme.accent, 0.06),
    '--accent-ink': tint(theme.accent, 0.55),
  };
  seriesPaletteFor(theme).forEach((color, index) => {
    tokens[`--series-${index + 1}`] = color;
  });
  return tokens;
}

// ==================================================================
// 当前主题
// ==================================================================

let active = themeById(DEFAULT_THEME_ID);

/** 当前主题对象。 */
export function activeTheme() {
  return active;
}

/** 当前主题主色。 */
export function accent() {
  return active.accent;
}

/** 当前主题的柔和高亮色（选中底纹）。 */
export function accentSoft() {
  return withAlpha(active.accent, 0.14);
}

/** 当前主题的图表序列配色。 */
export function seriesPalette() {
  return seriesPaletteFor(active);
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

/**
 * 应用主题：更新内存状态与 ``:root`` 上的 CSS 变量，并按需记住选择。
 * @returns 应用后的主题对象
 */
export function applyTheme(id, { persist = true } = {}) {
  active = themeById(id);

  if (typeof document !== 'undefined' && document.documentElement) {
    for (const [name, value] of Object.entries(tokensFor(active))) {
      document.documentElement.style.setProperty(name, value);
    }
  }
  if (persist) storageSet(STORAGE_KEY, active.id);
  return active;
}

/** 启动时读取记住的主题并应用；无有效存储时用默认主题（不写回，避免污染）。 */
export function initTheme() {
  return applyTheme(storageGet(STORAGE_KEY) || DEFAULT_THEME_ID, { persist: false });
}
