/**
 * clustering.js —— 城市聚类。
 *
 * 用途是横向管理：15 个城市逐个盯不现实，先按环境特征分组，就能对"同一类城市"
 * 用同一套治理策略。做法是把每个城市压缩成一个特征向量（窗口内各指标均值），
 * 标准化后做 KMeans，并输出相似度矩阵用于找"参考城市"。
 *
 * 数据来源：`/api/insights/clustering`（数据不足时返回 `available:false` + 中文 `message`）。
 *
 * 时间口径：特征由窗口内的逐小时记录聚合而来，时间为城市本地裸时间字符串，
 * 页面不做时区换算。
 */

import { api } from '/shared/api.js';
import { renderChart } from '/shared/charts.js';
import { ink } from '/shared/theme.js';
import {
  badge,
  button,
  card,
  chip,
  dash,
  el,
  emptyState,
  formatInt,
  kpiCard,
  mount,
  note,
  table,
} from '/shared/ui.js';

const WINDOW_OPTIONS = [
  { value: '7', label: '近 7 天' },
  { value: '30', label: '近 30 天' },
  { value: '90', label: '近 90 天' },
  { value: '180', label: '近 180 天' },
  { value: '365', label: '近 365 天' },
];

/** 与后端 `clustering.DEFAULT_FEATURES` 一致的默认特征。 */
const DEFAULT_FEATURES = [
  'temperature_2m',
  'relative_humidity_2m',
  'wind_speed_10m',
  'pressure_msl',
  'precipitation',
  'pm2_5',
  'pm10',
  'ozone',
  'european_aqi',
];

const K_MAX = 8;

const CLUSTER_COLORS = ['#3b82f6', '#22d3ee', '#a78bfa', '#f59e0b', '#22c55e', '#f472b6', '#38bdf8', '#fb923c'];

const state = {
  days: 30,
  k: null,
  cities: [],
  cityMap: new Map(),
  catalog: new Map(),
  metrics: [],
  supported: new Set(),
  featureSelection: new Set(),
  payload: null,
  hosts: null,
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

function metricLabel(name) {
  const meta = state.catalog.get(name);
  return meta?.label || name;
}

function metricUnit(name) {
  return state.catalog.get(name)?.unit || '';
}

function cityLabel(slug) {
  return state.cityMap.get(slug) || slug;
}

/** 已选特征；为空时回退到默认特征集（未选任何特征时不发送 features 参数）。 */
function requestedFeatures() {
  const picked = [...state.featureSelection];
  return picked.length ? picked : null;
}

function featureText() {
  const picked = requestedFeatures();
  if (!picked) return `默认 ${DEFAULT_FEATURES.length} 项特征`;
  return `${picked.length} 项特征`;
}

function silhouetteClass(value) {
  if (!isNum(value)) return 'text-mute';
  if (value >= 0.5) return 'text-ok';
  if (value >= 0.3) return 'text-warn';
  return 'text-dim';
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
    title: '数据不足，暂无法聚类',
    hint: `${message || '窗口内没有足够的可用数据。'}<br />请前往「数据刷新」触发一次数据采集，采集完成后再回到本页。`,
    compact: true,
    actions: [
      button('前往数据刷新', { kind: 'primary', onClick: () => ctx && ctx.navigate('ops') }),
      button('刷新本页', { onClick: () => ctx && ctx.refresh() }),
    ],
  }));
}

// ==================================================================
// 生命周期
// ==================================================================

export async function render(container, ctx) {
  ctxRef = ctx;
  mount(container, emptyState({ title: '正在读取聚类特征目录…', hint: '加载城市主数据与指标字典。', compact: true }));

  const [cityPayload, metricPayload] = await Promise.all([
    api.envCities().catch(() => ({ items: [] })),
    api.envMetrics().catch(() => ({ items: [] })),
  ]);

  state.cities = cityPayload?.items || [];
  state.cityMap = new Map(state.cities.map((city) => [city.slug, city.name_zh || city.slug]));

  const catalogItems = metricPayload?.items || [];
  state.catalog = new Map(catalogItems.map((item) => [item.name, item]));
  state.metrics = catalogItems.filter((item) => item.numeric);

  state.supported = new Set(state.metrics.map((item) => item.name));
  if (!state.featureSelection.size) {
    state.featureSelection = new Set(DEFAULT_FEATURES.filter((name) => state.supported.has(name)));
  }

  state.hosts = { controls: el('div'), body: el('div'), panel: null };
  mount(container, state.hosts.controls, state.hosts.body);

  renderControls(ctx);
  await loadClustering(ctx);
}

export function destroy() {
  ctxRef = null;
  state.hosts = null;
  state.payload = null;
}

// ==================================================================
// 控件
// ==================================================================

function kInput(onCommit) {
  const input = el('input.input', {
    type: 'number',
    value: state.k === null ? '' : String(state.k),
    min: '2',
    max: String(K_MAX),
    step: '1',
    placeholder: '留空则按轮廓系数自动选择',
  });
  const commit = () => {
    const raw = String(input.value).trim();
    if (!raw) {
      state.k = null;
      onCommit();
      return;
    }
    const parsed = Number(raw);
    if (!Number.isFinite(parsed)) {
      input.value = state.k === null ? '' : String(state.k);
      return;
    }
    const clamped = Math.min(K_MAX, Math.max(2, Math.round(parsed)));
    input.value = String(clamped);
    state.k = clamped;
    onCommit();
  };
  input.addEventListener('change', commit);
  input.addEventListener('blur', commit);
  return el('div.field', null, [
    el('label', { text: `簇数 K（2 ~ ${K_MAX}）` }),
    input,
  ]);
}

function renderControls(ctx) {
  const daySelect = el('div.field', null, [
    el('label', { text: '时间窗口' }),
    (() => {
      const node = el('select.select');
      for (const option of WINDOW_OPTIONS) {
        node.appendChild(el('option', {
          value: option.value,
          text: option.label,
          selected: String(option.value) === String(state.days) || null,
        }));
      }
      node.addEventListener('change', () => {
        state.days = Number(node.value);
        loadClustering(ctx);
      });
      return node;
    })(),
  ]);

  const featureChips = state.metrics.slice(0, 30).map((item) =>
    chip(item.label || item.name, null, state.featureSelection.has(item.name), () => {
      if (state.featureSelection.has(item.name)) {
        if (state.featureSelection.size <= 2) return; // 少于两个特征无法聚类
        state.featureSelection.delete(item.name);
      } else {
        state.featureSelection.add(item.name);
      }
      renderControls(ctx);
      loadClustering(ctx);
    }),
  );

  mount(
    state.hosts.controls,
    card('聚类参数', el('div', null, [
      el('div.toolbar', null, [
        daySelect,
        kInput(() => loadClustering(ctx)),
        el('div.field', null, [
          el('label', { text: '操作' }),
          el('div.card-actions', null, [
            button('重新聚类', { kind: 'primary', onClick: () => loadClustering(ctx) }),
            button('恢复默认特征', {
              onClick: () => {
                state.featureSelection = new Set(DEFAULT_FEATURES.filter((name) => state.supported?.has(name)));
                renderControls(ctx);
                loadClustering(ctx);
              },
            }),
            button('前往数据刷新', { onClick: () => ctx.navigate('ops') }),
          ]),
        ]),
      ]),
      el('div.field', { style: { marginTop: '12px' } }, [
        el('label', { text: `聚类特征（多选，至少保留 2 个；当前：${featureText()}）` }),
        el('div.chip-row', null, featureChips.length ? featureChips : [el('span.text-mute', { text: '暂无可选特征' })]),
      ]),
    ]), {
      subtitle:
        '特征为窗口内各指标的均值，聚类前会按列做 Z-score 标准化，避免量纲最大的指标主导距离；' +
        'K 留空时在 2 ~ 6 之间按轮廓系数自动选择。',
      actions: [badge(`近 ${state.days} 天`, 'badge-accent')],
    }),
  );
}

// ==================================================================
// 主体
// ==================================================================

async function loadClustering(ctx) {
  const panel = state.hosts?.panel || panelHost('城市分组结果', { subtitle: '' });
  state.hosts.panel = panel;
  if (!panel.node.isConnected) mount(state.hosts.body, panel.node);

  panelLoading(panel, '正在聚类…');

  const params = { cities: null, days: state.days, features: requestedFeatures(), k: state.k };
  let payload;
  try {
    payload = await api.insightsClustering(params);
  } catch (error) {
    panelError(panel, error, () => loadClustering(ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  state.payload = payload;
  // 接口回传的 features 是实际参与聚类的列，用它回填选择器，保证后续请求与结果一致
  if (Array.isArray(payload.features) && payload.features.length) {
    state.featureSelection = new Set(payload.features);
  }

  renderClustering(panel, payload, ctx);
}

function renderClustering(panel, payload, ctx) {
  const clusters = payload.clusters || [];
  const features = payload.features || [];
  const featureLabels = payload.feature_labels || features.map(metricLabel);
  const similarity = payload.similarity || { labels: [], values: [] };
  const candidateK = payload.candidate_k || [];
  const pairs = payload.most_similar_pairs || [];

  if (!clusters.length) {
    panelUnavailable(panel, '没有形成有效的聚类分组', ctx);
    return;
  }

  const bestCandidate = candidateK
    .filter((item) => isNum(item.silhouette))
    .sort((a, b) => b.silhouette - a.silhouette)[0] || null;
  const autoChosen = state.k === null;
  const featureLine = features.map((name) => `${metricLabel(name)}${metricUnit(name) ? `（${metricUnit(name)}）` : ''}`).join('、');

  const similarPairs = pairs.slice(0, 12).map((pair, index) => ({ ...pair, rank: index + 1 }));

  const pairColumns = [
    { key: 'rank', title: '序号', align: 'right', render: (row) => formatInt(row.rank) },
    { key: 'left', title: '城市 A', render: (row) => row.left_name || cityLabel(row.left) },
    { key: 'right', title: '城市 B', render: (row) => row.right_name || cityLabel(row.right) },
    {
      key: 'similarity',
      title: '相似度',
      align: 'right',
      render: (row) => el('span', { class: isNum(row.similarity) && row.similarity >= 0.9 ? 'text-ok' : '', text: fmt(row.similarity, 3) }),
    },
    {
      key: 'same_cluster',
      title: '是否同组',
      render: (row) => badge(row.same_cluster ? '同一分组' : '不同分组', row.same_cluster ? 'badge-ok' : 'badge-mute'),
    },
  ];

  const chartId = nextId('cluster-k');
  const heatId = nextId('cluster-heat');

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, [
      kpiCard({
        label: '簇数 K',
        value: formatInt(payload.k),
        unit: '组',
        foot: autoChosen && bestCandidate ? `按轮廓系数自动选择（最优候选 K=${bestCandidate.k}）` : '按手动指定的 K 聚类',
      }),
      kpiCard({
        label: '轮廓系数',
        value: fmt(payload.silhouette, 3),
        foot: isNum(payload.silhouette)
          ? payload.silhouette >= 0.5
            ? '簇内紧凑、簇间分离良好'
            : payload.silhouette >= 0.3
              ? '分组结构一般，建议更换特征或窗口'
              : '分组结构较弱，结论需谨慎'
          : '样本量不足无法评估',
        tone: isNum(payload.silhouette) && payload.silhouette >= 0.5 ? 'ok' : isNum(payload.silhouette) && payload.silhouette < 0.3 ? 'warn' : '',
      }),
      kpiCard({
        label: '簇内平方和（inertia）',
        value: fmt(payload.inertia, 2),
        foot: '越小说明簇内越紧凑（随 K 增大自然下降，不宜单独比较）',
      }),
      kpiCard({
        label: '参与聚类的城市',
        value: formatInt(payload.city_count),
        unit: '个',
        foot: `近 ${formatInt(payload.window_days)} 天均值特征`,
      }),
      kpiCard({
        label: '特征维度',
        value: formatInt(features.length),
        unit: '项',
        foot: `${featureText()}（标准化后参与距离计算）`,
      }),
    ]),
    el('div.grid.grid-2', null, clusters.map((cluster, index) => clusterCard(cluster, index, features))),
    card('城市相似度矩阵', el('div.chart', { id: heatId, style: { height: `${Math.max(320, (similarity.labels || []).length * 28 + 130)}px` } }), {
      subtitle: '相似度由标准化特征向量间的欧氏距离换算而来（1 表示完全一致，越接近 1 越相似）。',
    }),
    card('聚类数选择依据', el('div.chart', { id: chartId, style: { height: '320px' } }), {
      subtitle: '轮廓系数越高说明该 K 下的分组越清晰；inertia 随 K 单调下降，两者一起看才能判断拐点。',
    }),
    card('最相似城市对', table(pairColumns, similarPairs, { empty: '没有可比较的城市对' }), {
      subtitle: `按相似度降序取前 ${formatInt(similarPairs.length)} 对（接口最多返回 12 对）。`,
    }),
    note(payload.caveat || '聚类基于所选窗口的均值特征，窗口变化或数据缺失可能改变分组结果。'),
    note(`参与聚类的特征：${featureLine}。为避免过度拥挤，下方图表仅展示前 6 项簇中心原始量纲值。`),
  );

  window.requestAnimationFrame(() => {
    drawHeatmap(document.getElementById(heatId), similarity);
    drawCandidateK(document.getElementById(chartId), candidateK, payload.k);
  });
}

function clusterCard(cluster, index, features) {
  const color = CLUSTER_COLORS[index % CLUSTER_COLORS.length];
  const distinguishing = cluster.distinguishing || [];

  const chips = distinguishing.length
    ? distinguishing.map((item) =>
        el('span.chip', { style: { cursor: 'default' } }, [
          el('span', { text: `${item.label || metricLabel(item.feature)} ${item.direction || ''}` }),
          el('span.chip-count', { text: `z=${fmt(item.z, 2)}` }),
        ]),
      )
    : [el('span.text-mute', { text: '各特征接近整体均值，无明显区分特征' })];

  const members = cluster.members || [];
  const memberChips = members.map((member) =>
    el('span.chip', { style: { cursor: 'default' }, title: `与簇内其他成员的平均相似度 ${fmt(member.similarity_to_cluster, 3)}` }, [
      el('span', { text: member.city_name || cityLabel(member.city_slug) }),
      el('span.chip-count', { text: fmt(member.similarity_to_cluster, 2) }),
    ]),
  );

  const centroid = cluster.centroid || {};
  const centroidText = features
    .filter((name) => isNum(centroid[name]))
    .slice(0, 6)
    .map((name) => `${metricLabel(name)} ${fmt(centroid[name], 2)}${metricUnit(name) ? ` ${metricUnit(name)}` : ''}`)
    .join(' · ');

  return card(cluster.label || `分组 ${cluster.cluster_id + 1}`, el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px' } }, [
    el('div.row-between', null, [
      el('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '8px' } }, [
        el('span', { style: { width: '10px', height: '10px', borderRadius: '3px', background: color } }),
        el('span.text-dim', { style: { fontSize: '12.5px' }, text: `分组 ${cluster.cluster_id + 1}` }),
      ]),
      badge(`${formatInt(cluster.size)} 个城市`, 'badge-accent'),
    ]),
    el('div', null, [
      el('div.card-sub', { text: '相对整体显著偏离的特征（|z| ≥ 0.55）' }),
      el('div.chip-row', { style: { marginTop: '6px' } }, chips),
    ]),
    el('div', null, [
      el('div.card-sub', { text: '成员城市（括号内为与簇内其他成员的平均相似度）' }),
      el('div.chip-row', { style: { marginTop: '6px' } }, memberChips),
    ]),
    centroidText ? el('div.card-sub', { text: `簇中心（原始量纲）：${centroidText}` }) : null,
  ]), { subtitle: `含 ${formatInt(cluster.size)} 个城市` });
}

function drawHeatmap(target, similarity) {
  if (!target || !target.isConnected) return;
  const labels = similarity.labels || [];
  const values = similarity.values || [];

  if (labels.length < 2 || !values.length) {
    renderChart(target, { backgroundColor: 'transparent' }, {
      emptyMessage: '参与聚类的城市不足两个，无法计算相似度矩阵。',
    });
    return;
  }

  const cells = [];
  for (let row = 0; row < values.length; row += 1) {
    const line = values[row] || [];
    for (let col = 0; col < line.length; col += 1) {
      const value = num(line[col]);
      if (value !== null) cells.push([col, row, Number(value.toFixed(3))]);
    }
  }

  const flat = cells.map((cell) => cell[2]);

  renderChart(target, {
    backgroundColor: 'transparent',
    animation: false,
    textStyle: { color: ink().dim, fontFamily: 'inherit', fontSize: 12 },
    grid: { left: 12, right: 18, top: 20, bottom: 80, containLabel: true },
    tooltip: {
      trigger: 'item',
      backgroundColor: ink().tooltipBg,
      borderColor: ink().border,
      borderWidth: 1,
      textStyle: { color: ink().text, fontSize: 12 },
      confine: true,
      formatter: (params) => `${labels[params.value[1]]} × ${labels[params.value[0]]}<br />相似度 ${params.value[2]}`,
    },
    xAxis: {
      type: 'category',
      data: labels,
      splitArea: { show: true, areaStyle: { color: [ink().radarA, ink().radarB] } },
      axisLabel: { color: ink().mute, fontSize: 10.5, interval: 0, rotate: 45 },
      axisLine: { lineStyle: { color: ink().grid } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'category',
      data: labels,
      splitArea: { show: true, areaStyle: { color: [ink().radarA, ink().radarB] } },
      axisLabel: { color: ink().mute, fontSize: 10.5, interval: 0 },
      axisLine: { lineStyle: { color: ink().grid } },
      axisTick: { show: false },
    },
    visualMap: {
      min: flat.length ? Math.min(...flat) : 0,
      max: flat.length ? Math.max(...flat) : 1,
      calculable: true,
      orient: 'horizontal',
      left: 'center',
      bottom: 6,
      textStyle: { color: ink().mute, fontSize: 11 },
      inRange: { color: ['#0b1b33', '#1d4ed8', '#22d3ee', '#facc15'] },
    },
    series: [
      {
        name: '相似度',
        type: 'heatmap',
        data: cells,
        label: {
          show: labels.length <= 12,
          color: ink().text,
          fontSize: 10,
          formatter: (params) => params.value[2],
        },
        itemStyle: { borderColor: ink().cellBorder, borderWidth: 0.5 },
        emphasis: { itemStyle: { borderColor: '#93c5fd', borderWidth: 1.5 } },
      },
    ],
  });
}

function drawCandidateK(target, candidateK, chosenK) {
  if (!target || !target.isConnected) return;
  if (!candidateK.length) {
    renderChart(target, { backgroundColor: 'transparent' }, {
      emptyMessage: '本次未返回候选 K 的评估结果（可能是手动指定了 K，或城市数量不足以尝试多个 K）。',
    });
    return;
  }

  const categories = candidateK.map((item) => `K=${item.k}`);

  renderChart(target, {
    backgroundColor: 'transparent',
    animation: false,
    textStyle: { color: ink().dim, fontFamily: 'inherit', fontSize: 12 },
    legend: { top: 0, right: 8, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: ink().dim, fontSize: 11.5 } },
    grid: { left: 10, right: 22, top: 36, bottom: 8, containLabel: true },
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: ink().tooltipBg,
      borderColor: ink().border,
      borderWidth: 1,
      textStyle: { color: ink().text, fontSize: 12 },
      confine: true,
      formatter: (params) => {
        const item = candidateK[params?.[0]?.dataIndex];
        if (!item) return '';
        return [
          `K = ${item.k}${item.k === chosenK ? '（当前采用）' : ''}`,
          `轮廓系数 ${fmt(item.silhouette, 3)}`,
          `inertia ${fmt(item.inertia, 2)}`,
          `各簇规模 ${Object.entries(item.sizes || {}).map(([key, value]) => `${key}组:${value}`).join('、') || '—'}`,
        ].join('<br />');
      },
    },
    xAxis: {
      type: 'category',
      data: categories,
      axisLabel: { color: ink().mute, fontSize: 11, interval: 0 },
      axisLine: { lineStyle: { color: ink().grid } },
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        name: '轮廓系数',
        nameTextStyle: { color: ink().mute, fontSize: 11 },
        axisLabel: { color: ink().mute, fontSize: 11 },
        splitLine: { lineStyle: { color: ink().grid, type: 'dashed' } },
      },
      {
        type: 'value',
        name: 'inertia',
        nameTextStyle: { color: ink().mute, fontSize: 11 },
        axisLabel: { color: ink().mute, fontSize: 11 },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: 'inertia',
        type: 'bar',
        yAxisIndex: 1,
        barMaxWidth: 30,
        data: candidateK.map((item) => num(item.inertia)),
        itemStyle: { color: 'rgba(100, 116, 139, 0.55)', borderRadius: [3, 3, 0, 0] },
      },
      {
        name: '轮廓系数',
        type: 'line',
        yAxisIndex: 0,
        smooth: false,
        symbol: 'circle',
        symbolSize: 8,
        data: candidateK.map((item) => num(item.silhouette)),
        lineStyle: { width: 2, color: '#22d3ee' },
        itemStyle: { color: '#22d3ee' },
        markPoint: {
          symbolSize: 46,
          label: { color: '#0b1120', fontSize: 10, formatter: (params) => `K=${candidateK[params.dataIndex]?.k ?? ''}` },
          itemStyle: { color: '#facc15' },
          data: candidateK
            .map((item, index) => ({ index, silhouette: item.silhouette }))
            .filter((item) => isNum(item.silhouette))
            .sort((a, b) => b.silhouette - a.silhouette)
            .slice(0, 1)
            .map((item) => ({ coord: [item.index, item.silhouette], value: item.silhouette })),
        },
        markLine: isNum(chosenK)
          ? {
              silent: true,
              symbol: 'none',
              lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.4 },
              label: { formatter: `当前 K=${chosenK}`, color: ink().warnInk, fontSize: 11 },
              data: [{ xAxis: candidateK.findIndex((item) => item.k === chosenK) }],
            }
          : undefined,
      },
    ],
  });
}
