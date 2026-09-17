/**
 * compliance.js —— 达标考核。
 *
 * 把逐小时观测转成"可考核的口径"：优良天比例、等级天数分布、超标小时与目标完成度，
 * 并给出环比（与上一个等长周期）与同比（依赖历史归档，数据不足时只做说明）。
 *
 * 数据来源：
 *  * `/api/insights/compliance` —— 逐城市考核指标 + 全平台汇总；
 *  * `/api/insights/comparison` —— 环比与同比；
 *  * `/api/insights/compliance/export.csv` —— CSV 导出（浏览器直接下载）。
 *
 * 口径与默认值：优良天上限默认 40（对应 AQI 等级"良"的上界），目标比例默认 80%。
 * 等级天数按中国 GB 3095 表述的六级分类统计。
 */

import { api } from '/shared/api.js';
import { renderChart } from '/shared/charts.js';
import {
  badge,
  button,
  card,
  chip,
  dash,
  el,
  emptyState,
  formatDate,
  formatInt,
  formatPercent,
  kpiCard,
  mount,
  note,
  progressBar,
  select,
  table,
} from '/shared/ui.js';

const WINDOW_OPTIONS = [
  { value: '30', label: '近 30 天' },
  { value: '60', label: '近 60 天' },
  { value: '90', label: '近 90 天（默认）' },
  { value: '180', label: '近 180 天' },
  { value: '365', label: '近 365 天' },
];

/** 环比可用的日汇总指标（与后端 environment.daily 资产字段一致）。 */
const COMPARISON_METRICS = [
  { value: 'aqi_avg', label: '日均 AQI' },
  { value: 'aqi_max', label: '日最大 AQI' },
  { value: 'pm2_5_avg', label: 'PM2.5 日均' },
  { value: 'pm10_avg', label: 'PM10 日均' },
];

/** 空气质量等级与国标配色（与后端 AQILevel 定义一致）。 */
const LEVEL_ORDER = ['优', '良', '轻度污染', '中度污染', '重度污染', '严重污染'];
const LEVEL_COLOR = {
  优: '#00e400',
  良: '#ffff00',
  轻度污染: '#ff7e00',
  中度污染: '#ff0000',
  重度污染: '#99004c',
  严重污染: '#7e0023',
};

const TREND_TONE = {
  改善: '#22c55e',
  转差: '#ef4444',
  持平: '#64748b',
  数据不足: '#475569',
};

const state = {
  days: 90,
  goodCeiling: 40,
  targetRatio: 80,
  comparisonMetric: 'aqi_avg',
  cities: [],
  citySelection: new Set(),
  payload: null,
  comparison: null,
  hosts: null,
  sort: { key: 'good_day_ratio', dir: 'desc' },
};

let seq = 0;
let ctxRef = null;

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

function fmt(value, digits = 2) {
  if (!isNum(value)) return dash(value);
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${(value / 1e8).toFixed(2)}亿`;
  if (abs >= 1e5) return `${(value / 1e4).toFixed(1)}万`;
  if (abs >= 1000) return value.toFixed(0);
  return value.toFixed(digits);
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

function levelColor(level) {
  return LEVEL_COLOR[level] || '#64748b';
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
    title: '数据不足，暂无法考核',
    hint: `${message || '窗口内没有可用的日汇总数据。'}<br />请前往「数据刷新」触发一次数据采集，采集完成后再回到本页。`,
    compact: true,
    actions: [
      button('前往数据刷新', { kind: 'primary', onClick: () => ctx && ctx.navigate('ops') }),
      button('刷新本页', { onClick: () => ctx && ctx.refresh() }),
    ],
  }));
}

function numberField(label, { value, min, max, step = 1, onCommit }) {
  const input = el('input.input', {
    type: 'number',
    value: String(value),
    min: String(min),
    max: String(max),
    step: String(step),
  });
  const commit = () => {
    const parsed = Number(input.value);
    if (!Number.isFinite(parsed)) {
      input.value = String(value);
      return;
    }
    const clamped = Math.min(max, Math.max(min, parsed));
    input.value = String(clamped);
    onCommit(clamped);
  };
  input.addEventListener('change', commit);
  input.addEventListener('blur', commit);
  return el('div.field', null, [el('label', { text: label }), input]);
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

/** 排序只重绘考核面板，环比面板保持不变。 */
function makeSort(nextKey) {
  if (state.sort.key === nextKey) {
    state.sort = { key: nextKey, dir: state.sort.dir === 'desc' ? 'asc' : 'desc' };
  } else {
    state.sort = { key: nextKey, dir: 'desc' };
  }
  renderPayload(ctxRef);
}

/** 导出链接：CSV 由后端生成，浏览器直接下载。 */
function exportUrl() {
  const params = new URLSearchParams();
  const cities = requestedCities();
  if (cities) params.set('cities', cities.join(','));
  params.set('days', String(state.days));
  // 导出接口当前只声明 cities / days；多传的参数会被后端忽略，便于未来扩展时自动生效
  params.set('good_ceiling', String(state.goodCeiling));
  params.set('target_ratio', String(state.targetRatio));
  return `/api/insights/compliance/export.csv?${params.toString()}`;
}

// ==================================================================
// 生命周期
// ==================================================================

export async function render(container, ctx) {
  ctxRef = ctx;
  mount(container, emptyState({ title: '正在读取考核口径…', hint: '加载城市主数据。', compact: true }));

  const cityPayload = await api.envCities().catch(() => ({ items: [] }));
  state.cities = cityPayload?.items || [];

  state.hosts = { controls: el('div'), body: el('div'), compliance: null, comparison: null };
  mount(container, state.hosts.controls, state.hosts.body);

  renderControls(ctx);
  await loadCompliance(ctx);
}

export function destroy() {
  ctxRef = null;
  state.hosts = null;
  state.payload = null;
  state.comparison = null;
}

// ==================================================================
// 控件
// ==================================================================

function renderControls(ctx) {
  const daySelect = select(WINDOW_OPTIONS, {
    value: String(state.days),
    onChange: (value) => {
      state.days = Number(value);
      state.payload = null;
      state.comparison = null;
      renderControls(ctx);
      loadCompliance(ctx);
    },
  });

  const comparisonSelect = select(COMPARISON_METRICS, {
    value: state.comparisonMetric,
    onChange: (value) => {
      state.comparisonMetric = value;
      state.comparison = null;
      loadCompliance(ctx);
    },
  });

  const cityChips = state.cities.map((city) =>
    chip(city.name_zh, null, state.citySelection.has(city.slug), () => {
      if (state.citySelection.has(city.slug)) state.citySelection.delete(city.slug);
      else state.citySelection.add(city.slug);
      state.payload = null;
      state.comparison = null;
      renderControls(ctx);
      loadCompliance(ctx);
    }),
  );

  mount(
    state.hosts.controls,
    card('考核参数', el('div', null, [
      el('div.toolbar', null, [
        el('div.field', null, [el('label', { text: '周期' }), daySelect]),
        numberField('优良上限（AQI）', {
          value: state.goodCeiling,
          min: 1,
          max: 500,
          step: 1,
          onCommit: (value) => {
            state.goodCeiling = value;
            state.payload = null;
            loadCompliance(ctx);
          },
        }),
        numberField('目标比例（%）', {
          value: state.targetRatio,
          min: 0,
          max: 100,
          step: 1,
          onCommit: (value) => {
            state.targetRatio = value;
            state.payload = null;
            loadCompliance(ctx);
          },
        }),
        el('div.field', { style: { minWidth: '200px' } }, [el('label', { text: '环比对比指标' }), comparisonSelect]),
        el('div.field', null, [
          el('label', { text: '操作' }),
          el('div.card-actions', null, [
            button('重新考核', {
              kind: 'primary',
              onClick: () => {
                state.payload = null;
                state.comparison = null;
                loadCompliance(ctx);
              },
            }),
            el('a.btn', { href: exportUrl(), download: 'atmos_compliance.csv', text: '导出 CSV' }),
            button('前往数据刷新', { onClick: () => ctx.navigate('ops') }),
          ]),
        ]),
      ]),
      el('div.field', { style: { marginTop: '12px' } }, [
        el('label', { text: `城市（不选表示全部；当前：${cityText()}）` }),
        el('div.chip-row', null, cityChips.length ? cityChips : [el('span.text-mute', { text: '暂无可用城市' })]),
      ]),
    ]), {
      subtitle:
        '优良天以日均 AQI ≤ 优良上限判定（考核通行做法）；等级天数分布同样按日均 AQI 归档。' +
        '日期为城市本地日期，页面不做时区换算。',
      actions: [badge(`近 ${state.days} 天`, 'badge-accent')],
    }),
  );
}

// ==================================================================
// 主体
// ==================================================================

async function loadCompliance(ctx) {
  if (!state.hosts) return;
  const compliancePanel = panelHost('达标考核报表', {
    subtitle: `近 ${state.days} 天 · 优良上限 ${state.goodCeiling} · 目标比例 ${state.targetRatio}% · ${cityText()}`,
  });
  const comparisonPanel = panelHost('环比与同比', {
    subtitle: `与上一个等长周期（近 ${state.days} 天）比较 · 指标：${COMPARISON_METRICS.find((item) => item.value === state.comparisonMetric)?.label || state.comparisonMetric}`,
  });

  state.hosts.compliance = compliancePanel;
  state.hosts.comparison = comparisonPanel;

  mount(state.hosts.body, el('div', { style: { display: 'flex', flexDirection: 'column', gap: '16px' } }, [
    compliancePanel.node,
    comparisonPanel.node,
  ]));

  panelLoading(compliancePanel, '正在生成考核报表…');

  const params = {
    cities: requestedCities(),
    days: state.days,
    goodCeiling: state.goodCeiling,
    targetRatio: state.targetRatio,
  };

  const [complianceResult, comparisonResult] = await Promise.allSettled([
    api.insightsCompliance(params),
    api.insightsComparison({
      cities: requestedCities(),
      days: Math.min(180, Math.max(7, state.days)),
      metric: state.comparisonMetric,
    }),
  ]);

  if (complianceResult.status === 'rejected') {
    panelError(compliancePanel, complianceResult.reason, () => loadCompliance(ctx));
  } else if (!complianceResult.value || complianceResult.value.available === false) {
    panelUnavailable(compliancePanel, complianceResult.value?.message, ctx);
  } else {
    state.payload = complianceResult.value;
    renderPayload(ctx);
  }

  if (comparisonResult.status === 'rejected') {
    panelError(comparisonPanel, comparisonResult.reason, () => loadCompliance(ctx));
  } else if (!comparisonResult.value || comparisonResult.value.available === false) {
    panelUnavailable(comparisonPanel, comparisonResult.value?.message, ctx);
  } else {
    state.comparison = comparisonResult.value;
    renderComparison(comparisonPanel, comparisonResult.value);
  }
}

function renderPayload(ctx) {
  const panel = state.hosts?.compliance;
  if (!panel || !state.payload) return;
  renderCompliance(panel, state.payload, ctx);
}

// ==================================================================
// 考核报表
// ==================================================================

function renderCompliance(panel, payload, ctx) {
  const totals = payload.totals || {};
  const cities = payload.cities || [];
  const chartId = nextId('compliance-level');

  const targetsMet = cities.filter((row) => row.target_met === true).length;
  const targetsMissed = cities.filter((row) => row.target_met === false).length;

  const columns = [
    { key: 'city_name', title: '城市', sortable: true },
    { key: 'days', title: '统计天数', align: 'right', sortable: true, render: (row) => formatInt(row.days) },
    { key: 'good_days', title: '优良天', align: 'right', sortable: true, render: (row) => formatInt(row.good_days) },
    {
      key: 'good_day_ratio',
      title: '优良天比例',
      align: 'right',
      sortable: true,
      render: (row) => el('span', {
        class: row.target_met === true ? 'text-ok' : row.target_met === false ? 'text-bad' : 'text-mute',
        text: formatPercent(row.good_day_ratio, 2),
      }),
    },
    { key: 'target_ratio', title: '目标', align: 'right', render: (row) => formatPercent(row.target_ratio, 0) },
    {
      key: 'target_met',
      title: '达标情况',
      render: (row) => {
        if (row.target_met === null || row.target_met === undefined) return badge('数据不足', 'badge-mute');
        return badge(row.target_met ? '达标' : '未达标', row.target_met ? 'badge-ok' : 'badge-bad');
      },
    },
    {
      key: 'gap_to_target',
      title: '距目标',
      align: 'right',
      sortable: true,
      render: (row) => {
        if (!isNum(row.gap_to_target)) return '—';
        const gap = row.gap_to_target;
        const text = gap >= 0 ? `+${gap.toFixed(2)} 个百分点` : `${gap.toFixed(2)} 个百分点`;
        return el('span', { class: gap >= 0 ? 'text-ok' : 'text-bad', text });
      },
    },
    {
      key: 'progress',
      title: '目标完成度',
      width: '180px',
      render: (row) => {
        const ratio = isNum(row.good_day_ratio) ? row.good_day_ratio : 0;
        let footText = '无有效天数';
        if (isNum(row.good_day_ratio)) {
          footText = row.target_met
            ? `已达 ${formatPercent(ratio, 1)}，超出 ${Math.abs(row.gap_to_target).toFixed(2)} 个百分点`
            : `当前 ${formatPercent(ratio, 1)}，还差 ${Math.abs(row.gap_to_target).toFixed(2)} 个百分点`;
        }
        return el('div', { style: { display: 'flex', flexDirection: 'column', gap: '4px', minWidth: '150px' } }, [
          progressBar(ratio),
          el('span.text-mute', { style: { fontSize: '11px' }, text: footText }),
        ]);
      },
    },
    { key: 'aqi_avg', title: '日均 AQI', align: 'right', sortable: true, render: (row) => fmt(row.aqi_avg, 1) },
    { key: 'aqi_peak', title: 'AQI 峰值', align: 'right', sortable: true, render: (row) => fmt(row.aqi_peak, 1) },
    { key: 'exceed_hours', title: '超标小时', align: 'right', sortable: true, render: (row) => formatInt(row.exceed_hours) },
    {
      key: 'exceed_hour_ratio',
      title: '超标小时占比',
      align: 'right',
      sortable: true,
      render: (row) => el('span', {
        class: isNum(row.exceed_hour_ratio) && row.exceed_hour_ratio > 10 ? 'text-warn' : '',
        text: formatPercent(row.exceed_hour_ratio, 2),
      }),
    },
    { key: 'obs_hours', title: '有效观测小时', align: 'right', sortable: true, render: (row) => formatInt(row.obs_hours) },
  ];

  const keys = new Set(columns.map((column) => column.key));
  const sortKey = keys.has(state.sort.key) ? state.sort.key : null;

  const levelLegend = LEVEL_ORDER.map((level) =>
    el('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: '#a9b5c9' } }, [
      el('span', { style: { width: '10px', height: '10px', borderRadius: '3px', background: levelColor(level) } }),
      el('span', { text: level }),
    ]),
  );

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, [
      kpiCard({
        label: '平台优良天比例',
        value: formatPercent(totals.good_day_ratio, 2),
        foot: `目标 ${formatPercent(payload.target_ratio, 0)} · ${isNum(totals.good_day_ratio) && totals.good_day_ratio >= payload.target_ratio ? '整体达标' : '整体未达标'}`,
        tone: isNum(totals.good_day_ratio) && totals.good_day_ratio >= payload.target_ratio ? 'ok' : 'warn',
      }),
      kpiCard({
        label: '平台日均 AQI',
        value: fmt(totals.aqi_avg, 1),
        foot: `统计站日 ${formatInt(totals.station_days)} 天 · 优良天 ${formatInt(totals.good_days)} 天`,
      }),
      kpiCard({
        label: '参与考核城市',
        value: formatInt(totals.city_count),
        unit: '个',
        foot: `优良上限 ${fmt(payload.good_ceiling, 0)}（${payload.good_ceiling_label || '—'}）`,
      }),
      kpiCard({
        label: '优良天比例最高',
        value: totals.best_city || '—',
        foot: cities.length ? `优良天比例 ${formatPercent(cities[0].good_day_ratio, 2)}` : '—',
        tone: 'ok',
      }),
      kpiCard({
        label: '优良天比例最低',
        value: totals.worst_city || '—',
        foot: cities.length ? `优良天比例 ${formatPercent(cities[cities.length - 1].good_day_ratio, 2)}` : '—',
        tone: 'bad',
      }),
      kpiCard({
        label: '目标完成情况',
        value: `${formatInt(targetsMet)} / ${formatInt(cities.length)}`,
        unit: '个城市达标',
        foot: targetsMissed ? `${formatInt(targetsMissed)} 个城市未达标，需重点关注` : '全部城市均已完成目标',
        tone: targetsMissed ? 'warn' : 'ok',
      }),
    ]),
    card('等级天数分布（堆叠）', el('div.chart', { id: chartId, style: { height: `${Math.max(320, cities.length * 26 + 120)}px` } }), {
      subtitle: `按日均 AQI 归档的六级天数；统计周期 ${formatDate(payload.period?.start)} 至 ${formatDate(payload.period?.end)}。`,
      actions: levelLegend,
    }),
    card('逐城市考核明细', table(columns, sortedRows(cities, sortKey, state.sort.dir), {
      empty: '窗口内没有可考核的城市',
      sortKey,
      sortDir: state.sort.dir,
      onSort: makeSort,
      rowKey: (row) => row.city_slug,
    }), {
      subtitle: '点击表头排序；进度条展示该城市优良天比例相对目标的比例（达到目标即满格）。',
    }),
    note(
      `口径说明：优良天 = 日均 AQI ≤ ${fmt(payload.good_ceiling, 0)}；目标比例 ${formatPercent(payload.target_ratio, 0)}。` +
      '超标小时占比 = 超标小时数 / 有效观测小时数（阈值固定为 AQI 优良上限，与考核目标比例相互独立）。' +
      '导出 CSV 使用后端默认口径（优良上限 40、目标比例 80%），与页面当前参数可能不同，请以文件内容为准。',
    ),
  );

  window.requestAnimationFrame(() => {
    const target = document.getElementById(chartId);
    if (!target || !target.isConnected) return;
    renderChart(target, {
      backgroundColor: 'transparent',
      animation: false,
      textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
      legend: { top: 0, right: 8, icon: 'roundRect', itemWidth: 10, itemHeight: 10, itemGap: 12, textStyle: { color: '#a9b5c9', fontSize: 11.5 } },
      grid: { left: 10, right: 24, top: 36, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: 'rgba(15, 22, 38, 0.96)',
        borderColor: '#33415c',
        borderWidth: 1,
        textStyle: { color: '#e6ebf4', fontSize: 12 },
        confine: true,
        formatter: (params) => {
          const row = cities[params?.[0]?.dataIndex];
          if (!row) return '';
          const lines = [`${row.city_name}（共 ${formatInt(row.days)} 天）`];
          for (const level of LEVEL_ORDER) {
            const value = row.level_distribution?.[level] || 0;
            if (value) lines.push(`${level} ${formatInt(value)} 天`);
          }
          lines.push(`优良天比例 ${formatPercent(row.good_day_ratio, 2)}`);
          return lines.join('<br />');
        },
      },
      xAxis: {
        type: 'category',
        data: cities.map((row) => row.city_name),
        axisLabel: { color: '#7b889e', fontSize: 11, interval: 0, rotate: 30, hideOverlap: false },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: '天数',
        nameTextStyle: { color: '#7b889e', fontSize: 11 },
        axisLabel: { color: '#7b889e', fontSize: 11 },
        splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
      },
      series: LEVEL_ORDER.map((level) => ({
        name: level,
        type: 'bar',
        stack: 'level',
        barMaxWidth: 26,
        data: cities.map((row) => row.level_distribution?.[level] || 0),
        itemStyle: { color: levelColor(level) },
        label: { show: false },
      })),
    });
  });
}

// ==================================================================
// 环比与同比
// ==================================================================

function renderComparison(panel, payload) {
  const rows = payload.cities || [];
  const yoy = payload.year_over_year;
  const unit = payload.unit || '';
  const metricText = payload.metric_label || payload.metric;

  if (!rows.length) {
    panelUnavailable(panel, '上一个等长周期没有可比数据', ctxRef);
    return;
  }

  const chartId = nextId('compliance-comparison');
  const improve = rows.filter((row) => row.trend === '改善').length;
  const worse = rows.filter((row) => row.trend === '转差').length;
  const flat = rows.filter((row) => row.trend === '持平').length;
  const missing = rows.filter((row) => row.trend === '数据不足').length;

  const columns = [
    { key: 'city_name', title: '城市' },
    { key: 'current', title: `本期均值${unit ? `（${unit}）` : ''}`, align: 'right', render: (row) => fmt(row.current, 2) },
    { key: 'previous', title: '上期均值', align: 'right', render: (row) => fmt(row.previous, 2) },
    {
      key: 'change',
      title: '变化量',
      align: 'right',
      render: (row) => el('span', {
        class: isNum(row.change) ? (row.change > 0 ? 'text-bad' : row.change < 0 ? 'text-ok' : 'text-dim') : 'text-mute',
        text: fmt(row.change, 3),
      }),
    },
    {
      key: 'change_ratio',
      title: '环比变化',
      align: 'right',
      render: (row) => el('span', {
        class: isNum(row.change_ratio) ? (row.change_ratio > 0 ? 'text-bad' : row.change_ratio < 0 ? 'text-ok' : 'text-dim') : 'text-mute',
        text: isNum(row.change_ratio) ? formatPercent(row.change_ratio, 2) : '—',
      }),
    },
    {
      key: 'trend',
      title: '趋势',
      render: (row) => badge(
        row.trend || '—',
        row.trend === '改善' ? 'badge-ok' : row.trend === '转差' ? 'badge-bad' : row.trend === '持平' ? 'badge-mute' : 'badge-info',
      ),
    },
    { key: 'current_days', title: '本期天数', align: 'right', render: (row) => formatInt(row.current_days) },
    { key: 'previous_days', title: '上期天数', align: 'right', render: (row) => formatInt(row.previous_days) },
  ];

  // 同比数据不足属于正常情况（受历史归档长度限制），只做说明，不当作错误
  const yearOverYearBlock = yoy?.available
    ? card('同比（历史归档）', table(
        [
          { key: 'city_name', title: '城市' },
          { key: 'current', title: `${yoy.current_year} 年同期`, align: 'right', render: (row) => fmt(row.current, 2) },
          { key: 'previous', title: `${yoy.previous_year} 年同期`, align: 'right', render: (row) => fmt(row.previous, 2) },
          { key: 'change', title: '变化量', align: 'right', render: (row) => fmt(row.change, 3) },
          {
            key: 'change_ratio',
            title: '同比变化',
            align: 'right',
            render: (row) => el('span', {
              class: isNum(row.change_ratio) ? (row.change_ratio > 0 ? 'text-bad' : 'text-ok') : 'text-mute',
              text: isNum(row.change_ratio) ? formatPercent(row.change_ratio, 2) : '—',
            }),
          },
          { key: 'current_days', title: '本期天数', align: 'right', render: (row) => formatInt(row.current_days) },
          { key: 'previous_days', title: '去年同期天数', align: 'right', render: (row) => formatInt(row.previous_days) },
        ],
        yoy.cities || [],
        { empty: '暂无可比城市' },
      ), { subtitle: yoy.note || '同比基于历史归档（ERA5）' })
    : card('同比（历史归档）', el('div.banner.banner-info', { style: { marginBottom: '0' } }, [
        el('div.banner-body', null, [
          el('div.banner-title', { text: '同比暂不可用（属正常情况，不影响环比结论）' }),
          el('div', { text: yoy?.message || '历史归档数据不足，无法进行同比。' }),
        ]),
      ]), {
      subtitle: '同比依赖覆盖去年同期（约 400 天）的历史归档；归档不含空气质量要素，因此以气温等气象指标替代。',
    });

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, [
      kpiCard({
        label: '本期区间',
        value: `${formatDate(payload.current_period?.start)} ~ ${formatDate(payload.current_period?.end)}`,
        foot: `对比区间 ${formatDate(payload.previous_period?.start)} ~ ${formatDate(payload.previous_period?.end)}`,
      }),
      kpiCard({ label: '空气质量改善城市', value: formatInt(improve), unit: '个', foot: '环比变化为负', tone: 'ok' }),
      kpiCard({ label: '空气质量转差城市', value: formatInt(worse), unit: '个', foot: '环比变化为正', tone: 'bad' }),
      kpiCard({ label: '持平 / 数据不足', value: `${formatInt(flat)} / ${formatInt(missing)}`, foot: '城市数量（持平 / 数据不足）' }),
      kpiCard({ label: '对比指标', value: metricText, foot: `单位：${unit || '无量纲'}` }),
      kpiCard({
        label: '同比可用性',
        value: yoy?.available ? '可用' : '不可用',
        foot: yoy?.available ? `对比 ${yoy.previous_year} 年同期` : '受历史归档长度限制',
        tone: yoy?.available ? 'ok' : '',
      }),
    ]),
    card(`${metricText} 环比变化`, el('div.chart', { id: chartId, style: { height: `${Math.max(300, rows.length * 24 + 120)}px` } }), {
      subtitle: '柱高为环比变化比例（%），颜色对应趋势：绿色为改善、红色为转差、灰色为持平、深灰为数据不足。',
    }),
    card('环比明细', table(columns, rows, { empty: '暂无环比数据' }), {
      subtitle: '环比 =（本期均值 − 上期均值）/ 上期均值；变化量为本期与上期的绝对差值。',
    }),
    yearOverYearBlock,
    note('环比受季节性与天气年际波动影响，单个周期的一升一降不足以判断治理成效；建议结合多个周期与同比一并观察。'),
  );

  window.requestAnimationFrame(() => {
    const target = document.getElementById(chartId);
    if (!target || !target.isConnected) return;
    renderChart(target, {
      backgroundColor: 'transparent',
      animation: false,
      textStyle: { color: '#a9b5c9', fontFamily: 'inherit', fontSize: 12 },
      grid: { left: 10, right: 26, top: 24, bottom: 8, containLabel: true },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        backgroundColor: 'rgba(15, 22, 38, 0.96)',
        borderColor: '#33415c',
        borderWidth: 1,
        textStyle: { color: '#e6ebf4', fontSize: 12 },
        confine: true,
        formatter: (params) => {
          const row = rows[params?.[0]?.dataIndex];
          if (!row) return '';
          return [
            `${row.city_name}（${row.trend || '—'}）`,
            `本期 ${fmt(row.current, 2)}${unit ? ` ${unit}` : ''} · 上期 ${fmt(row.previous, 2)}${unit ? ` ${unit}` : ''}`,
            `变化量 ${fmt(row.change, 3)}`,
            `环比 ${isNum(row.change_ratio) ? formatPercent(row.change_ratio, 2) : '—'}`,
          ].join('<br />');
        },
      },
      xAxis: {
        type: 'category',
        data: rows.map((row) => row.city_name),
        axisLabel: { color: '#7b889e', fontSize: 11, interval: 0, rotate: 30, hideOverlap: false },
        axisLine: { lineStyle: { color: '#263148' } },
        axisTick: { show: false },
      },
      yAxis: {
        type: 'value',
        name: '环比变化 %',
        nameTextStyle: { color: '#7b889e', fontSize: 11 },
        axisLabel: { color: '#7b889e', fontSize: 11 },
        splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
      },
      series: [
        {
          name: '环比变化',
          type: 'bar',
          barMaxWidth: 26,
          data: rows.map((row) => ({
            value: num(row.change_ratio),
            itemStyle: {
              color: TREND_TONE[row.trend] || '#64748b',
              borderRadius: isNum(row.change_ratio) && row.change_ratio >= 0 ? [3, 3, 0, 0] : [0, 0, 3, 3],
            },
          })),
        },
      ],
    });
  });
}
