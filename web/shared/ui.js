/**
 * ui.js —— 视图层共享工具。
 *
 * 职责：
 * 1. 轻量 DOM 构造器（无框架，避免拼接字符串带来的转义风险）；
 * 2. 数值/时间/体积等展示格式化；
 * 3. 通用组件：卡片、KPI、表格、空状态、图表容器、抽屉、提示条。
 *
 * 约定：本文件不发起任何网络请求，保持为纯展示层。
 */

// ==================================================================
// DOM 构造
// ==================================================================

/**
 * 创建元素。
 * @param {string} tag 形如 `div`、`span.cls`、`div.a.b#id`
 * @param {object|null} attrs 属性；`text` 写入文本，`html` 写入 HTML，
 *        `class`/`style`（对象）/`dataset`/其余为普通属性或事件（onXxx）
 * @param {Array|Node|string} children 子节点
 */
export function el(tag, attrs = null, children = null) {
  const [name, ...classes] = String(tag).split('.');
  const [tagName, id] = name.split('#');
  const node = document.createElement(tagName || 'div');
  if (id) node.id = id;
  if (classes.length) node.className = classes.join(' ');

  if (attrs && typeof attrs === 'object') {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') {
        node.className = [node.className, value].filter(Boolean).join(' ');
      } else if (key === 'text') {
        node.textContent = String(value);
      } else if (key === 'html') {
        node.innerHTML = String(value);
      } else if (key === 'style' && typeof value === 'object') {
        Object.assign(node.style, value);
      } else if (key === 'dataset' && typeof value === 'object') {
        Object.assign(node.dataset, value);
      } else if (key.startsWith('on') && typeof value === 'function') {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value === true) {
        node.setAttribute(key, '');
      } else {
        node.setAttribute(key, String(value));
      }
    }
  }

  appendChildren(node, children);
  return node;
}

function appendChildren(node, children) {
  if (children === null || children === undefined) return;
  const list = Array.isArray(children) ? children : [children];
  for (const child of list) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(child instanceof Node ? child : document.createTextNode(String(child)));
  }
}

/** 清空容器并可选追加新内容。 */
export function mount(container, ...children) {
  container.textContent = '';
  appendChildren(container, children);
  return container;
}

/** 便捷选择器。 */
export function qs(selector, root = document) {
  return root.querySelector(selector);
}

/** HTML 转义（仅用于必须拼接 HTML 的场景）。 */
export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ==================================================================
// 数值与文本格式化
// ==================================================================

/** 空值统一展示为破折号。 */
export function dash(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'number' && Number.isNaN(value)) return '—';
  return String(value);
}

export function isNumber(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

/** 千分位整数。 */
export function formatInt(value) {
  if (!isNumber(value)) return '—';
  return Math.round(value).toLocaleString('zh-CN');
}

/** 固定小数位。 */
export function formatFixed(value, digits = 1) {
  if (!isNumber(value)) return '—';
  return value.toFixed(digits);
}

/** 大数缩写：1.2万 / 3.4亿。 */
export function formatCompact(value) {
  if (!isNumber(value)) return '—';
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e4) return `${(value / 1e4).toFixed(2)}万`;
  return Math.round(value).toLocaleString('zh-CN');
}

/** 字节体积。 */
export function formatBytes(value) {
  if (!isNumber(value) || value < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let size = value;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024;
    index += 1;
  }
  return `${size.toFixed(index === 0 ? 0 : 1)} ${units[index]}`;
}

/** 毫秒耗时。 */
export function formatDuration(ms) {
  if (!isNumber(ms)) return '—';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)} s`;
  return `${(ms / 60000).toFixed(1)} min`;
}

/** 数值 + 单位。 */
export function formatWithUnit(value, unit, digits = 1) {
  const text = isNumber(value) ? (Math.abs(value) >= 1000 ? formatCompact(value) : value.toFixed(digits)) : '—';
  if (text === '—') return text;
  return unit ? `${text} ${unit}` : text;
}

/** 百分比数值（后端多返回 0-100 的数值）。 */
export function formatPercent(value, digits = 1) {
  if (!isNumber(value)) return '—';
  return `${value.toFixed(digits)}%`;
}

// ==================================================================
// 时间格式化
//
// 平台存在两类时间：
//   * 审计时间 —— ISO-8601 UTC，以 `Z` 结尾，展示时转换为浏览器本地时区；
//   * 数据时间 —— 城市本地时间的裸字符串（无时区），只做格式化，绝不转换时区。
// ==================================================================

const pad = (value) => String(value).padStart(2, '0');

/** 审计时间（UTC，带 Z）→ 本地时区展示。 */
export function formatAuditTime(value, withDate = true) {
  if (!value) return '—';
  const date = value instanceof Date ? value : new Date(String(value));
  if (Number.isNaN(date.getTime())) return String(value);
  const time = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  if (!withDate) return time;
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${time}`;
}

/** 在审计时间后追加"（本地时间）"标注，避免读者误判时区。 */
export function formatAuditTimeLocal(value) {
  if (!value) return '—';
  return `${formatAuditTime(value)} 本地`;
}

/**
 * 数据时间（城市本地裸时间）→ 直接展示，不做时区换算。
 * 兼容 `2024-05-01T08:00:00`、`2024-05-01 08:00:00`、`2024-05-01`。
 */
export function formatDataTime(value, withDate = true) {
  if (value === null || value === undefined || value === '') return '—';
  const raw = String(value);
  if (raw.includes('T') || raw.includes(' ')) {
    const [day, rest] = raw.split(/[T ]/);
    const hhmm = rest.slice(0, 5);
    return withDate ? `${day} ${hhmm}` : hhmm;
  }
  return raw;
}

/** 图表横轴标签：`MM-DD HH:00`。 */
export function shortDataTime(value) {
  const text = formatDataTime(value, true);
  if (text === '—') return '—';
  const [day, time] = text.split(' ');
  const parts = (day || '').split('-');
  const shortDay = parts.length >= 3 ? `${parts[1]}-${parts[2]}` : day;
  return time ? `${shortDay} ${time.slice(0, 5)}` : shortDay;
}

/** 日期（YYYY-MM-DD）。 */
export function formatDate(value) {
  if (!value) return '—';
  return String(value).split(/[T ]/)[0];
}

// ==================================================================
// 枚举 / 等级 / 状态映射
// ==================================================================

export const RUN_STATUS = {
  pending: { label: '等待中', cls: 'badge-mute' },
  running: { label: '运行中', cls: 'badge-info' },
  success: { label: '成功', cls: 'badge-ok' },
  partial: { label: '部分成功', cls: 'badge-warn' },
  failed: { label: '失败', cls: 'badge-bad' },
  skipped: { label: '已跳过', cls: 'badge-mute' },
};

export const CHECK_STATUS = {
  pass: { label: '通过', cls: 'badge-ok' },
  warn: { label: '预警', cls: 'badge-warn' },
  fail: { label: '不通过', cls: 'badge-bad' },
  error: { label: '执行异常', cls: 'badge-bad' },
  skipped: { label: '已跳过', cls: 'badge-mute' },
};

export const SEVERITY = {
  critical: { label: '严重', cls: 'badge-bad' },
  major: { label: '重要', cls: 'badge-warn' },
  minor: { label: '次要', cls: 'badge-info' },
};

export const ALERT_SEVERITY = {
  severe: { label: '严重', cls: 'badge-bad' },
  warning: { label: '警告', cls: 'badge-warn' },
  info: { label: '提示', cls: 'badge-info' },
};

export const LAYER_LABEL = {
  reference: '参考层',
  raw: '原始层',
  refined: '精炼层',
  serving: '服务层',
};

export const LAYER_COLOR = {
  reference: '#8b9cb6',
  raw: '#38bdf8',
  refined: '#a78bfa',
  serving: '#22c55e',
};

export const DOMAIN_LABEL = {
  weather: '气象',
  air_quality: '空气质量',
  integrated: '环境融合',
  reference: '参考数据',
};

export const DIMENSION_LABEL = {
  completeness: '完整性',
  timeliness: '时效性',
  validity: '有效性',
  consistency: '一致性',
  uniqueness: '唯一性',
};

export const DIMENSION_KEYS = ['completeness', 'timeliness', 'validity', 'consistency', 'uniqueness'];

export const GRADE_ORDER = ['A', 'B', 'C', 'D', 'E'];

export const GRADE_COLOR = {
  A: '#22c55e',
  B: '#38bdf8',
  C: '#f59e0b',
  D: '#f97316',
  E: '#ef4444',
};

export const GRADE_LABEL = { A: '优秀', B: '良好', C: '关注', D: '较差', E: '待治理' };

export function gradeColor(grade) {
  return GRADE_COLOR[grade] || '#64748b';
}

/** 质量等级徽标。 */
export function gradeBadge(grade, extraClass = '') {
  if (!grade || !GRADE_COLOR[grade]) {
    return el('span', { class: `grade grade-none ${extraClass}`.trim(), text: '—', title: '尚未评测' });
  }
  return el('span', {
    class: `grade grade-${grade} ${extraClass}`.trim(),
    text: grade,
    title: `${grade} · ${GRADE_LABEL[grade] || ''}`,
  });
}

/** 通用徽标。 */
export function badge(text, cls = 'badge-mute', title = null) {
  return el('span', { class: `badge ${cls}`, text: dash(text), title: title || null });
}

export function runStatusBadge(status, labelOverride = null) {
  const meta = RUN_STATUS[status] || { label: status || '未知', cls: 'badge-mute' };
  return badge(labelOverride || meta.label, meta.cls);
}

export function checkStatusBadge(status, labelOverride = null) {
  const meta = CHECK_STATUS[status] || { label: status || '未知', cls: 'badge-mute' };
  return badge(labelOverride || meta.label, meta.cls);
}

export function layerBadge(layer, labelOverride = null) {
  if (!layer) return badge('—', 'badge-mute');
  return badge(labelOverride || LAYER_LABEL[layer] || layer, `badge-layer-${layer}`);
}

/** 等级色圆点+文字（用于图例）。 */
export function legendItem(color, label) {
  return el('span.legend-item', null, [
    el('span.legend-swatch', { style: { background: color } }),
    el('span', { text: label }),
  ]);
}

// ==================================================================
// 通用组件
// ==================================================================

export function card(title, body, options = {}) {
  const head = title
    ? el('div.card-head', null, [
        el('div', null, [
          el('h3.card-title', { text: title }),
          options.subtitle ? el('div.card-sub', { text: options.subtitle }) : null,
        ]),
        options.actions ? el('div.card-actions', null, options.actions) : null,
      ])
    : null;
  return el(`section.card${options.class ? `.${options.class}` : ''}`, { id: options.id || null }, [
    head,
    body,
  ]);
}

export function kpiCard({ label, value, unit, foot, tone = '', hint = '' }) {
  const toneClass = tone ? ` value-${tone}` : '';
  return el('div.kpi', { title: hint || null }, [
    el('div.kpi-label', null, [el('span', { text: label })]),
    el('div.kpi-value', null, [
      el('span', { class: toneClass.trim(), text: dash(value) }),
      unit ? el('span.unit', { text: unit }) : null,
    ]),
    el('div.kpi-foot', { text: foot || '' }),
  ]);
}

export function field(label, control) {
  return el('div.field', null, [el('label', { text: label }), control]);
}

export function select(options, { value = '', onChange, id = null, disabled = false } = {}) {
  const node = el('select.select', { id, disabled: disabled || null });
  for (const option of options) {
    node.appendChild(
      el('option', {
        value: option.value,
        text: option.label,
        selected: String(option.value) === String(value) || null,
      }),
    );
  }
  if (onChange) node.addEventListener('change', () => onChange(node.value, node));
  return node;
}

export function input(attrs = {}, onInput = null) {
  const node = el('input.input', { type: 'text', ...attrs });
  if (onInput) node.addEventListener('input', () => onInput(node.value, node));
  return node;
}

export function checkbox({ label, checked = false, onChange = null, title = '' }) {
  const box = el('input', { type: 'checkbox', checked: checked || null, title: title || null });
  if (onChange) box.addEventListener('change', () => onChange(box.checked, box));
  return el('label.checkbox', { title: title || null }, [box, el('span', { text: label })]);
}

export function button(label, { onClick, kind = 'default', size = '', disabled = false, title = '' } = {}) {
  const kindClass = kind === 'primary' ? ' btn-primary' : kind === 'ghost' ? ' btn-ghost' : kind === 'danger' ? ' btn-danger' : '';
  const sizeClass = size === 'sm' ? ' btn-sm' : '';
  const node = el(`button.btn${kindClass}${sizeClass}`, {
    type: 'button',
    disabled: disabled || null,
    title: title || null,
    text: label,
  });
  if (onClick) node.addEventListener('click', onClick);
  return node;
}

/** 可点击的筛选 chip。 */
export function chip(label, count, active, onClick) {
  return el('button.chip', {
    type: 'button',
    class: active ? 'active' : '',
    onClick,
  }, [el('span', { text: label }), count === null || count === undefined ? null : el('span.chip-count', { text: count })]);
}

export function chipRow(label, chips, { onClear = null, showClear = false } = {}) {
  return el('div.chip-row', null, [
    label ? el('span.chip-label', { text: label }) : null,
    ...chips,
    showClear && onClear ? el('button.btn-link', { type: 'button', text: '清除', onClick: onClear }) : null,
  ]);
}

/**
 * 通用表格。
 * columns: [{ key, title, align, width, render(row), sortable, value(row) }]
 */
export function table(columns, rows, options = {}) {
  const { onRowClick = null, rowKey = null, empty = '暂无记录', sortKey = null, sortDir = 'desc', onSort = null } = options;

  const headCells = columns.map((column) => {
    const isSorted = sortKey && column.key === sortKey;
    const th = el('th', {
      class: [column.align === 'right' ? 'num' : '', column.sortable ? 'sortable' : ''].filter(Boolean).join(' '),
      style: column.width ? { width: column.width } : null,
      title: column.title || '',
    }, [
      el('span', { text: column.title }),
      isSorted ? el('span.sort-mark', { text: sortDir === 'asc' ? '▲' : '▼' }) : null,
    ]);
    if (column.sortable && onSort) th.addEventListener('click', () => onSort(column.key));
    return th;
  });

  const tbody = el('tbody');
  if (!rows || rows.length === 0) {
    tbody.appendChild(
      el('tr', null, [el('td', { colspan: String(columns.length), class: 'text-mute', text: empty, style: { textAlign: 'center', padding: '18px' } })]),
    );
  } else {
    rows.forEach((row, index) => {
      const tr = el('tr', {
        class: onRowClick ? 'selectable' : '',
        onClick: onRowClick ? () => onRowClick(row, index) : null,
      });
      for (const column of columns) {
        const cellValue = column.render ? column.render(row) : dash(row[column.key]);
        const td = el('td', { class: column.align === 'right' ? 'num' : column.wrap ? 'wrap' : '' });
        if (cellValue instanceof Node) td.appendChild(cellValue);
        else td.textContent = cellValue === null || cellValue === undefined ? '—' : String(cellValue);
        tr.appendChild(td);
      }
      if (rowKey) tr.dataset.key = String(rowKey(row));
      tbody.appendChild(tr);
    });
  }

  return el('div.table-wrap', null, [
    el('table.table', null, [el('thead', null, el('tr', null, headCells)), tbody]),
  ]);
}

/** 键值对列表。 */
export function kvList(entries) {
  const dl = el('dl.kv');
  for (const [key, value] of entries) {
    if (value === null || value === undefined || value === '') continue;
    dl.appendChild(el('dt', { text: key }));
    dl.appendChild(el('dd', null, value instanceof Node ? value : document.createTextNode(String(value))));
  }
  return dl;
}

export function sectionTitle(text) {
  return el('div.section-title', { text });
}

/** 空状态。actions 为按钮数组。 */
export function emptyState({ title = '暂无数据', hint = '', actions = [], compact = false } = {}) {
  return el('div.empty', { style: compact ? { minHeight: '90px', padding: '18px 14px' } : null }, [
    el('div.empty-title', { text: title }),
    hint ? el('div.empty-hint', { html: hint }) : null,
    actions.length ? el('div.empty-actions', null, actions) : null,
  ]);
}

/** 无数据时统一引导用户触发采集。 */
export function collectHint(extra = '') {
  const base = '当前平台尚无可展示的数据。请先执行采集管道（POST <code>/api/ops/collect</code>），或前往「数据刷新」页点击「触发采集」。';
  return extra ? `${base}<br />${extra}` : base;
}

export function loadingBlock(text = '正在加载…') {
  return el('div.loading', null, [el('div.spinner'), el('div', { text, class: 'text-mute' })]);
}

export function skeleton(height = 90) {
  return el('div.skeleton', { style: { minHeight: `${height}px` } });
}

/** 进度条；percent 为 null 时显示不确定态。 */
export function progressBar(percent) {
  const indeterminate = percent === null || percent === undefined;
  return el('div.progress', { class: indeterminate ? 'progress-indeterminate' : '' }, [
    el('span', { style: { width: indeterminate ? '40%' : `${Math.max(0, Math.min(100, percent))}%` } }),
  ]);
}

/** 图表容器：交给 charts.js 初始化。 */
export function chartBox(height = 300, { id = null } = {}) {
  return el('div.chart', { id, style: { height: `${height}px` } });
}

export function note(text) {
  if (!text) return null;
  return el('div.chart-note', { text });
}

// ==================================================================
// 顶部提示条
// ==================================================================

/**
 * 在 #bannerStack 中插入一条提示。
 * @returns {function} 关闭函数
 */
export function banner({ title = '', message = '', kind = 'info', timeout = 0, dismissible = true } = {}) {
  const stack = qs('#bannerStack');
  if (!stack) return () => {};
  const node = el(`div.banner.banner-${kind}`, null, [
    el('div.banner-body', null, [
      title ? el('div.banner-title', { text: title }) : null,
      message ? el('div', { html: message }) : null,
    ]),
    dismissible ? el('button.banner-close', { type: 'button', text: '✕', onClick: () => close() }) : null,
  ]);
  stack.appendChild(node);
  let timer = null;
  function close() {
    if (timer) clearTimeout(timer);
    node.remove();
  }
  if (timeout > 0) timer = setTimeout(close, timeout);
  return close;
}

export function clearBanners() {
  const stack = qs('#bannerStack');
  if (stack) stack.textContent = '';
}

// ==================================================================
// 轻提示
// ==================================================================

export function toast(message, kind = 'info', timeout = 3200) {
  const root = qs('#toastRoot');
  if (!root) return () => {};
  const node = el(`div.toast.toast-${kind}`, { text: message });
  root.appendChild(node);
  const timer = setTimeout(() => node.remove(), timeout);
  return () => {
    clearTimeout(timer);
    node.remove();
  };
}

// ==================================================================
// 抽屉（资产详情 / 血缘影响面）
// ==================================================================

let drawerState = null;

/**
 * 打开右侧抽屉。
 * @param {{title:string, subtitle?:string, body:Node|Node[], footer?:Node[], tabs?:Array, onClose?:Function}} config
 */
export function openDrawer(config) {
  closeDrawer();
  const root = qs('#drawerRoot');
  if (!root) return null;

  const bodyNode = el('div.drawer-body');
  appendChildren(bodyNode, config.body || null);

  const head = el('div.drawer-head', null, [
    el('div', null, [
      el('h2', { text: config.title || '', style: { fontSize: '16px' } }),
      config.subtitle ? el('div.card-sub', { text: config.subtitle }) : null,
    ]),
    el('div.card-actions', null, [
      ...(config.headActions || []),
      el('button.btn.btn-ghost.btn-sm', { type: 'button', text: '关闭', onClick: () => closeDrawer() }),
    ]),
  ]);

  const panel = el('div.drawer', null, [
    head,
    bodyNode,
    config.footer && config.footer.length ? el('div.drawer-foot', null, config.footer) : null,
  ]);

  const mask = el('div.drawer-mask', { onClick: () => closeDrawer() });
  root.textContent = '';
  root.appendChild(mask);
  root.appendChild(panel);
  root.hidden = false;
  document.body.style.overflow = 'hidden';

  const onKey = (event) => {
    if (event.key === 'Escape') closeDrawer();
  };
  document.addEventListener('keydown', onKey);

  drawerState = {
    onClose: config.onClose || null,
    cleanup: () => document.removeEventListener('keydown', onKey),
    body: bodyNode,
  };
  return drawerState;
}

export function closeDrawer() {
  const root = qs('#drawerRoot');
  if (drawerState?.cleanup) drawerState.cleanup();
  const callback = drawerState?.onClose;
  drawerState = null;
  if (root) {
    root.hidden = true;
    root.textContent = '';
  }
  document.body.style.overflow = '';
  if (typeof callback === 'function') callback();
}

export function isDrawerOpen() {
  return drawerState !== null;
}
