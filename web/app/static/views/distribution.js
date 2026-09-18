/**
 * distribution.js —— 统计与分布。
 *
 * 两条业务线索：
 * 1. **分布形态** —— 均值/离散程度/五数概括/直方图分箱（回答"数据长什么样"）；
 * 2. **指标相关** —— Pearson 矩阵与最强相关对（回答"哪些指标同向变化"）。
 *
 * 数据来源：
 *  * `/api/insights/distribution` —— 逐指标统计、箱线图五数概括、直方图、逐城市对比；
 *  * `/api/insights/correlation`  —— Pearson / Spearman 矩阵与 highlights。
 *
 * 所有 insights 接口在数据不足时返回 `available:false` + 中文 `message`，
 * 本页据此渲染"前往数据刷新"的空状态，绝不画空图表。
 *
 * 时间口径：接口返回的 `time`/`date` 均为城市本地裸时间字符串，本页只做格式化，
 * 不做任何时区换算。
 */

import { api } from '/shared/api.js';
import { renderChart } from '/shared/charts.js';
import { accent, tint, withAlpha } from '/shared/theme.js';
import {
  badge,
  button,
  card,
  chip,
  dash,
  el,
  emptyState,
  formatInt,
  formatPercent,
  kpiCard,
  mount,
  note,
  select,
  table,
} from '/shared/ui.js';

const WINDOW_OPTIONS = [
  { value: '7', label: '近 7 天' },
  { value: '30', label: '近 30 天' },
  { value: '90', label: '近 90 天' },
  { value: '180', label: '近 180 天' },
  { value: '365', label: '近 365 天' },
];

const DEFAULT_METRICS = ['temperature_2m', 'european_aqi', 'pm2_5'];

const state = {
  days: 30,
  bins: 20,
  cities: [],
  metrics: [],
  catalog: new Map(),
  metricSet: new Set(),
  citySelection: new Set(),
  sort: { key: 'mean', dir: 'desc' },
  hosts: null,
};

let seq = 0;

// ==================================================================
// 局部工具
// ==================================================================

/** 递增序号，用于生成不会冲突的容器 id。 */
function nextId(prefix) {
  seq += 1;
  return `${prefix}-${seq}`;
}

function isNum(value) {
  return typeof value === 'number' && Number.isFinite(value);
}

function num(value) {
  return isNum(value) ? value : null;
}

/** 数值 + 单位：先按量级切换紧凑写法，再补单位；缺失值统一破折号。 */
function fmt(value, unit = '', digits = 2) {
  if (!isNum(value)) return dash(value);
  const abs = Math.abs(value);
  let text;
  if (abs >= 1e8) text = `${(value / 1e8).toFixed(2)}亿`;
  else if (abs >= 1e5) text = `${(value / 1e4).toFixed(1)}万`;
  else if (abs >= 1000) text = value.toFixed(0);
  else text = value.toFixed(digits);
  return unit ? `${text} ${unit}` : text;
}

/** 单位后缀，例如 "（μg/m³）"；无量纲指标返回空串。 */
function unitSuffix(unit) {
  return unit ? `（${unit}）` : '';
}

function metricLabel(name) {
  const meta = state.catalog.get(name);
  return meta?.label || name;
}

/** 兜底：把接口返回中的指标信息合并进本地字典。 */
function mergeMetric(name, label, unit) {
  const known = state.catalog.get(name) || {};
  state.catalog.set(name, {
    ...known,
    name,
    label: label || known.label || name,
    unit: unit ?? known.unit ?? '',
  });
}

/**
 * 面板：统一处理"局部加载中 / 局部失败 / 局部空数据"，
 * 保证任一子请求失败时页面其余部分仍然可用。
 */
function panelHost(title, options = {}) {
  const host = el('div');
  return { node: card(title, host, options), host };
}

function panelLoading(panel, text = '正在加载…') {
  mount(panel.host, emptyState({ title: text, compact: true }));
}

function panelError(panel, error, onRetry) {
  mount(panel.host, emptyState({
    title: '该面板加载失败',
    hint: error?.message || '未知错误',
    compact: true,
    actions: onRetry ? [button('重试', { onClick: onRetry })] : [],
  }));
}

/** 数据不足时的统一空状态：明确引导到「数据刷新」。 */
function panelUnavailable(panel, message, ctx) {
  mount(panel.host, emptyState({
    title: '数据不足，暂无法分析',
    hint: `${message || '窗口内没有足够的可用数据。'}<br />请前往「数据刷新」触发一次数据采集，采集完成后再回到本页。`,
    compact: true,
    actions: [
      button('前往数据刷新', { kind: 'primary', onClick: () => ctx && ctx.navigate('ops') }),
      button('刷新本页', { onClick: () => ctx && ctx.refresh() }),
    ],
  }));
}

/** 排序后的行；sortKey 为空时保持原顺序。 */
function sortedRows(rows, sortKey, sortDir) {
  if (!sortKey) return rows;
  const factor = sortDir === 'asc' ? 1 : -1;
  return [...rows].sort((left, right) => {
    const a = left?.[sortKey];
    const b = right?.[sortKey];
    if (a === null || a === undefined) return 1;
    if (b === null || b === undefined) return -1;
    if (typeof a === 'number' && typeof b === 'number') return (a - b) * factor;
    return String(a).localeCompare(String(b), 'zh-CN') * factor;
  });
}

/** 可排序表头：点击同一列切换方向，切换到新列时回到降序。 */
function sortHandler(nextKey) {
  if (state.sort.key === nextKey) {
    state.sort = { key: nextKey, dir: state.sort.dir === 'desc' ? 'asc' : 'desc' };
  } else {
    state.sort = { key: nextKey, dir: 'desc' };
  }
  renderBody();
}

/** 表格包裹：统一注入排序状态与回调。 */
function sortedTable(columns, rows, empty = '暂无记录') {
  const keys = new Set(columns.map((column) => column.key));
  const sortable = keys.has(state.sort.key) ? state.sort.key : null;
  return table(columns, sortedRows(rows, sortable, state.sort.dir), {
    empty,
    sortKey: sortable,
    sortDir: state.sort.dir,
    onSort: sortHandler,
    rowKey: (row) => `${row.metric || ''}-${row.city_slug || row.city_name || ''}`,
  });
}

function coefficientClass(value) {
  if (!isNum(value)) return 'text-mute';
  if (Math.abs(value) >= 0.6) return value > 0 ? 'text-bad' : 'text-ok';
  if (Math.abs(value) >= 0.4) return 'text-warn';
  return 'text-dim';
}

// ==================================================================
// 生命周期
// ==================================================================

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在读取分析目录…', hint: '加载城市主数据与指标字典。', compact: true }));

  const [cityPayload, metricPayload] = await Promise.all([
    api.envCities().catch(() => ({ items: [] })),
    api.envMetrics().catch(() => ({ items: [] })),
  ]);

  state.cities = cityPayload?.items || [];
  const catalogItems = metricPayload?.items || [];
  state.catalog = new Map(catalogItems.map((item) => [item.name, item]));
  state.metrics = catalogItems.filter((item) => item.numeric);
  if (!state.metrics.length) {
    state.metrics = DEFAULT_METRICS.map((name) => ({ name, label: name, unit: '' }));
  }

  if (!state.metricSet.size) {
    const available = new Set(state.metrics.map((item) => item.name));
    const preferred = DEFAULT_METRICS.filter((name) => available.has(name));
    const fallback = state.metrics.slice(0, 3).map((item) => item.name);
    state.metricSet = new Set(preferred.length ? preferred : fallback);
  }

  state.hosts = { control: el('div'), body: el('div') };
  mount(container, state.hosts.control, state.hosts.body);
  renderControls(ctx);
  renderBody(ctx);
}

export function destroy() {
  state.hosts = null;
  state.sort = { key: 'mean', dir: 'desc' };
}

// ==================================================================
// 控件
// ==================================================================

function selectedMetrics() {
  const names = [...state.metricSet];
  return names.length ? names : state.metrics.slice(0, 1).map((item) => item.name);
}

/** 城市筛选：为空表示全部（接口对空值即取全部启用城市）。 */
function requestedCities() {
  const slugs = [...state.citySelection];
  return slugs.length ? slugs : null;
}

function cityText() {
  const slugs = [...state.citySelection];
  if (!slugs.length) return `全部 ${state.cities.length} 个城市`;
  return `${slugs.length} 个城市`;
}

function renderControls(ctx) {
  const requested = selectedMetrics();

  const daySelect = select(WINDOW_OPTIONS, {
    value: String(state.days),
    onChange: (value) => {
      state.days = Number(value);
      renderBody(ctx);
    },
  });

  const metricChips = state.metrics.slice(0, 28).map((item) =>
    chip(item.label || item.name, null, state.metricSet.has(item.name), () => {
      if (state.metricSet.has(item.name)) {
        if (state.metricSet.size <= 1) return;
        state.metricSet.delete(item.name);
      } else {
        state.metricSet.add(item.name);
      }
      renderControls(ctx);
      renderBody(ctx);
    }),
  );

  const cityChips = state.cities.map((city) =>
    chip(city.name_zh, null, state.citySelection.has(city.slug), () => {
      if (state.citySelection.has(city.slug)) state.citySelection.delete(city.slug);
      else state.citySelection.add(city.slug);
      renderControls(ctx);
      renderBody(ctx);
    }),
  );

  mount(
    state.hosts.control,
    card('分析范围', el('div', null, [
      el('div.toolbar', null, [
        el('div.field', null, [el('label', { text: '时间窗口' }), daySelect]),
        el('div.field', null, [
          el('label', { text: '操作' }),
          el('div.card-actions', null, [
            button('重新计算', { kind: 'primary', onClick: () => renderBody(ctx) }),
            button('清空城市选择', {
              onClick: () => {
                state.citySelection.clear();
                renderControls(ctx);
                renderBody(ctx);
              },
            }),
            button('前往数据刷新', { onClick: () => ctx.navigate('ops') }),
          ]),
        ]),
      ]),
      el('div.field', { style: { marginTop: '12px' } }, [
        el('label', { text: `对比指标（已选 ${requested.length} 个，至少保留 1 个）` }),
        el('div.chip-row', null, metricChips),
      ]),
      el('div.field', { style: { marginTop: '12px' } }, [
        el('label', { text: `城市（不选表示全部；当前：${cityText()}）` }),
        el('div.chip-row', null, cityChips.length ? cityChips : [el('span.text-mute', { text: '暂无可用城市' })]),
      ]),
    ]), {
      subtitle: '数据时间为城市本地时间（裸时间字符串），本页不做时区换算；统计基于融合宽表逐小时记录。',
      actions: [badge(`近 ${state.days} 天`, 'badge-accent')],
    }),
  );
}

// ==================================================================
// 主体
// ==================================================================

async function renderBody(ctx) {
  const host = state.hosts?.body;
  if (!host) return;

  const metrics = selectedMetrics();
  const distributionPanel = panelHost('描述统计与分布', {
    subtitle: `${metrics.map(metricLabel).join(' · ')} · ${cityText()} · 近 ${state.days} 天`,
  });
  const correlationPanel = panelHost('相关系数矩阵', {
    subtitle: 'Pearson 线性相关 + Spearman 秩相关；同时看两者可区分线性与单调非线性关系。',
  });

  mount(
    host,
    el('div', { style: { display: 'flex', flexDirection: 'column', gap: '16px' } }, [
      distributionPanel.node,
      correlationPanel.node,
    ]),
  );

  await Promise.all([
    loadDistribution(distributionPanel, metrics, ctx),
    loadCorrelation(correlationPanel, metrics, ctx),
  ]);
}

// ==================================================================
// 统计与分布
// ==================================================================

async function loadDistribution(panel, metrics, ctx) {
  panelLoading(panel, '正在计算分布统计…');
  let payload;
  try {
    payload = await api.insightsDistribution({ cities: requestedCities(), metrics, days: state.days, bins: state.bins });
  } catch (error) {
    panelError(panel, error, () => loadDistribution(panel, metrics, ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  const items = (payload.metrics || []).filter((item) => item && item.overall);
  if (!items.length) {
    panelUnavailable(panel, '所选指标在窗口内没有有效数值', ctx);
    return;
  }

  for (const item of items) mergeMetric(item.metric, item.label, item.unit);

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, buildKpis(items, payload)),
    el('div.grid.grid-2', null, items.map((item) => boxplotCard(item))),
    el('div.grid.grid-2', null, items.map((item) => histogramCard(item))),
    statsTableCard(items),
    note(
      `统计口径：五数概括为 Tukey 箱线图（1.5 倍四分位距围栏），须线端点取自围栏内的最小/最大值；` +
      `变异系数 CV = 标准差 / 均值，用于跨量纲比较波动性。共 ${formatInt(payload.row_count)} 条逐小时记录，` +
      `覆盖 ${formatInt(payload.city_count)} 个城市。`,
    ),
  );
}

function buildKpis(items, payload) {
  if (items.length === 1) {
    const item = items[0];
    const overall = item.overall || {};
    return [
      kpiCard({
        label: '均值',
        value: fmt(overall.mean, '', 2),
        unit: item.unit || '',
        foot: `${metricLabel(item.metric)} · ${formatInt(overall.count)} 个样本`,
      }),
      kpiCard({ label: '标准差', value: fmt(overall.std, '', 2), unit: item.unit || '', foot: `变异系数 CV ${fmt(overall.cv, '', 3)}` }),
      kpiCard({ label: '最小值', value: fmt(overall.min, '', 2), unit: item.unit || '', foot: `P5 ${fmt(overall.p5, '', 2)}` }),
      kpiCard({ label: '最大值', value: fmt(overall.max, '', 2), unit: item.unit || '', foot: `P95 ${fmt(overall.p95, '', 2)}` }),
      kpiCard({
        label: '中位数 P50',
        value: fmt(overall.p50, '', 2),
        unit: item.unit || '',
        foot: `P25 ${fmt(overall.p25, '', 2)} / P75 ${fmt(overall.p75, '', 2)}`,
      }),
      kpiCard({ label: '极差', value: fmt(overall.range, '', 2), unit: item.unit || '', foot: '最大值 − 最小值' }),
    ];
  }

  const indexes = items.map((item) => ({
    label: item.label || metricLabel(item.metric),
    unit: item.unit || '',
    cv: isNum(item.overall?.cv) ? Math.abs(item.overall.cv) : null,
    mean: item.overall?.mean,
    std: item.overall?.std,
  }));

  const mostVolatile = indexes.filter((item) => item.cv !== null).sort((a, b) => b.cv - a.cv)[0] || null;
  const widest = indexes
    .filter((item) => isNum(item.mean) && isNum(item.std) && item.std !== 0)
    .sort((a, b) => b.std - a.std)[0] || null;

  return [
    kpiCard({
      label: '参与分析的指标',
      value: formatInt(items.length),
      unit: '个',
      foot: `窗口 ${formatInt(payload.window_days || state.days)} 天`,
    }),
    kpiCard({ label: '逐小时记录数', value: formatInt(payload.row_count), unit: '条', foot: '融合宽表原始记录' }),
    kpiCard({ label: '覆盖城市', value: formatInt(payload.city_count), unit: '个', foot: '按城市分组统计' }),
    kpiCard({
      label: '波动最大指标',
      value: mostVolatile ? mostVolatile.label : '—',
      foot: mostVolatile ? `变异系数 ${fmt(mostVolatile.cv, '', 3)}` : '样本不足',
      tone: 'warn',
    }),
    kpiCard({
      label: '离散度最高指标',
      value: widest ? widest.label : '—',
      unit: widest ? widest.unit : '',
      foot: widest ? `标准差 ${fmt(widest.std, '', 2)}` : '样本不足',
    }),
    kpiCard({ label: '直方图分箱', value: formatInt(state.bins), unit: '箱', foot: '等宽分箱' }),
  ];
}

function boxplotCard(item) {
  const rows = (item.by_city || []).filter((row) => row.boxplot);
  const chartId = nextId('dist-box');
  if (!rows.length) {
    return card(`${item.label || metricLabel(item.metric)} 逐城市箱线图`, emptyState({
      title: '该指标没有可用的逐城市箱线数据',
      compact: true,
    }));
  }

  const node = card(
    `${item.label || metricLabel(item.metric)} 逐城市箱线图`,
    el('div.chart', { id: chartId, style: { height: '300px' } }),
    {
      subtitle: `${unitSuffix(item.unit) || '无量纲'} · ${formatInt(rows.length)} 个城市 · 城市本地时间窗口近 ${state.days} 天`,
    },
  );

  // 延迟到插入 DOM 之后再绘制：容器尺寸为 0 时 ECharts 初始化会失败
  window.requestAnimationFrame(() => {
    const target = document.getElementById(chartId);
    if (!target || !target.isConnected) return;
    renderChart(target, {
      backgroundColor: 'transparent',
      animation: false,
      textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
      grid: { left: 10, right: 18, top: 24, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15, 22, 38, 0.96)',
        borderColor: '#33415c',
        borderWidth: 1,
        textStyle: { color: '#e6ebf4', fontSize: 12 },
        confine: true,
      },
      xAxis: {
        type: 'category',
        data: rows.map((row) => row.city_name || row.city_slug),
        axisLabel: { color: '#7b889e', fontSize: 11, interval: 0, hideOverlap: true },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: item.unit || '',
        nameTextStyle: { color: '#7b889e', fontSize: 11 },
        axisLabel: { color: '#7b889e', fontSize: 11 },
        splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
      },
      series: [
        {
          name: item.label || item.metric,
          type: 'boxplot',
          data: rows.map((row) => [
            num(row.boxplot.whisker_low),
            num(row.boxplot.q1),
            num(row.boxplot.median),
            num(row.boxplot.q3),
            num(row.boxplot.whisker_high),
          ]),
          boxWidth: [10, 34],
          itemStyle: { color: withAlpha(accent(), 0.22), borderColor: accent(), borderWidth: 1.4 },
          emphasis: { itemStyle: { borderColor: tint(accent(), 0.5), borderWidth: 2 } },
        },
      ],
    });
  });

  return node;
}

function histogramCard(item) {
  const bins = item.histogram?.bins || [];
  const chartId = nextId('dist-hist');
  const unit = item.unit || '';

  if (!bins.length) {
    return card(`${item.label || metricLabel(item.metric)} 分布直方图`, emptyState({
      title: '没有可用的分箱结果',
      compact: true,
    }));
  }

  const node = card(
    `${item.label || metricLabel(item.metric)} 分布直方图`,
    el('div.chart', { id: chartId, style: { height: '300px' } }),
    {
      subtitle: `${unitSuffix(unit) || '无量纲'} · ${formatInt(item.overall?.count)} 个样本 · 等宽 ${formatInt(bins.length)} 箱`,
    },
  );

  window.requestAnimationFrame(() => {
    const target = document.getElementById(chartId);
    if (!target || !target.isConnected) return;
    renderChart(target, {
      backgroundColor: 'transparent',
      animation: false,
      textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
      grid: { left: 10, right: 18, top: 26, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: 'rgba(15, 22, 38, 0.96)',
        borderColor: '#33415c',
        borderWidth: 1,
        textStyle: { color: '#e6ebf4', fontSize: 12 },
        confine: true,
        formatter: (params) => {
          const bin = bins[params?.[0]?.dataIndex ?? 0];
          if (!bin) return '';
          return [
            `区间 ${bin.lower} ~ ${bin.upper}${unit ? ` ${unit}` : ''}`,
            `记录数 ${formatInt(bin.count)}`,
            `占比 ${formatPercent(bin.ratio, 2)}`,
          ].join('<br />');
        },
      },
      xAxis: {
        type: 'category',
        data: bins.map((bin) => String(bin.lower)),
        name: unit || '',
        nameTextStyle: { color: '#7b889e', fontSize: 11 },
        axisLabel: { color: '#7b889e', fontSize: 10, hideOverlap: true },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: '记录数',
        nameTextStyle: { color: '#7b889e', fontSize: 11 },
        axisLabel: { color: '#7b889e', fontSize: 11 },
        splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
      },
      series: [
        {
          name: '记录数',
          type: 'bar',
          data: bins.map((bin) => bin.count),
          barMaxWidth: 26,
          itemStyle: { color: '#38bdf8', borderRadius: [3, 3, 0, 0] },
        },
      ],
    });
  });

  return node;
}

function statsTableCard(items) {
  const rows = [];
  for (const item of items) {
    for (const row of item.by_city || []) {
      rows.push({
        metric: item.metric,
        metric_label: item.label || metricLabel(item.metric),
        unit: item.unit || '',
        city_slug: row.city_slug,
        city_name: row.city_name || row.city_slug,
        count: row.count,
        mean: row.mean,
        std: row.std,
        min: row.min,
        max: row.max,
        p50: row.p50,
        p95: row.p95,
        cv: row.cv,
      });
    }
  }

  const columns = [
    { key: 'metric_label', title: '指标', sortable: true },
    { key: 'city_name', title: '城市', sortable: true },
    { key: 'mean', title: '均值', align: 'right', sortable: true, render: (row) => fmt(row.mean, '', 2) },
    { key: 'p50', title: 'P50 中位数', align: 'right', sortable: true, render: (row) => fmt(row.p50, '', 2) },
    { key: 'p95', title: 'P95', align: 'right', sortable: true, render: (row) => fmt(row.p95, '', 2) },
    { key: 'std', title: '标准差', align: 'right', sortable: true, render: (row) => fmt(row.std, '', 2) },
    {
      key: 'cv',
      title: '变异系数 CV',
      align: 'right',
      sortable: true,
      render: (row) => (isNum(row.cv) ? fmt(Math.abs(row.cv), '', 3) : '—'),
    },
    { key: 'min', title: '最小值', align: 'right', sortable: true, render: (row) => fmt(row.min, '', 2) },
    { key: 'max', title: '最大值', align: 'right', sortable: true, render: (row) => fmt(row.max, '', 2) },
    { key: 'count', title: '样本数', align: 'right', sortable: true, render: (row) => formatInt(row.count) },
    { key: 'unit', title: '单位', render: (row) => el('span.text-mute', { text: row.unit || '无量纲' }) },
  ];

  return card('逐城市统计明细', sortedTable(columns, rows, '窗口内没有可用的逐城市统计'), {
    subtitle: `共 ${formatInt(rows.length)} 行 · 点击表头排序（单位随指标变化，见「单位」列）`,
  });
}

// ==================================================================
// 相关系数矩阵
// ==================================================================

async function loadCorrelation(panel, metrics, ctx) {
  panelLoading(panel, '正在计算相关系数矩阵…');
  let payload;
  try {
    payload = await api.insightsCorrelation({ cities: requestedCities(), metrics, days: state.days });
  } catch (error) {
    panelError(panel, error, () => loadCorrelation(panel, metrics, ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  const labels = payload.labels || [];
  const labelsZh = payload.labels_zh || labels;
  const units = payload.units || [];
  const matrix = payload.pearson || [];

  if (labels.length < 2 || !matrix.length) {
    panelUnavailable(panel, '可用指标不足两个，无法计算相关性', ctx);
    return;
  }

  const cells = [];
  let peak = null;
  for (let row = 0; row < matrix.length; row += 1) {
    const line = matrix[row] || [];
    for (let col = 0; col < line.length; col += 1) {
      const value = num(line[col]);
      if (value === null) continue;
      cells.push([col, row, Number(value.toFixed(3))]);
      if (row !== col && (peak === null || Math.abs(value) > Math.abs(peak.value))) {
        peak = { value, row, col };
      }
    }
  }

  const heatId = nextId('dist-heat');
  const heatNode = el('div.chart', { id: heatId, style: { height: `${Math.max(300, labels.length * 30 + 120)}px` } });

  const highlightTable = table(
    [
      { key: 'left', title: '指标 A', render: (row) => metricLabel(row.left) },
      { key: 'right', title: '指标 B', render: (row) => metricLabel(row.right) },
      {
        key: 'pearson',
        title: 'Pearson',
        align: 'right',
        render: (row) => el('span', { class: coefficientClass(row.pearson), text: fmt(row.pearson, '', 3) }),
      },
      { key: 'spearman', title: 'Spearman', align: 'right', render: (row) => fmt(row.spearman, '', 3) },
      { key: 'strength', title: '强度判定', render: (row) => badge(row.strength || '—', 'badge-info') },
    ],
    payload.highlights || [],
    { empty: '没有达到展示门槛的相关对' },
  );

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, [
      kpiCard({ label: '参与指标', value: formatInt(labels.length), unit: '个', foot: `有效记录 ${formatInt(payload.row_count)} 条` }),
      kpiCard({
        label: '最强相关对',
        value: peak ? `${metricLabel(labels[peak.col])} / ${metricLabel(labels[peak.row])}` : '—',
        foot: peak ? `Pearson ${fmt(peak.value, '', 3)}` : '无可计算的相关对',
        tone: peak && Math.abs(peak.value) >= 0.6 ? 'warn' : '',
      }),
      kpiCard({
        label: '最强相关强度',
        value: peak ? fmt(Math.abs(peak.value), '', 3) : '—',
        foot: peak ? strengthText(peak.value) : '—',
      }),
      kpiCard({
        label: '覆盖城市',
        value: formatInt((payload.cities || []).length),
        unit: '个',
        foot: `近 ${formatInt(payload.window_days || state.days)} 天`,
      }),
    ]),
    card('Pearson 相关系数矩阵', heatNode, {
      subtitle: '纵轴为指标 A，横轴为指标 B；色标接近 1 表示正向同变，接近 −1 表示反向同变，对角线恒为 1（缺失值不参与计算）。',
    }),
    card('最强相关对明细', highlightTable, {
      subtitle: '按 |Pearson| 降序取前 12 对；Pearson 捕捉线性关系，Spearman 捕捉单调关系，两者差异明显时提示存在非线性。',
    }),
    note(
      `单位：${labels
        .map((name, index) => `${metricLabel(name)}${units[index] ? `（${units[index]}）` : ''}`)
        .join('、')}。相关性只描述同变关系，不构成因果结论。`,
    ),
  );

  window.requestAnimationFrame(() => {
    const target = document.getElementById(heatId);
    if (!target || !target.isConnected) return;
    renderChart(target, {
      backgroundColor: 'transparent',
      animation: false,
      textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
      grid: { left: 12, right: 18, top: 20, bottom: 86, containLabel: true },
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15, 22, 38, 0.96)',
        borderColor: '#33415c',
        borderWidth: 1,
        textStyle: { color: '#e6ebf4', fontSize: 12 },
        confine: true,
        formatter: (params) => {
          const col = labelsZh[params.value[0]] || labels[params.value[0]];
          const row = labelsZh[params.value[1]] || labels[params.value[1]];
          return `${row} × ${col}<br />Pearson ${params.value[2]}`;
        },
      },
      xAxis: {
        type: 'category',
        data: labelsZh,
        splitArea: { show: true, areaStyle: { color: ['rgba(21, 29, 46, 0.35)', 'rgba(11, 17, 32, 0.35)'] } },
        axisLabel: { color: '#7b889e', fontSize: 10.5, interval: 0, rotate: 45 },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'category',
        data: labelsZh,
        splitArea: { show: true, areaStyle: { color: ['rgba(21, 29, 46, 0.35)', 'rgba(11, 17, 32, 0.35)'] } },
        axisLabel: { color: '#7b889e', fontSize: 10.5, interval: 0 },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      visualMap: {
        min: -1,
        max: 1,
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 6,
        textStyle: { color: '#7b889e', fontSize: 11 },
        inRange: { color: ['#b91c1c', '#e2e8f0', '#1d4ed8'] },
      },
      series: [
        {
          name: 'Pearson',
          type: 'heatmap',
          data: cells,
          label: {
            show: labels.length <= 10,
            color: '#e6ebf4',
            fontSize: 10,
            formatter: (params) => params.value[2],
          },
          itemStyle: { borderColor: 'rgba(11, 17, 32, 0.6)', borderWidth: 0.5 },
          emphasis: { itemStyle: { borderColor: '#93c5fd', borderWidth: 1.5 } },
        },
      ],
    });
  });
}

function strengthText(value) {
  const magnitude = Math.abs(value);
  if (magnitude >= 0.8) return '极强';
  if (magnitude >= 0.6) return '强';
  if (magnitude >= 0.4) return '中等';
  if (magnitude >= 0.2) return '弱';
  return '几乎无';
}
