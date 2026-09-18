/**
 * relationship.js —— 成因分析（气象—空气质量关系）。
 *
 * 回答"为什么这次污染重"：把气象要素与污染物配对，同时给出散点形态、两类相关系数、
 * 分箱趋势与一元回归。**这是相关性分析，不是因果推断**，接口返回的 `interpretation`
 * 与 `caveat` 因此在页面上被放在显眼位置。
 *
 * 数据来源：
 *  * `/api/insights/relationship/pairs` —— 预设配对及其业务含义；
 *  * `/api/insights/relationship`       —— 散点、相关系数、分箱曲线、回归。
 *
 * 时间口径：接口按城市本地裸时间字符串取数，页面不做时区换算。
 */

import { api } from '/shared/api.js';
import { renderChart, theme, tooltipStyle } from '/shared/charts.js';
import {
  badge,
  button,
  card,
  chip,
  dash,
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

const WINDOW_OPTIONS = [
  { value: '7', label: '近 7 天' },
  { value: '30', label: '近 30 天' },
  { value: '90', label: '近 90 天' },
  { value: '180', label: '近 180 天' },
  { value: '365', label: '近 365 天' },
];

const BIN_OPTIONS = [3, 5, 8, 12, 16, 20].map((value) => ({ value: String(value), label: `${value} 个分箱` }));

const CITY_COLORS = [
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
  '#94a3b8',
  '#facc15',
  '#2dd4bf',
  '#f87171',
  '#818cf8',
];

/** 接口不可用时的兜底配对（与 atmos/insights/relationship.py: DEFAULT_PAIRS 一致）。 */
const FALLBACK_PAIRS = [
  { x: 'wind_speed_10m', y: 'pm2_5', title: '风速 → PM2.5', rationale: '风速越大，水平和垂直扩散越强，颗粒物越难累积。通常呈负相关。' },
  { x: 'relative_humidity_2m', y: 'pm2_5', title: '相对湿度 → PM2.5', rationale: '高湿促进二次气溶胶生成与吸湿增长，常呈正相关；但在降水时会被冲刷。' },
  { x: 'temperature_2m', y: 'ozone', title: '气温 → 臭氧', rationale: '高温强辐射下光化学反应加剧，臭氧生成量上升，通常呈正相关。' },
];

const state = {
  days: 30,
  bins: 8,
  cities: [],
  cityMap: new Map(),
  catalog: new Map(),
  pairs: [],
  pairIndex: 0,
  byCity: false,
  payload: null,
  hosts: null,
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

function cityLabel(slug) {
  return state.cityMap.get(slug) || slug;
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

function currentPair() {
  return state.pairs[state.pairIndex] || state.pairs[0] || null;
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
    hint: `${message || '所选指标在窗口内没有有效配对数据。'}<br />请前往「数据刷新」触发一次数据采集，采集完成后再回到本页。`,
    compact: true,
    actions: [
      button('前往数据刷新', { kind: 'primary', onClick: () => ctx && ctx.navigate('ops') }),
      button('刷新本页', { onClick: () => ctx && ctx.refresh() }),
    ],
  }));
}

function strengthText(value) {
  if (!isNum(value)) return '样本不足';
  const magnitude = Math.abs(value);
  if (magnitude >= 0.8) return '极强';
  if (magnitude >= 0.6) return '强';
  if (magnitude >= 0.4) return '中等';
  if (magnitude >= 0.2) return '弱';
  return '几乎无';
}

function coefficientClass(value) {
  if (!isNum(value)) return 'text-mute';
  if (Math.abs(value) >= 0.6) return value > 0 ? 'text-bad' : 'text-ok';
  if (Math.abs(value) >= 0.4) return 'text-warn';
  return 'text-dim';
}

/**
 * 重读结论块：结论与口径提醒需要显眼但不必跳色。
 * 用左边框 + 主题文字色，保证在深色主题下始终可读。
 */
function callout({ title, text }) {
  return el('div', {
    style: {
      borderLeft: '3px solid #3b82f6',
      padding: '10px 14px',
      background: 'rgba(59, 130, 246, 0.07)',
      borderRadius: '6px',
      display: 'flex',
      flexDirection: 'column',
      gap: '6px',
    },
  }, [
    el('span', { style: { fontSize: '12px', color: '#7b889e' }, text: title }),
    el('div', { style: { fontSize: '13px', color: '#e6ebf4', lineHeight: '1.6' }, text: text || '—' }),
  ]);
}

// ==================================================================
// 生命周期
// ==================================================================

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在读取可分析配对…', hint: '加载配对清单与指标字典。', compact: true }));

  const [pairsPayload, cityPayload, metricPayload] = await Promise.all([
    api.insightsRelationshipPairs().catch(() => null),
    api.envCities().catch(() => ({ items: [] })),
    api.envMetrics().catch(() => ({ items: [] })),
  ]);

  const items = pairsPayload?.items || [];
  state.pairs = items.length ? items : FALLBACK_PAIRS;
  if (state.pairIndex >= state.pairs.length) state.pairIndex = 0;

  state.cities = cityPayload?.items || [];
  state.cityMap = new Map(state.cities.map((city) => [city.slug, city.name_zh || city.slug]));

  const catalogItems = metricPayload?.items || [];
  state.catalog = new Map(catalogItems.map((item) => [item.name, item]));

  state.hosts = { controls: el('div'), body: el('div') };
  mount(container, state.hosts.controls, state.hosts.body);

  renderControls(ctx);
  await loadPair(ctx);
}

export function destroy() {
  state.hosts = null;
  state.payload = null;
}

// ==================================================================
// 控件
// ==================================================================

function renderControls(ctx) {
  const pair = currentPair();

  const pairSelect = select(
    state.pairs.map((item, index) => ({ value: String(index), label: item.title || `${item.x} → ${item.y}` })),
    {
      value: String(state.pairIndex),
      onChange: (value) => {
        state.pairIndex = Number(value);
        renderControls(ctx);
        loadPair(ctx);
      },
    },
  );

  const daySelect = select(WINDOW_OPTIONS, {
    value: String(state.days),
    onChange: (value) => {
      state.days = Number(value);
      loadPair(ctx);
    },
  });

  const binSelect = select(BIN_OPTIONS, {
    value: String(state.bins),
    onChange: (value) => {
      state.bins = Number(value);
      loadPair(ctx);
    },
  });

  const byCityChip = chip('逐城市给出相关系数', null, state.byCity, () => {
    state.byCity = !state.byCity;
    renderControls(ctx);
    loadPair(ctx);
  });
  byCityChip.title = '开启后接口会额外返回每个城市的 Pearson / Spearman 系数，用于检验规律是否普遍成立';

  mount(
    state.hosts.controls,
    card('分析设置', el('div', null, [
      el('div.toolbar', null, [
        el('div.field', { style: { minWidth: '260px' } }, [el('label', { text: '指标配对' }), pairSelect]),
        el('div.field', null, [el('label', { text: '时间窗口' }), daySelect]),
        el('div.field', null, [el('label', { text: '分箱数量' }), binSelect]),
        el('div.field', null, [
          el('label', { text: '选项' }),
          el('div.chip-row', null, [byCityChip]),
        ]),
        el('div.field', null, [
          el('label', { text: '操作' }),
          el('div.card-actions', null, [
            button('重新分析', { kind: 'primary', onClick: () => loadPair(ctx) }),
            button('前往数据刷新', { onClick: () => ctx.navigate('ops') }),
          ]),
        ]),
      ]),
      pair?.rationale
        ? el('div.card-sub', { text: `业务含义：${pair.rationale}`, style: { marginTop: '10px' } })
        : null,
    ]), {
      subtitle: '自变量为气象要素，因变量为污染物；配对清单由后端预设，每对都带有明确的业务含义。',
      actions: [badge(`近 ${state.days} 天`, 'badge-accent')],
    }),
  );
}

// ==================================================================
// 主体
// ==================================================================

async function loadPair(ctx) {
  const host = state.hosts?.body;
  const pair = currentPair();
  if (!host) return;

  if (!pair) {
    mount(host, emptyState({
      title: '没有可分析的指标配对',
      hint: '配对清单为空，请检查后端关联分析配置。',
      actions: [button('前往数据刷新', { kind: 'primary', onClick: () => ctx.navigate('ops') })],
    }));
    return;
  }

  const panel = panelHost(`${pair.title || `${pair.x} → ${pair.y}`} 关系分析`, {
    subtitle: `${metricLabel(pair.x)} → ${metricLabel(pair.y)} · 近 ${state.days} 天 · ${state.byCity ? '逐城市相关系数' : '全平台合并分析'}`,
  });
  mount(host, panel.node);
  panelLoading(panel, '正在计算相关性与回归…');

  const params = {
    x: pair.x,
    y: pair.y,
    days: state.days,
    bins: state.bins,
    byCity: state.byCity,
  };

  let payload;
  try {
    payload = await api.insightsRelationship(params);
  } catch (error) {
    panelError(panel, error, () => loadPair(ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  state.payload = payload;
  renderPair(panel, payload, ctx);
}

function renderPair(panel, payload, ctx) {
  mergeMetric(payload.x, payload.x_label, payload.x_unit);
  mergeMetric(payload.y, payload.y_label, payload.y_unit);

  const xLabel = payload.x_label || metricLabel(payload.x);
  const yLabel = payload.y_label || metricLabel(payload.y);
  const xUnit = payload.x_unit || '';
  const yUnit = payload.y_unit || '';
  const scatter = (payload.scatter || []).filter((point) => isNum(point.x) && isNum(point.y));
  const bins = payload.bins || [];
  const regression = payload.regression || null;

  if (!scatter.length) {
    panelUnavailable(panel, '所选配对在窗口内没有有效的成对样本', ctx);
    return;
  }

  const scatterId = nextId('rel-scatter');
  const binId = nextId('rel-bin');

  const r2 = num(regression?.r2);
  const detailBlocks = [
    el('div.grid.grid-kpi', null, [
      kpiCard({
        label: 'Pearson 线性相关',
        value: fmt(payload.pearson, '', 3),
        foot: payload.pearson_label || strengthText(payload.pearson),
        tone: isNum(payload.pearson) && Math.abs(payload.pearson) >= 0.6 ? 'warn' : '',
      }),
      kpiCard({
        label: 'Spearman 秩相关',
        value: fmt(payload.spearman, '', 3),
        foot: payload.spearman_label || strengthText(payload.spearman),
      }),
      kpiCard({
        label: '回归 R²',
        value: fmt(r2, '', 3),
        foot: r2 === null ? '样本不足' : `解释 ${(r2 * 100).toFixed(1)}% 的因变量变异`,
      }),
      kpiCard({
        label: '有效样本',
        value: formatInt(payload.samples),
        unit: '条',
        foot: `覆盖 ${formatInt(payload.city_count)} 个城市 · 近 ${formatInt(payload.window_days)} 天`,
      }),
      kpiCard({
        label: '回归方程',
        value: regression ? `${fmt(regression.slope, '', 4)} x + ${fmt(regression.intercept, '', 3)}` : '—',
        foot: `y = ${yLabel}${yUnit ? `（${yUnit}）` : ''}，x = ${xLabel}${xUnit ? `（${xUnit}）` : ''}`,
      }),
      kpiCard({
        label: '参与拟合的样本',
        value: formatInt(regression?.n ?? payload.samples),
        unit: '条',
        foot: '一元最小二乘',
      }),
    ]),
    el('div.grid.grid-2', null, [
      callout({ title: '结论（后端自动生成）', text: payload.interpretation }),
      callout({ title: '口径提醒（相关性 ≠ 因果）', text: payload.caveat || '相关性不等于因果。' }),
    ]),
    el('div.grid.grid-2', null, [
      card(`${xLabel} 与 ${yLabel} 散点与回归`, el('div.chart', { id: scatterId, style: { height: '380px' } }), {
        subtitle: `每个点是一条逐小时记录（接口抽样上限 1500 点）；颜色区分城市，虚线为一元线性回归。单位：x ${xUnit || '无量纲'}，y ${yUnit || '无量纲'}。`,
      }),
      card('分箱趋势', el('div.chart', { id: binId, style: { height: '380px' } }), {
        subtitle: bins.length
          ? `按 ${xLabel} 等宽分为 ${bins.length} 箱，展示每箱的均值与中位数；悬停可查看箱内样本数。`
          : '样本量不足以分箱时该图不展示。',
      }),
    ]),
    bins.length
      ? card('分箱明细', table(
          [
            { key: 'range', title: `${xLabel} 区间`, render: (row) => `${fmt(row.lower, '', 2)} ~ ${fmt(row.upper, '', 2)}` },
            { key: 'center', title: '箱中心', align: 'right', render: (row) => fmt(row.center, xUnit, 2) },
            { key: 'mean', title: `${yLabel} 均值`, align: 'right', render: (row) => fmt(row.mean, yUnit, 2) },
            { key: 'median', title: `${yLabel} 中位数`, align: 'right', render: (row) => fmt(row.median, yUnit, 2) },
            { key: 'count', title: '样本数', align: 'right', render: (row) => formatInt(row.count) },
          ],
          bins,
          { empty: '暂无可分箱样本' },
        ), { subtitle: '分箱均值比散点更能看清趋势方向；均值与中位数差异大时提示箱内存在偏态或极端值。' })
      : null,
  ].filter(Boolean);

  const citySection = renderCitySection(payload);

  mount(
    panel.host,
    ...detailBlocks,
    citySection,
    note(
      `数据时间：城市本地时间（裸时间字符串），窗口近 ${formatInt(payload.window_days)} 天，取数时间 ` +
      `${payload.generated_at ? formatDataTime(payload.generated_at) : '—'}。` +
      '回归线只描述窗口内的线性趋势，超出窗口范围不外推。',
    ),
  );

  window.requestAnimationFrame(() => {
    drawScatter(document.getElementById(scatterId), payload, scatter);
    drawBins(document.getElementById(binId), payload, bins);
  });
}

function renderCitySection(payload) {
  if (!state.byCity) return null;
  const rows = payload.by_city || [];
  if (!rows.length) {
    return card('逐城市相关系数', emptyState({
      title: '没有城市具备足够的配对样本',
      hint: '每个城市至少需要 3 条有效配对记录才能计算相关系数。',
      compact: true,
    }));
  }

  return card('逐城市相关系数', table(
    [
      { key: 'city_name', title: '城市' },
      { key: 'samples', title: '有效样本', align: 'right', render: (row) => formatInt(row.samples) },
      {
        key: 'pearson',
        title: 'Pearson',
        align: 'right',
        render: (row) => el('span', { class: coefficientClass(row.pearson), text: fmt(row.pearson, '', 3) }),
      },
      { key: 'spearman', title: 'Spearman', align: 'right', render: (row) => fmt(row.spearman, '', 3) },
      { key: 'strength', title: '强度判定', render: (row) => badge(row.strength || '—', 'badge-info') },
    ],
    rows,
    { empty: '暂无逐城市相关系数' },
  ), {
    subtitle: '用于检验"总体规律"是否在每个城市都成立；符号不一致说明该规律受城市特定条件影响。',
  });
}

function drawScatter(target, payload, scatter) {
  if (!target || !target.isConnected) return;

  const byCity = new Map();
  for (const point of scatter) {
    const key = point.city || '未知城市';
    if (!byCity.has(key)) byCity.set(key, []);
    byCity.get(key).push([point.x, point.y]);
  }

  const series = [...byCity.entries()].map(([slug, data], index) => ({
    name: cityLabel(slug),
    type: 'scatter',
    symbolSize: 5,
    data,
    itemStyle: { color: CITY_COLORS[index % CITY_COLORS.length], opacity: 0.72 },
    emphasis: { focus: 'series' },
  }));

  const regression = payload.regression;
  if (regression && isNum(regression.slope) && isNum(regression.intercept) && scatter.length) {
    const xs = scatter.map((point) => point.x);
    const minX = Math.min(...xs);
    const maxX = Math.max(...xs);
    series.push({
      name: '一元线性回归',
      type: 'line',
      symbol: 'none',
      smooth: false,
      data: [
        [minX, regression.slope * minX + regression.intercept],
        [maxX, regression.slope * maxX + regression.intercept],
      ],
      lineStyle: { width: 2, type: 'dashed', color: '#f59e0b' },
      itemStyle: { color: '#f59e0b' },
      tooltip: { valueFormatter: (value) => fmt(value?.[1], payload.y_unit || '', 2) },
      z: 5,
    });
  }

  renderChart(target, {
    ...theme(),
    grid: { left: 12, right: 22, top: 38, bottom: 10, containLabel: true },
    legend: { top: 0, right: 8, icon: 'circle', itemWidth: 9, itemHeight: 9, itemGap: 12, textStyle: { color: '#a9b5c9', fontSize: 11.5 } },
    tooltip: {
      ...tooltipStyle('item'),
      formatter: (params) => {
        if (params.seriesType === 'line') {
          return `一元线性回归<br />斜率 ${fmt(payload.regression?.slope, '', 4)} · 截距 ${fmt(payload.regression?.intercept, '', 3)}<br />R² ${fmt(payload.regression?.r2, '', 3)}`;
        }
        const [x, y] = params.value || [];
        return [
          `${params.seriesName}`,
          `${payload.x_label || payload.x} ${fmt(x, payload.x_unit || '', 2)}`,
          `${payload.y_label || payload.y} ${fmt(y, payload.y_unit || '', 2)}`,
        ].join('<br />');
      },
    },
    xAxis: {
      type: 'value',
      name: `${payload.x_label || payload.x}${payload.x_unit ? ` / ${payload.x_unit}` : ''}`,
      nameLocation: 'middle',
      nameGap: 28,
      nameTextStyle: { color: '#7b889e', fontSize: 11 },
      axisLabel: { color: '#7b889e', fontSize: 11 },
      splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
    },
    yAxis: {
      type: 'value',
      name: `${payload.y_label || payload.y}${payload.y_unit ? ` / ${payload.y_unit}` : ''}`,
      nameTextStyle: { color: '#7b889e', fontSize: 11 },
      axisLabel: { color: '#7b889e', fontSize: 11 },
      splitLine: { lineStyle: { color: '#263148', type: 'dashed' } },
    },
    series,
  });
}

function drawBins(target, payload, bins) {
  if (!target || !target.isConnected) return;
  if (!bins.length) {
    renderChart(target, theme(), {
      emptyMessage: '样本量不足以分箱（分箱至少需要「箱数 × 2」条有效记录），可减少分箱数量或延长窗口。',
    });
    return;
  }

  const unit = payload.y_unit || '';
  const categories = bins.map((bin) => `${bin.center}`);

  renderChart(target, {
    ...theme(),
    grid: { left: 10, right: 22, top: 36, bottom: 8, containLabel: true },
    legend: { top: 0, right: 8, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: '#a9b5c9', fontSize: 11.5 } },
    tooltip: {
      ...tooltipStyle('axis'),
      formatter: (params) => {
        const bin = bins[params?.[0]?.dataIndex ?? 0];
        if (!bin) return '';
        return [
          `${payload.x_label || payload.x} ∈ [${fmt(bin.lower, '', 2)}, ${fmt(bin.upper, '', 2)}]`,
          `${payload.y_label || payload.y} 均值 ${fmt(bin.mean, unit, 2)}`,
          `中位数 ${fmt(bin.median, unit, 2)}`,
          `样本数 ${formatInt(bin.count)}`,
        ].join('<br />');
      },
    },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: categories,
      name: `${payload.x_label || payload.x}${payload.x_unit ? ` / ${payload.x_unit}` : ''}`,
      nameLocation: 'middle',
      nameGap: 28,
      nameTextStyle: { color: '#7b889e', fontSize: 11 },
      axisLabel: { color: '#7b889e', fontSize: 10.5, hideOverlap: true },
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
        name: '箱内均值',
        type: 'line',
        smooth: true,
        symbol: 'circle',
        symbolSize: 6,
        data: bins.map((bin) => num(bin.mean)),
        lineStyle: { width: 2, color: '#3b82f6' },
        itemStyle: { color: '#3b82f6' },
        areaStyle: { color: '#3b82f6', opacity: 0.1 },
      },
      {
        name: '箱内中位数',
        type: 'line',
        smooth: true,
        symbol: 'none',
        data: bins.map((bin) => num(bin.median)),
        lineStyle: { width: 1.6, type: 'dashed', color: '#22d3ee' },
        itemStyle: { color: '#22d3ee' },
      },
    ],
  });
}
