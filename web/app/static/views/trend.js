/**
 * trend.js —— 历史趋势。
 *
 * 基于历史再分析（ERA5）归档资产的长期序列 `/api/env/trend`。
 * 归档数据依赖 `collect_weather_archive` 作业，未回补时接口返回 `available:false`，
 * 此时页面给出明确提示而不是空图表。
 */

import { api } from '/shared/api.js';
import { lineChart, renderChart } from '/shared/charts.js';
import {
  badge,
  button,
  card,
  el,
  emptyState,
  formatDataTime,
  formatFixed,
  formatInt,
  kpiCard,
  mount,
  note,
  select,
  table,
} from '/shared/ui.js';

/** 归档资产默认提供的核心气象要素（避免列出宽表中全部几十个字段）。 */
const TREND_METRICS = [
  'temperature_2m',
  'apparent_temperature',
  'relative_humidity_2m',
  'dew_point_2m',
  'precipitation',
  'wind_speed_10m',
  'pressure_msl',
  'cloud_cover',
  'shortwave_radiation',
  'pm2_5',
  'pm10',
  'european_aqi',
];

const DAY_OPTIONS = [
  { value: '90', label: '近 90 天' },
  { value: '180', label: '近 180 天' },
  { value: '365', label: '近 1 年' },
  { value: '730', label: '近 2 年' },
  { value: '1825', label: '近 5 年（上限）' },
];

let relays = [];
let state = { city: null, metric: 'temperature_2m', days: 365, window: 'day', hosts: null, cities: [], metrics: [] };

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在加载历史趋势…', hint: '读取城市清单与指标字典。' }));

  const [cityPayload, metricPayload] = await Promise.all([
    api.envCities().catch(() => ({ items: [] })),
    api.envMetrics().catch(() => ({ items: [] })),
  ]);

  const cities = cityPayload?.items || [];
  const catalog = new Map((metricPayload?.items || []).map((item) => [item.name, item]));
  const metrics = TREND_METRICS.map((name) => catalog.get(name)).filter(Boolean);

  if (!cities.length) {
    mount(container, card('历史趋势', emptyState({
      title: '暂无可用城市',
      hint: '城市主数据为空，请先在「数据刷新」页执行一次采集。',
      actions: [button('前往数据刷新', { kind: 'primary', onClick: () => ctx.navigate('ops') })],
    })));
    return;
  }

  if (!state.city || !cities.some((item) => item.slug === state.city)) state.city = cities[0].slug;
  if (metrics.length && !metrics.some((item) => item.name === state.metric)) state.metric = metrics[0].name;

  state.cities = cities;
  state.metrics = metrics.length ? metrics : [{ name: state.metric, label: state.metric, unit: '' }];
  state.hosts = { control: el('div'), body: el('div') };

  mount(container, state.hosts.control, state.hosts.body);
  renderControls(ctx);
  renderTrend(ctx);
}

export function destroy() {
  relays = [];
  if (state.hosts) state.hosts = null;
}

// ==================================================================
// 控件
// ==================================================================

function renderControls(ctx) {
  const citySelect = select(
    state.cities.map((city) => ({ value: city.slug, label: `${city.name_zh}（${city.slug}）` })),
    { value: state.city, onChange: (value) => { state.city = value; renderTrend(ctx); } },
  );

  const metricSelect = select(
    state.metrics.map((item) => ({ value: item.name, label: `${item.label}${item.unit ? `（${item.unit}）` : ''}` })),
    { value: state.metric, onChange: (value) => { state.metric = value; renderTrend(ctx); } },
  );

  const daySelect = select(DAY_OPTIONS, {
    value: String(state.days),
    onChange: (value) => { state.days = Number(value); renderTrend(ctx); },
  });

  const windowSelect = select(
    [
      { value: 'day', label: '按日聚合' },
      { value: 'month', label: '按月聚合' },
    ],
    { value: state.window, onChange: (value) => { state.window = value; renderTrend(ctx); } },
  );

  mount(
    state.hosts.control,
    card('趋势查询条件', el('div.toolbar', null, [
      el('div.field', { style: { minWidth: '210px' } }, [el('label', { text: '城市' }), citySelect]),
      el('div.field', { style: { minWidth: '230px' } }, [el('label', { text: '指标' }), metricSelect]),
      el('div.field', null, [el('label', { text: '时间跨度' }), daySelect]),
      el('div.field', null, [el('label', { text: '聚合窗口' }), windowSelect]),
      el('div.field', null, [
        el('label', { text: '操作' }),
        el('div.card-actions', null, [button('重新查询', { kind: 'primary', onClick: () => renderTrend(ctx) })]),
      ]),
    ]), {
      subtitle: '历史趋势来自 ERA5 再分析归档资产；数据时间为城市本地时间，已排除最近 6 天（再分析数据的发布延迟）。',
    }),
  );
}

// ==================================================================
// 主体
// ==================================================================

async function renderTrend(ctx) {
  const host = state.hosts.body;
  mount(host, emptyState({ title: '正在查询归档数据…', compact: true }));

  const chartBox = el('div.chart.chart-xl', { style: { height: '420px' } });

  let payload;
  try {
    payload = await api.envTrend({
      city: state.city,
      days: state.days,
      metric: state.metric,
      window: state.window,
    });
  } catch (error) {
    mount(host, card('历史趋势', emptyState({
      title: '归档数据查询失败',
      hint: error.message,
      actions: [button('重试', { kind: 'primary', onClick: () => renderTrend(ctx) })],
    })));
    return;
  }

  const points = (payload?.points || []).filter((point) => point && point.time);
  const unit = payload?.unit || '';
  const label = payload?.metric_label || state.metric;

  if (!payload?.available || !points.length) {
    mount(host, card(`${label} 历史趋势`, emptyState({
      title: '暂无历史归档数据',
      hint: payload?.note
        ? `${payload.note}。<br />也可在「数据刷新」页触发采集时勾选「包含历史归档回补」。`
        : '该城市没有可用的归档分区，请先在「数据刷新」页触发一次包含历史归档回补的采集。',
      actions: [
        button('前往数据刷新', { kind: 'primary', onClick: () => ctx.navigate('ops') }),
        button('查看实时监测', { onClick: () => ctx.navigate('monitor') }),
      ],
    })));
    return;
  }

  const values = points.map((point) => (typeof point.value === 'number' ? point.value : null)).filter((value) => value !== null);
  const avg = values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
  const max = values.length ? Math.max(...values) : null;
  const min = values.length ? Math.min(...values) : null;
  const samples = points.reduce((sum, point) => sum + (typeof point.samples === 'number' ? point.samples : 0), 0);

  mount(
    host,
    el('div.grid.grid-4', null, [
      kpiCard({ label: '区间均值', value: formatFixed(avg, 1), unit, foot: `${points.length} 个${state.window === 'month' ? '月' : '日'}桶` }),
      kpiCard({ label: '区间最高', value: formatFixed(max, 1), unit }),
      kpiCard({ label: '区间最低', value: formatFixed(min, 1), unit }),
      kpiCard({ label: '参与统计的原始记录', value: formatInt(samples), unit: '条', foot: '归档逐小时记录数' }),
    ]),
    card(`${label} 历史趋势`, chartBox, {
      subtitle: `${payload.city_name || state.city} · ${payload.range?.[0] || '—'} 至 ${payload.range?.[1] || '—'} · ${state.window === 'month' ? '按月均值' : '按日均值'} · 单位：${unit || '无量纲'}`,
      actions: [badge(`近 ${state.days} 天`, 'badge-accent')],
    }),
    note('归档数据为再分析产品（ERA5），与实况观测存在系统性差异，适合观察长期气候趋势而非逐小时对比。'),
    card('数据点明细', table(
      [
        { key: 'time', title: state.window === 'month' ? '月份' : '日期', render: (row) => formatDataTime(row.time) },
        { key: 'value', title: `均值${unit ? `（${unit}）` : ''}`, align: 'right', render: (row) => formatFixed(row.value, 2) },
        { key: 'samples', title: '样本数', align: 'right', render: (row) => formatInt(row.samples) },
      ],
      [...points].reverse().slice(0, 60),
      { empty: '暂无数据点' },
    ), { subtitle: '按时间倒序展示最近 60 个数据点' }),
  );

  window.requestAnimationFrame(() => {
    if (!chartBox.isConnected) return;
    const chart = renderChart(chartBox, lineChart({
      categories: points.map((point) => formatDataTime(point.time)),
      zoom: true,
      series: [{ name: `${label}${unit ? `（${unit}）` : ''}`, data: points.map((point) => (typeof point.value === 'number' ? point.value : null)), color: '#38bdf8', area: true }],
    }));
    if (chart) relays.push(chart);
  });
}
