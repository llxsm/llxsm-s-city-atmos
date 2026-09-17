/**
 * compare.js —— 多城市对比。
 *
 * 从 `/api/env/metrics` 中筛选可比数值指标，支持按小时或按日聚合，
 * 输出多序列折线图与统计对比表（均值 / 最大 / 最小 / P95 / 样本数）。
 */

import { api } from '/shared/api.js';
import { lineChart, PALETTE, renderChart } from '/shared/charts.js';
import {
  badge,
  button,
  card,
  checkbox,
  collectHint,
  el,
  emptyState,
  formatFixed,
  formatInt,
  mount,
  note,
  select,
  shortDataTime,
  table,
} from '/shared/ui.js';

/** 与接口限制保持一致：hourly 窗口取近 7 天，daily 取近 30 天。 */
const AGGREGATE_HOURS = { hourly: 168, daily: 720 };

let relays = [];
let state = {
  selected: [],
  metric: 'european_aqi',
  aggregate: 'hourly',
  initialized: false,
  /** 视图私有上下文，避免在事件里反查 DOM */
  hosts: null,
  cities: [],
  metrics: [],
};

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在加载对比数据…', hint: '读取城市清单与可比指标目录。' }));

  const [cityPayload, metricPayload, ranking] = await Promise.all([
    api.envCities().catch(() => ({ items: [] })),
    api.envMetrics().catch(() => ({ items: [], comparable: [] })),
    api.envRanking(15).catch(() => null),
  ]);

  const cities = cityPayload?.items || [];
  const comparableNames = new Set(metricPayload?.comparable || []);
  const metrics = (metricPayload?.items || []).filter(
    (item) => item.comparable && (comparableNames.size === 0 || comparableNames.has(item.name)),
  );

  if (!cities.length || !metrics.length) {
    mount(
      container,
      card('多城市对比', emptyState({
        title: '暂无可对比的数据',
        hint: collectHint('对比需要至少两个城市的融合宽表数据，请先在「数据刷新」页触发采集管道。'),
        actions: [button('前往数据刷新', { kind: 'primary', onClick: () => ctx.navigate('ops') })],
      })),
    );
    return;
  }

  if (!state.initialized) {
    // 默认选择 AQI 最差的前若干城市，便于直接观察差异
    const preferred = (ranking?.worst_air || []).slice(0, 4).map((item) => item.city_slug);
    state.selected = (preferred.length >= 2 ? preferred : cities.slice(0, 4).map((item) => item.slug)).slice(0, 8);
    state.initialized = true;
  }
  if (!metrics.some((item) => item.name === state.metric)) state.metric = metrics[0].name;

  state.cities = cities;
  state.metrics = metrics;
  state.ctx = ctx;
  state.hosts = {
    picker: el('div'),
    chart: el('div'),
    stat: el('div'),
  };

  mount(container, state.hosts.picker, state.hosts.chart, state.hosts.stat);

  renderPicker();
  renderChartArea();
}

export function destroy() {
  relays = [];
  if (state.hosts) state.hosts = null;
}

// ==================================================================
// 统一重绘入口
// ==================================================================

function redraw({ picker = false } = {}) {
  if (picker) renderPicker();
  renderChartArea();
}

// ==================================================================
// 选择器
// ==================================================================

function renderPicker() {
  const { picker: host } = state.hosts;
  const { cities, metrics } = state;

  const metricSelect = select(
    metrics.map((item) => ({ value: item.name, label: `${item.label}${item.unit ? `（${item.unit}）` : ''}` })),
    { value: state.metric, onChange: (value) => { state.metric = value; redraw(); } },
  );

  const aggregateSelect = select(
    [
      { value: 'hourly', label: '逐小时（近 7 天）' },
      { value: 'daily', label: '按日聚合（近 30 天）' },
    ],
    { value: state.aggregate, onChange: (value) => { state.aggregate = value; redraw(); } },
  );

  const quickActions = [
    button('选择最差空气城市', {
      size: 'sm',
      onClick: () => {
        state.selected = cities.slice(0, 6).map((item) => item.slug);
        redraw({ picker: true });
      },
    }),
    button('清空', {
      size: 'sm',
      onClick: () => {
        state.selected = [];
        redraw({ picker: true });
      },
    }),
  ];

  const toggles = cities.map((city) =>
    checkbox({
      label: city.name_zh || city.slug,
      checked: state.selected.includes(city.slug),
      title: `时区：${city.timezone || '未知'}`,
      onChange: (checked) => {
        if (checked) {
          if (!state.selected.includes(city.slug)) {
            if (state.selected.length >= 8) {
              state.selected.shift();
            }
            state.selected.push(city.slug);
          }
        } else {
          state.selected = state.selected.filter((slug) => slug !== city.slug);
        }
        redraw({ picker: true });
      },
    }),
  );

  mount(
    host,
    card('对比设置', el('div', null, [
      el('div.toolbar', null, [
        el('div.field', { style: { minWidth: '260px' } }, [
          el('label', { text: '对比指标（仅列出可比较的数值指标）' }),
          metricSelect,
        ]),
        el('div.field', null, [el('label', { text: '时间粒度' }), aggregateSelect]),
        el('div.field', null, [el('label', { text: '快捷选择' }), el('div.card-actions', null, quickActions)]),
      ]),
      el('div.section-title', { text: `参与对比的城市（已选 ${state.selected.length} 个，最多 8 个）`, style: { marginTop: '14px' } }),
      el('div.chip-row', null, toggles),
    ]), {
      subtitle: '对比曲线的时间戳为各城市本地时间；跨时区城市的时间轴不完全对齐，请谨慎比较绝对时刻。',
    }),
  );
}

// ==================================================================
// 图表与统计
// ==================================================================

async function renderChartArea() {
  const { chart: chartHost, stat: statHost } = state.hosts;
  const metricMeta = state.metrics.find((item) => item.name === state.metric) || { label: state.metric, unit: '' };

  if (state.selected.length < 2) {
    mount(
      chartHost,
      card('多城市曲线', emptyState({
        title: '请至少选择 2 个城市',
        hint: '接口要求 <code>cities</code> 至少包含 2 个城市 slug。',
        compact: true,
      })),
    );
    statHost.textContent = '';
    return;
  }

  const chartBox = el('div.chart', { style: { height: '380px' } });

  mount(
    chartHost,
    card(`${metricMeta.label} 对比`, chartBox, {
      subtitle: `${state.aggregate === 'daily' ? '按日聚合均值' : '逐小时原始值'} · 单位：${metricMeta.unit || '无量纲'}`,
      actions: [badge(`${state.selected.length} 个城市`, 'badge-accent')],
    }),
  );

  let payload;
  try {
    payload = await api.envCompare({
      cities: state.selected,
      metric: state.metric,
      hours: AGGREGATE_HOURS[state.aggregate] || 168,
      aggregate: state.aggregate,
    });
  } catch (error) {
    mount(chartBox, emptyState({ title: '对比数据读取失败', hint: error.message, compact: true }));
    statHost.textContent = '';
    return;
  }

  const series = payload?.series || [];
  const statistics = payload?.statistics || [];

  if (!series.length) {
    mount(chartBox, emptyState({
      title: '所选城市在该窗口内没有数据',
      hint: '可尝试切换时间粒度，或先执行采集管道补充数据。',
      compact: true,
    }));
    mount(statHost, emptyState({ title: '暂无统计结果', compact: true }));
    return;
  }

  // 各城市时间轴可能不完全一致：取并集并按 ISO 字符串排序（可直接比较）
  const axisSet = new Set();
  const valueIndex = series.map((item) => {
    const map = new Map();
    for (const point of item.points || []) map.set(String(point.time), point.value);
    for (const time of map.keys()) axisSet.add(time);
    return map;
  });
  const axis = [...axisSet].sort();
  const axisLabels = axis.map((time) => shortDataTime(time));

  window.requestAnimationFrame(() => {
    if (!chartBox.isConnected) return;
    const chart = renderChart(chartBox, lineChart({
      categories: axisLabels,
      series: series.map((item, index) => ({
        name: item.city_name || item.city_slug,
        color: PALETTE[index % PALETTE.length],
        data: axis.map((time) => {
          const value = valueIndex[index].get(time);
          return typeof value === 'number' ? value : null;
        }),
      })),
    }));
    if (chart) relays.push(chart);
  });

  mount(
    statHost,
    card('统计对比', table(
      [
        { key: 'city_name', title: '城市' },
        { key: 'avg', title: '均值', align: 'right', render: (row) => formatFixed(row.avg, 2) },
        { key: 'max', title: '最大值', align: 'right', render: (row) => formatFixed(row.max, 2) },
        { key: 'min', title: '最小值', align: 'right', render: (row) => formatFixed(row.min, 2) },
        { key: 'p95', title: 'P95', align: 'right', render: (row) => formatFixed(row.p95, 2) },
        { key: 'samples', title: '样本数', align: 'right', render: (row) => formatInt(row.samples) },
      ],
      statistics,
      { empty: '暂无统计数据' },
    ), { subtitle: `按均值倒序 · 单位为 ${metricMeta.unit || '无量纲'}` }),
    note('统计口径与看板一致：逐小时为原始值，按日聚合为当日均值；P95 为 95 分位数。跨时区城市的时间轴仅保证各自本地时间对齐。'),
  );
}
