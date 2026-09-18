/**
 * profile.js —— 时段规律（三个标签页）。
 *
 * 用"重复出现的模式"回答治理问题：
 *  * 日内小时 —— 一天之内什么时候最脏？各城市日变化幅度差多少？
 *  * 周内规律 —— 工作日与周末有差别吗？差别多大？
 *  * 月度趋势 —— 月度水平在改善还是转差？
 *
 * 数据来源：`/api/insights/hourly-profile`、`/api/insights/weekly-profile`、
 * `/api/insights/monthly-trend`。三者在数据不足时均返回 `available:false` +
 * 中文 `message`，本页据此渲染空状态并引导到「数据刷新」。
 *
 * 时间口径：`time`/`date` 为城市本地裸时间字符串，小时分组直接取该字符串的小时位，
 * 不做时区换算。
 */

import { api } from '/shared/api.js';
import { renderChart } from '/shared/charts.js';
import { accent, seriesPalette } from '/shared/theme.js';
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

const WEEKDAY_ORDER = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];

const HOUR_METRIC_FALLBACK = 'european_aqi';
const MONTH_METRIC_FALLBACK = 'european_aqi';
const WEEKLY_METRIC_FALLBACK = ['temperature_2m', 'european_aqi', 'pm2_5'];

const TABS = [
  { key: 'hourly', label: '日内小时' },
  { key: 'weekly', label: '周内规律' },
  { key: 'monthly', label: '月度趋势' },
];

const state = {
  tab: 'hourly',
  days: 30,
  cities: [],
  catalog: new Map(),
  metrics: [],
  citySelection: new Set(),
  weeklyMetrics: null,
  /** 单指标标签页当前选择的指标（hourly / monthly 各自记忆）。 */
  tabMetric: { hourly: null, monthly: null },
  data: { hourly: null, weekly: null, monthly: null },
  hosts: null,
  sort: {
    hourly: { key: 'amplitude', dir: 'desc' },
    weekly: { key: 'delta_ratio', dir: 'desc' },
    monthly: { key: 'month', dir: 'desc' },
  },
};

let seq = 0;

// ==================================================================
// 局部工具
// ==================================================================

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

function unitSuffix(unit) {
  return unit ? `（${unit}）` : '';
}

function metricLabel(name) {
  const meta = state.catalog.get(name);
  return meta?.label || name;
}

function metricUnit(name) {
  return state.catalog.get(name)?.unit || '';
}

function mergeMetric(name, label, unit) {
  const known = state.catalog.get(name) || {};
  state.catalog.set(name, {
    ...known,
    name,
    label: label || known.label || name,
    unit: unit ?? known.unit ?? '',
  });
}

function requestedCities() {
  const slugs = [...state.citySelection];
  return slugs.length ? slugs : null;
}

function cityText() {
  const slugs = [...state.citySelection];
  if (!slugs.length) return `全部 ${state.cities.length} 个城市`;
  return `${slugs.length} 个城市`;
}

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

function makeSort(tab) {
  return (nextKey) => {
    const current = state.sort[tab];
    if (current.key === nextKey) {
      state.sort[tab] = { key: nextKey, dir: current.dir === 'desc' ? 'asc' : 'desc' };
    } else {
      state.sort[tab] = { key: nextKey, dir: 'desc' };
    }
    renderTabBody(ctxRef);
  };
}

/** 当前视图上下文（供排序回调在无参调用时使用）。 */
let ctxRef = null;

function sortedTableFor(tab, columns, rows, empty) {
  const keys = new Set(columns.map((column) => column.key));
  const key = keys.has(state.sort[tab].key) ? state.sort[tab].key : null;
  return table(columns, sortedRows(rows, key, state.sort[tab].dir), {
    empty,
    sortKey: key,
    sortDir: state.sort[tab].dir,
    onSort: makeSort(tab),
  });
}

function hourLabel(hour) {
  if (!isNum(hour)) return '—';
  return `${String(hour).padStart(2, '0')}:00`;
}

// ==================================================================
// 生命周期
// ==================================================================

export async function render(container, ctx) {
  ctxRef = ctx;
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
    state.metrics = [
      { name: HOUR_METRIC_FALLBACK, label: HOUR_METRIC_FALLBACK, unit: '' },
    ];
  }

  state.hosts = { control: el('div'), toolbar: el('div'), body: el('div') };
  mount(container, state.hosts.control, state.hosts.toolbar, state.hosts.body);

  renderControls(ctx);
  renderToolbar(ctx);
  renderTabBody(ctx);
}

export function destroy() {
  ctxRef = null;
  state.hosts = null;
  state.data = { hourly: null, weekly: null, monthly: null };
}

// ==================================================================
// 控件
// ==================================================================

function metricOptions() {
  return state.metrics.map((item) => ({
    value: item.name,
    label: `${item.label || item.name}${item.unit ? `（${item.unit}）` : ''}`,
  }));
}

/** 当前标签页偏好的指标名（用于渲染工具栏的默认值）。 */
function tabMetricKey(tab) {
  const available = new Set(state.metrics.map((item) => item.name));
  const chosen = state.tabMetric[tab];
  if (chosen && available.has(chosen)) return chosen;
  const preferred = tab === 'weekly' ? WEEKLY_METRIC_FALLBACK[0] : tab === 'monthly' ? MONTH_METRIC_FALLBACK : HOUR_METRIC_FALLBACK;
  if (available.has(preferred)) return preferred;
  return state.metrics[0]?.name || preferred;
}

/** 单指标标签页：选择指标后记忆并重绘该标签页。 */
function singleMetricField(tab, ctx) {
  return el('div.field', { style: { minWidth: '230px' } }, [
    el('label', { text: '分析指标' }),
    select(metricOptions(), {
      value: tabMetricKey(tab),
      onChange: (value) => {
        state.tabMetric[tab] = value;
        state.data[tab] = null;
        renderToolbar(ctx);
        renderTabBody(ctx);
      },
    }),
  ]);
}

function currentMetricEntries() {
  if (state.tab === 'weekly') {
    if (Array.isArray(state.weeklyMetrics) && state.weeklyMetrics.length) {
      const available = new Set(state.metrics.map((item) => item.name));
      const kept = state.weeklyMetrics.filter((name) => available.has(name));
      if (kept.length) return kept;
    }
    const available = new Set(state.metrics.map((item) => item.name));
    const picked = WEEKLY_METRIC_FALLBACK.filter((name) => available.has(name));
    return picked.length ? picked : [tabMetricKey('weekly')];
  }
  return [tabMetricKey(state.tab)];
}

function renderControls(ctx) {
  const daySelect = select(WINDOW_OPTIONS, {
    value: String(state.days),
    onChange: (value) => {
      state.days = Number(value);
      state.data[state.tab] = null;
      renderControls(ctx);
      renderToolbar(ctx);
      renderTabBody(ctx);
    },
  });

  const chips = state.cities.map((city) =>
    chip(city.name_zh, null, state.citySelection.has(city.slug), () => {
      if (state.citySelection.has(city.slug)) state.citySelection.delete(city.slug);
      else state.citySelection.add(city.slug);
      state.data[state.tab] = null;
      renderControls(ctx);
      renderToolbar(ctx);
      renderTabBody(ctx);
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
            button('重新计算', {
              kind: 'primary',
              onClick: () => {
                state.data[state.tab] = null;
                renderTabBody(ctx);
              },
            }),
            button('清空城市选择', {
              onClick: () => {
                state.citySelection.clear();
                state.data[state.tab] = null;
                renderControls(ctx);
                renderToolbar(ctx);
                renderTabBody(ctx);
              },
            }),
            button('前往数据刷新', { onClick: () => ctx.navigate('ops') }),
          ]),
        ]),
      ]),
      el('div.field', { style: { marginTop: '12px' } }, [
        el('label', { text: `城市（不选表示全部；当前：${cityText()}）` }),
        el('div.chip-row', null, chips.length ? chips : [el('span.text-mute', { text: '暂无可用城市' })]),
      ]),
    ]), {
      subtitle: '数据时间为城市本地时间（裸时间字符串），小时分组直接取该字符串的小时位，本页不做时区换算。',
      actions: [badge(`近 ${state.days} 天`, 'badge-accent')],
    }),
  );
}

/** 标签页切换栏 + 当前标签页专属的指标选择器。 */
function renderToolbar(ctx) {
  const tabButtons = TABS.map((tab) =>
    el('button.tab', {
      type: 'button',
      class: state.tab === tab.key ? 'active' : '',
      text: tab.label,
      onClick: () => {
        if (state.tab === tab.key) return;
        state.tab = tab.key;
        renderToolbar(ctx);
        renderTabBody(ctx);
      },
    }),
  );

  const fields = [];

  if (state.tab === 'hourly') {
    fields.push(singleMetricField('hourly', ctx));
  }

  if (state.tab === 'weekly') {
    const selectedWeekday = new Set(currentMetricEntries());
    const chips = state.metrics.map((item) =>
      chip(item.label || item.name, null, selectedWeekday.has(item.name), () => {
        const next = new Set(currentMetricEntries());
        if (next.has(item.name)) {
          if (next.size <= 1) return;
          next.delete(item.name);
        } else {
          next.add(item.name);
        }
        state.weeklyMetrics = [...next];
        state.data.weekly = null;
        renderToolbar(ctx);
        renderTabBody(ctx);
      }),
    );
    fields.push(el('div.field', { style: { flex: '1 1 260px' } }, [
      el('label', { text: '对比指标（多选，至少保留 1 个）' }),
      el('div.chip-row', null, chips),
    ]));
  }

  if (state.tab === 'monthly') {
    fields.push(singleMetricField('monthly', ctx));
  }

  mount(
    state.hosts.toolbar,
    card(TABS.find((tab) => tab.key === state.tab)?.label || '时段规律',
      el('div', null, [
        el('div.tabs', null, tabButtons),
        fields.length ? el('div.toolbar', { style: { marginTop: '12px' } }, fields) : null,
      ]),
      { subtitle: '三个标签页各自独立取数，切换标签只重绘该页内容。' },
    ),
  );
}

// ==================================================================
// 标签页分发
// ==================================================================

function renderTabBody(ctx) {
  if (!state.hosts) return;
  if (state.tab === 'hourly') return renderHourly(ctx);
  if (state.tab === 'weekly') return renderWeekly(ctx);
  return renderMonthly(ctx);
}

// ==================================================================
// 日内小时
// ==================================================================

async function renderHourly(ctx) {
  const host = state.hosts.body;
  const metric = tabMetricKey('hourly');
  const panel = panelHost('城市 × 小时热力图', {
    subtitle: `${metricLabel(metric)}${unitSuffix(metricUnit(metric))} · ${cityText()} · 近 ${state.days} 天（城市本地时间）`,
  });
  mount(host, panel.node);

  if (state.data.hourly && state.data.hourly.metric === metric && state.data.hourly.days === state.days && state.data.hourly.cityKey === cityKey()) {
    renderHourlyContent(panel, state.data.hourly.payload);
    return;
  }

  panelLoading(panel, '正在计算日内小时画像…');
  let payload;
  try {
    payload = await api.insightsHourlyProfile({ cities: requestedCities(), metric, days: state.days });
  } catch (error) {
    panelError(panel, error, () => renderHourly(ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  state.data.hourly = { metric, days: state.days, cityKey: cityKey(), payload };
  renderHourlyContent(panel, payload);
}

function cityKey() {
  return [...state.citySelection].sort().join(',');
}

function renderHourlyContent(panel, payload) {
  mergeMetric(payload.metric, payload.label, payload.unit);

  const rows = (payload.rows || []).filter((row) => Array.isArray(row.values));
  if (!rows.length) {
    panelUnavailable(panel, '所选指标在窗口内没有可用的逐小时数据', ctxRef);
    return;
  }

  const hours = payload.hours || Array.from({ length: 24 }, (_, index) => index);
  const cells = [];
  let peak = null;
  rows.forEach((row, rowIndex) => {
    row.values.forEach((value, hourIndex) => {
      const numeric = num(value);
      if (numeric === null) return;
      cells.push([hourIndex, rowIndex, Number(numeric.toFixed(3))]);
      if (peak === null || numeric > peak.value) {
        peak = { value: numeric, row, hour: hours[hourIndex] ?? hourIndex };
      }
    });
  });

  const chartId = nextId('profile-hour');
  const chartNode = el('div.chart', { id: chartId, style: { height: `${Math.max(300, rows.length * 30 + 110)}px` } });

  const columns = [
    { key: 'city_name', title: '城市', sortable: true },
    { key: 'peak_hour', title: '峰值时段', render: (row) => hourLabel(row.peak_hour) },
    { key: 'peak_value', title: '峰值', align: 'right', sortable: true, render: (row) => fmt(row.peak_value, payload.unit || '', 2) },
    { key: 'trough_hour', title: '谷值时段', render: (row) => hourLabel(row.trough_hour) },
    { key: 'trough_value', title: '谷值', align: 'right', sortable: true, render: (row) => fmt(row.trough_value, payload.unit || '', 2) },
    { key: 'amplitude', title: '日内振幅', align: 'right', sortable: true, render: (row) => fmt(row.amplitude, payload.unit || '', 2) },
  ];

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, [
      kpiCard({
        label: '全窗口最高时刻',
        value: peak ? `${peak.row.city_name} ${hourLabel(peak.hour)}` : '—',
        foot: peak ? `${metricLabel(payload.metric)} ${fmt(peak.value, payload.unit || '', 2)}` : '无有效数据',
        tone: 'warn',
      }),
      kpiCard({
        label: '日变化最剧烈城市',
        value: payload.most_volatile || '—',
        foot: rows.length ? `日内振幅 ${fmt(rows[0].amplitude, payload.unit || '', 2)}` : '—',
      }),
      kpiCard({ label: '参与统计的城市', value: formatInt(rows.length), unit: '个', foot: cityText() }),
      kpiCard({ label: '小时槽位', value: formatInt(hours.length), unit: '个', foot: '按本地小时取均值' }),
    ]),
    card('城市 × 小时均值热力图', chartNode, {
      subtitle: '纵轴为城市，横轴为本地小时；颜色越亮表示该时段的平均水平越高。',
    }),
    card('各城市峰值 / 谷值时段', sortedTableFor('hourly', columns, rows, '暂无逐城市峰值统计'), {
      subtitle: '按日内振幅降序排列；振幅越大说明该城市日变化越剧烈。',
    }),
    note('热力图与峰值统计均取窗口内逐小时记录的算术平均，"峰值时段"是平均意义上的高峰，不代表某一天的具体极值。'),
  );

  window.requestAnimationFrame(() => {
    const target = document.getElementById(chartId);
    if (!target || !target.isConnected) return;
    const values = cells.map((cell) => cell[2]);
    renderChart(target, {
      backgroundColor: 'transparent',
      animation: false,
      textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
      grid: { left: 12, right: 18, top: 18, bottom: 64, containLabel: true },
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(15, 22, 38, 0.96)',
        borderColor: '#33415c',
        borderWidth: 1,
        textStyle: { color: '#e6ebf4', fontSize: 12 },
        confine: true,
        formatter: (params) => {
          const row = rows[params.value[1]];
          const hour = hours[params.value[0]] ?? params.value[0];
          return [
            `${row?.city_name || ''} · ${hourLabel(hour)}`,
            `${metricLabel(payload.metric)} ${fmt(params.value[2], payload.unit || '', 2)}`,
          ].join('<br />');
        },
      },
      xAxis: {
        type: 'category',
        data: hours.map((hour) => hourLabel(hour)),
        splitArea: { show: true, areaStyle: { color: ['rgba(21, 29, 46, 0.35)', 'rgba(11, 17, 32, 0.35)'] } },
        axisLabel: { color: '#7b889e', fontSize: 10.5, interval: 0 },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'category',
        data: rows.map((row) => row.city_name || row.city_slug),
        splitArea: { show: true, areaStyle: { color: ['rgba(21, 29, 46, 0.35)', 'rgba(11, 17, 32, 0.35)'] } },
        axisLabel: { color: '#7b889e', fontSize: 11, interval: 0 },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      visualMap: {
        min: values.length ? Math.min(...values) : 0,
        max: values.length ? Math.max(...values) : 1,
        calculable: true,
        orient: 'horizontal',
        left: 'center',
        bottom: 6,
        textStyle: { color: '#7b889e', fontSize: 11 },
        inRange: { color: ['#0b1b33', '#1d4ed8', '#22d3ee', '#f59e0b', '#ef4444'] },
      },
      series: [
        {
          name: metricLabel(payload.metric),
          type: 'heatmap',
          data: cells,
          itemStyle: { borderColor: 'rgba(11, 17, 32, 0.6)', borderWidth: 0.5 },
          emphasis: { itemStyle: { borderColor: '#93c5fd', borderWidth: 1.5 } },
        },
      ],
    });
  });
}

// ==================================================================
// 周内规律
// ==================================================================

async function renderWeekly(ctx) {
  const host = state.hosts.body;
  const metrics = currentMetricEntries();
  const panel = panelHost('工作日与周末对比', {
    subtitle: `${metrics.map(metricLabel).join(' · ')} · ${cityText()} · 近 ${state.days} 天（按日汇总）`,
  });
  mount(host, panel.node);

  const key = `${metrics.join('|')}@${state.days}#${cityKey()}`;
  if (state.data.weekly && state.data.weekly.key === key) {
    renderWeeklyContent(panel, state.data.weekly.payload, ctx);
    return;
  }

  panelLoading(panel, '正在计算周内规律…');
  let payload;
  try {
    payload = await api.insightsWeeklyProfile({ cities: requestedCities(), metrics, days: state.days });
  } catch (error) {
    panelError(panel, error, () => renderWeekly(ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  state.data.weekly = { key, payload };
  renderWeeklyContent(panel, payload, ctx);
}

function renderWeeklyContent(panel, payload, ctx) {
  const items = payload.metrics || [];
  if (!items.length) {
    panelUnavailable(panel, '所选指标在窗口内没有可用的日汇总数据', ctx);
    return;
  }

  for (const item of items) mergeMetric(item.metric, item.label, item.unit);

  const labels = payload.weekday_labels?.length ? payload.weekday_labels : WEEKDAY_ORDER;
  const barId = nextId('profile-week-bar');
  const lineId = nextId('profile-week-line');

  const largest = items
    .filter((item) => isNum(item.delta_ratio))
    .sort((a, b) => Math.abs(b.delta_ratio) - Math.abs(a.delta_ratio))[0] || null;

  const columns = [
    { key: 'label', title: '指标', render: (row) => row.label || metricLabel(row.metric) },
    { key: 'unit', title: '单位', render: (row) => el('span.text-mute', { text: row.unit || '无量纲' }) },
    { key: 'weekday_mean', title: '工作日均值', align: 'right', sortable: true, render: (row) => fmt(row.weekday_mean, '', 2) },
    { key: 'weekend_mean', title: '周末均值', align: 'right', sortable: true, render: (row) => fmt(row.weekend_mean, '', 2) },
    {
      key: 'delta',
      title: '差值（周末 − 工作日）',
      align: 'right',
      sortable: true,
      render: (row) => el('span', {
        class: isNum(row.delta) ? (row.delta > 0 ? 'text-bad' : row.delta < 0 ? 'text-ok' : 'text-dim') : 'text-mute',
        text: fmt(row.delta, '', 2),
      }),
    },
    {
      key: 'delta_ratio',
      title: '相对变化',
      align: 'right',
      sortable: true,
      render: (row) => (isNum(row.delta_ratio) ? formatPercent(row.delta_ratio, 2) : '—'),
    },
    {
      key: 'higher_on',
      title: '水平更高',
      render: (row) => badge(row.higher_on || '—', row.higher_on === '周末' ? 'badge-warn' : 'badge-info'),
    },
  ];

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, [
      kpiCard({
        label: '差异最大指标',
        value: largest ? largest.label || metricLabel(largest.metric) : '—',
        foot: largest ? `周末相对工作日 ${isNum(largest.delta_ratio) ? formatPercent(largest.delta_ratio, 2) : '—'}` : '样本不足',
        tone: 'warn',
      }),
      kpiCard({ label: '参与对比的指标', value: formatInt(items.length), unit: '个', foot: metricsText(items) }),
      kpiCard({ label: '参与统计的日数', value: formatInt(payload.day_count), unit: '条', foot: '各城市日汇总记录合计' }),
      kpiCard({ label: '时间窗口', value: formatInt(payload.window_days || state.days), unit: '天', foot: cityText() }),
    ]),
    el('div.grid.grid-2', null, [
      card('工作日 vs 周末（分组柱状图）', el('div.chart', { id: barId, style: { height: '320px' } }), {
        subtitle: '同一指标并列展示两个均值，单位随指标变化。',
      }),
      card('周一至周日逐日水平', el('div.chart', { id: lineId, style: { height: '320px' } }), {
        subtitle: '横轴为星期，纵轴为窗口内该星期的平均值。',
      }),
    ]),
    card('周内规律明细', sortedTableFor('weekly', columns, items, '暂无周内规律统计'), {
      subtitle: '差值为「周末均值 − 工作日均值」，正值表示周末水平更高。',
    }),
    note('工作日 / 周末的差异只是现象描述：节假日调休、区域性排放变化等都可能造成同样的差异，不宜直接当作因果结论。'),
  );

  window.requestAnimationFrame(() => {
    const barNode = document.getElementById(barId);
    if (barNode && barNode.isConnected) {
      const categories = items.map((item) => item.label || metricLabel(item.metric));
      renderChart(barNode, {
        backgroundColor: 'transparent',
        animation: false,
        textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
        legend: { top: 0, right: 8, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: '#a9b5c9', fontSize: 11.5 } },
        grid: { left: 10, right: 20, top: 36, bottom: 6, containLabel: true },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'shadow' },
          backgroundColor: 'rgba(15, 22, 38, 0.96)',
          borderColor: '#33415c',
          borderWidth: 1,
          textStyle: { color: '#e6ebf4', fontSize: 12 },
          confine: true,
          formatter: (params) => {
            const item = items[params?.[0]?.dataIndex ?? 0];
            if (!item) return '';
            const unit = item.unit ? ` ${item.unit}` : '';
            return [
              `${item.label || metricLabel(item.metric)}`,
              `工作日 ${fmt(item.weekday_mean, unit, 2)}`,
              `周末 ${fmt(item.weekend_mean, unit, 2)}`,
              `相对变化 ${isNum(item.delta_ratio) ? formatPercent(item.delta_ratio, 2) : '—'}`,
            ].join('<br />');
          },
        },
        xAxis: {
          type: 'category',
          data: categories,
          axisLabel: { color: '#7b889e', fontSize: 11, interval: 0, hideOverlap: true },
          axisLine: { lineStyle: { color: '#263148' } },
          axisTick: { show: false },
        },
        yAxis: {
          type: 'value',
          axisLabel: { color: '#7b889e', fontSize: 11 },
          splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
        },
        series: [
          {
            name: '工作日均值',
            type: 'bar',
            data: items.map((item) => num(item.weekday_mean)),
            barMaxWidth: 20,
            itemStyle: { color: accent(), borderRadius: [3, 3, 0, 0] },
          },
          {
            name: '周末均值',
            type: 'bar',
            data: items.map((item) => num(item.weekend_mean)),
            barMaxWidth: 20,
            itemStyle: { color: '#f59e0b', borderRadius: [3, 3, 0, 0] },
          },
        ],
      });
    }

    const lineNode = document.getElementById(lineId);
    if (lineNode && lineNode.isConnected) {
      const colors = seriesPalette();
      renderChart(lineNode, {
        backgroundColor: 'transparent',
        animation: false,
        textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
        legend: { top: 0, right: 8, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: '#a9b5c9', fontSize: 11.5 } },
        grid: { left: 10, right: 20, top: 36, bottom: 6, containLabel: true },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'cross' },
          backgroundColor: 'rgba(15, 22, 38, 0.96)',
          borderColor: '#33415c',
          borderWidth: 1,
          textStyle: { color: '#e6ebf4', fontSize: 12 },
          confine: true,
        },
        xAxis: {
          type: 'category',
          boundaryGap: false,
          data: labels,
          axisLabel: { color: '#7b889e', fontSize: 11 },
          axisLine: { lineStyle: { color: '#263148' } },
          axisTick: { show: false },
        },
        yAxis: {
          type: 'value',
          axisLabel: { color: '#7b889e', fontSize: 11 },
          splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
        },
        series: items.map((item, index) => ({
          name: item.label || metricLabel(item.metric),
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 5,
          data: (item.by_weekday || []).map(num),
          lineStyle: { width: 1.8 },
          itemStyle: { color: colors[index % colors.length] },
        })),
      });
    }
  });
}

function metricsText(items) {
  return items.map((item) => item.label || metricLabel(item.metric)).join('、');
}

// ==================================================================
// 月度趋势
// ==================================================================

async function renderMonthly(ctx) {
  const host = state.hosts.body;
  const metric = tabMetricKey('monthly');
  const days = Math.max(28, state.days);
  const panel = panelHost('月度趋势', {
    subtitle: `${metricLabel(metric)}${unitSuffix(metricUnit(metric))} · ${cityText()} · 近 ${days} 天（按日汇总后按月聚合）`,
  });
  mount(host, panel.node);

  const key = `${metric}@${days}#${cityKey()}`;
  if (state.data.monthly && state.data.monthly.key === key) {
    renderMonthlyContent(panel, state.data.monthly.payload, ctx);
    return;
  }

  panelLoading(panel, '正在计算月度趋势…');
  let payload;
  try {
    payload = await api.insightsMonthlyTrend({ cities: requestedCities(), metric, days });
  } catch (error) {
    panelError(panel, error, () => renderMonthly(ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  state.data.monthly = { key, payload };
  renderMonthlyContent(panel, payload, ctx);
}

function renderMonthlyContent(panel, payload, ctx) {
  const months = payload.months || [];
  if (!months.length) {
    panelUnavailable(panel, '所选指标在窗口内没有可用的月度数据', ctx);
    return;
  }

  mergeMetric(payload.metric, payload.label, payload.unit);
  const unit = payload.unit || '';
  const chartId = nextId('profile-month');

  const values = months.map((item) => num(item.mean)).filter((value) => value !== null);
  const average = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  const highest = [...months].filter((item) => isNum(item.max)).sort((a, b) => b.max - a.max)[0] || null;
  const lowest = [...months].filter((item) => isNum(item.min)).sort((a, b) => a.min - b.min)[0] || null;
  const latest = months[months.length - 1];
  const previous = months.length > 1 ? months[months.length - 2] : null;
  const monthDelta = isNum(latest?.mean) && isNum(previous?.mean) ? latest.mean - previous.mean : null;

  const columns = [
    { key: 'month', title: '月份', sortable: true },
    { key: 'count', title: '样本天数', align: 'right', sortable: true, render: (row) => formatInt(row.count) },
    { key: 'mean', title: '月均值', align: 'right', sortable: true, render: (row) => fmt(row.mean, unit, 2) },
    { key: 'max', title: '月内最高', align: 'right', sortable: true, render: (row) => fmt(row.max, unit, 2) },
    { key: 'min', title: '月内最低', align: 'right', sortable: true, render: (row) => fmt(row.min, unit, 2) },
    {
      key: 'range',
      title: '月内极差',
      align: 'right',
      render: (row) => (isNum(row.max) && isNum(row.min) ? fmt(row.max - row.min, unit, 2) : '—'),
    },
  ];

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, [
      kpiCard({
        label: '最新月份均值',
        value: latest ? fmt(latest.mean, '', 2) : '—',
        unit,
        foot: latest ? `${latest.month} · 较上月 ${monthDelta === null ? '—' : `${monthDelta > 0 ? '+' : ''}${fmt(monthDelta, '', 2)}`}` : '—',
        tone: monthDelta === null ? '' : monthDelta > 0 ? 'bad' : 'ok',
      }),
      kpiCard({ label: '窗口平均', value: fmt(average, '', 2), unit, foot: `${formatInt(months.length)} 个月度桶` }),
      kpiCard({
        label: '最高月内极值',
        value: highest ? fmt(highest.max, '', 2) : '—',
        unit,
        foot: highest ? `${highest.month} · 月均值 ${fmt(highest.mean, '', 2)}` : '—',
      }),
      kpiCard({
        label: '最低月内极值',
        value: lowest ? fmt(lowest.min, '', 2) : '—',
        unit,
        foot: lowest ? `${lowest.month} · 月均值 ${fmt(lowest.mean, '', 2)}` : '—',
      }),
    ]),
    card('月度均值与月内极值', el('div.chart', { id: chartId, style: { height: '380px' } }), {
      subtitle: `实线为月均值的 3 个月移动平均，浅色带为月内最高 / 最低；单位：${unit || '无量纲'}。`,
    }),
    card('月度明细', sortedTableFor('monthly', columns, months, '暂无月度数据'), {
      subtitle: '按月份倒序排列；样本天数即该月参与聚合的日汇总记录数。',
    }),
    note(
      `月度聚合基于日汇总资产（窗口近 ${formatInt(payload.window_days || state.days)} 天，覆盖 ` +
      `${formatInt(payload.city_count)} 个城市）。月份不足时趋势线会显得跳变，属于样本长度问题而非治理成效突变。`,
    ),
  );

  window.requestAnimationFrame(() => {
    const target = document.getElementById(chartId);
    if (!target || !target.isConnected) return;
    const categories = months.map((item) => item.month);
    const means = months.map((item) => num(item.mean));
    const rolling = means.map((_, index) => {
      if (index < 2) return null;
      const window3 = means.slice(index - 2, index + 1);
      if (window3.some((value) => value === null)) return null;
      return Number((window3.reduce((sum, value) => sum + value, 0) / 3).toFixed(3));
    });

    renderChart(target, {
      backgroundColor: 'transparent',
      animation: false,
      textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
      legend: { top: 0, right: 8, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: '#a9b5c9', fontSize: 11.5 } },
      grid: { left: 10, right: 22, top: 36, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        backgroundColor: 'rgba(15, 22, 38, 0.96)',
        borderColor: '#33415c',
        borderWidth: 1,
        textStyle: { color: '#e6ebf4', fontSize: 12 },
        confine: true,
        formatter: (params) => {
          const item = months[params?.[0]?.dataIndex ?? 0];
          if (!item) return '';
          const suffix = unit ? ` ${unit}` : '';
          return [
            `${item.month}`,
            `月均值 ${fmt(item.mean, suffix, 2)}`,
            `月内最高 ${fmt(item.max, suffix, 2)}`,
            `月内最低 ${fmt(item.min, suffix, 2)}`,
            `样本天数 ${formatInt(item.count)}`,
          ].join('<br />');
        },
      },
      xAxis: {
        type: 'category',
        boundaryGap: false,
        data: categories,
        axisLabel: { color: '#7b889e', fontSize: 11, hideOverlap: true },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: unit || '',
        nameTextStyle: { color: '#7b889e', fontSize: 11 },
        axisLabel: { color: '#7b889e', fontSize: 11 },
        splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
      },
      series: [
        {
          name: '月内最高',
          type: 'line',
          smooth: false,
          symbol: 'none',
          data: months.map((item) => num(item.max)),
          lineStyle: { width: 1.1, type: 'dashed', color: '#f59e0b' },
          itemStyle: { color: '#f59e0b' },
        },
        {
          name: '月内最低',
          type: 'line',
          smooth: false,
          symbol: 'none',
          data: months.map((item) => num(item.min)),
          lineStyle: { width: 1.1, type: 'dashed', color: '#38bdf8' },
          itemStyle: { color: '#38bdf8' },
        },
        {
          name: '月均值',
          type: 'line',
          smooth: true,
          symbol: 'circle',
          symbolSize: 6,
          data: means,
          lineStyle: { width: 2, color: '#a78bfa' },
          itemStyle: { color: '#a78bfa' },
          areaStyle: { color: '#a78bfa', opacity: 0.12 },
        },
        {
          name: '3 月移动平均',
          type: 'line',
          smooth: true,
          symbol: 'none',
          data: rolling,
          lineStyle: { width: 2, color: '#22c55e' },
          itemStyle: { color: '#22c55e' },
        },
      ],
    });
  });
}
