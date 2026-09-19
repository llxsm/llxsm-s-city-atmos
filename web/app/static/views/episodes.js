/**
 * episodes.js —— 污染过程与异常。
 *
 * 两个粒度不同的分析合在一页：
 *  * **污染过程** —— 把连续超标时段合并为一次"事件"，给起止、时长、峰值、首要污染物
 *    与期间气象（`/api/insights/episodes`）；
 *  * **异常点检测** —— 按 Z 分数或 Tukey 围栏标记显著偏离常态的时刻
 *    （`/api/insights/outliers`）。
 *
 * 两者在数据不足时都返回 `available:false` + 中文 `message`，本页据此渲染空状态并
 * 引导到「数据刷新」。
 *
 * 时间口径：`start` / `end` / `time` 均为城市本地裸时间字符串，图表横轴直接使用这些
 * 字符串解析出的本地时刻，**不做任何时区换算**。
 */

import { api } from '/shared/api.js';
import { renderChart, theme, tooltipStyle } from '/shared/charts.js';
import { ink } from '/shared/theme.js';
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
  formatPercent,
  kpiCard,
  kvList,
  mount,
  note,
  select,
  table,
} from '/shared/ui.js';

const DAY_OPTIONS = [
  { value: '30', label: '近 30 天' },
  { value: '60', label: '近 60 天' },
  { value: '92', label: '近 92 天' },
  { value: '180', label: '近 180 天' },
  { value: '365', label: '近 365 天' },
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

const SEVERITY_TONE = {
  严重: 'badge-bad',
  较重: 'badge-warn',
  中等: 'badge-info',
  轻微: 'badge-mute',
};

const OUTLIER_METHODS = [
  { value: 'zscore', label: 'Z 分数（适合近似正态指标）' },
  { value: 'iqr', label: 'Tukey 围栏（适合长尾指标）' },
];

const POLLUTANT_LABEL = {
  pm2_5: 'PM2.5',
  pm10: 'PM10',
  ozone: '臭氧 O₃',
  nitrogen_dioxide: '二氧化氮 NO₂',
  sulphur_dioxide: '二氧化硫 SO₂',
  carbon_monoxide: '一氧化碳 CO',
};

const state = {
  days: 92,
  cities: [],
  catalog: new Map(),
  metrics: [],
  citySelection: new Set(),
  episode: { threshold: 40, minGap: 3, minDuration: 2, limit: 30, expanded: null, payload: null, key: null },
  outlier: { metric: 'european_aqi', method: 'zscore', threshold: 3, payload: null, key: null },
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

function cityKey() {
  return [...state.citySelection].sort().join(',');
}

/**
 * 把城市本地裸时间字符串解析为本地时刻（毫秒）。
 * 这里只做"字符串 → 本地时刻"的解析，用于把事件画到时间轴上；
 * 不涉及任何时区换算，标签仍按原始字符串展示。
 */
function localTime(value) {
  if (!value) return null;
  const text = String(value).replace(' ', 'T');
  const stamp = Date.parse(text);
  return Number.isNaN(stamp) ? null : stamp;
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
    title: '数据不足，暂无法分析',
    hint: `${message || '窗口内没有足够的可用数据。'}<br />请前往「数据刷新」触发一次数据采集，采集完成后再回到本页。`,
    compact: true,
    actions: [
      button('前往数据刷新', { kind: 'primary', onClick: () => ctx && ctx.navigate('ops') }),
      button('刷新本页', { onClick: () => ctx && ctx.refresh() }),
    ],
  }));
}

/** 数值输入控件；使用 change 事件，避免每敲一个字符就发起一次分析。 */
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
    state.metrics = [{ name: 'european_aqi', label: 'AQI', unit: '' }];
  }
  if (!state.metrics.some((item) => item.name === state.outlier.metric)) {
    state.outlier.metric = state.metrics[0].name;
  }

  state.hosts = {
    controls: el('div'),
    episodes: panelHost('污染过程识别', { subtitle: '把连续超标时段合并为一次污染过程' }),
    outliers: panelHost('异常点检测', { subtitle: '按统计方法标记显著偏离常态的时刻' }),
  };

  mount(container, state.hosts.controls, state.hosts.episodes.node, state.hosts.outliers.node);
  renderControls(ctx);
  await Promise.all([loadEpisodes(ctx), loadOutliers(ctx)]);
}

export function destroy() {
  state.hosts = null;
  state.episode.payload = null;
  state.episode.expanded = null;
  state.outlier.payload = null;
}

// ==================================================================
// 控件
// ==================================================================

function renderControls(ctx) {
  const daySelect = select(DAY_OPTIONS, {
    value: String(state.days),
    onChange: (value) => {
      state.days = Number(value);
      state.episode.payload = null;
      state.outlier.payload = null;
      state.episode.expanded = null;
      renderControls(ctx);
      loadEpisodes(ctx);
      loadOutliers(ctx);
    },
  });

  const cityChips = state.cities.map((city) =>
    chip(city.name_zh, null, state.citySelection.has(city.slug), () => {
      if (state.citySelection.has(city.slug)) state.citySelection.delete(city.slug);
      else state.citySelection.add(city.slug);
      state.episode.payload = null;
      state.outlier.payload = null;
      state.episode.expanded = null;
      renderControls(ctx);
      loadEpisodes(ctx);
      loadOutliers(ctx);
    }),
  );

  const outlierMetricSelect = select(
    state.metrics.map((item) => ({
      value: item.name,
      label: `${item.label || item.name}${item.unit ? `（${item.unit}）` : ''}`,
    })),
    {
      value: state.outlier.metric,
      onChange: (value) => {
        state.outlier.metric = value;
        state.outlier.payload = null;
        loadOutliers(ctx);
      },
    },
  );

  const outlierMethodSelect = select(OUTLIER_METHODS, {
    value: state.outlier.method,
    onChange: (value) => {
      state.outlier.method = value;
      state.outlier.payload = null;
      loadOutliers(ctx);
    },
  });

  mount(
    state.hosts.controls,
    card('分析范围', el('div', null, [
      el('div.toolbar', null, [
        el('div.field', null, [el('label', { text: '时间窗口' }), daySelect]),
        numberField('超标阈值（AQI）', {
          value: state.episode.threshold,
          min: 1,
          max: 500,
          step: 1,
          onCommit: (value) => {
            state.episode.threshold = value;
            state.episode.payload = null;
            state.episode.expanded = null;
            loadEpisodes(ctx);
          },
        }),
        numberField('允许合并的最大达标间隙（小时）', {
          value: state.episode.minGap,
          min: 0,
          max: 24,
          step: 1,
          onCommit: (value) => {
            state.episode.minGap = value;
            state.episode.payload = null;
            state.episode.expanded = null;
            loadEpisodes(ctx);
          },
        }),
        numberField('成案的最短持续时长（小时）', {
          value: state.episode.minDuration,
          min: 1,
          max: 48,
          step: 1,
          onCommit: (value) => {
            state.episode.minDuration = value;
            state.episode.payload = null;
            state.episode.expanded = null;
            loadEpisodes(ctx);
          },
        }),
        numberField('返回条数上限', {
          value: state.episode.limit,
          min: 1,
          max: 200,
          step: 1,
          onCommit: (value) => {
            state.episode.limit = value;
            state.episode.payload = null;
            state.episode.expanded = null;
            loadEpisodes(ctx);
          },
        }),
      ]),
      el('div.toolbar', { style: { marginTop: '12px' } }, [
        el('div.field', { style: { minWidth: '240px' } }, [el('label', { text: '异常检测指标' }), outlierMetricSelect]),
        el('div.field', { style: { minWidth: '240px' } }, [el('label', { text: '异常检测方法' }), outlierMethodSelect]),
        numberField(
          state.outlier.method === 'iqr' ? '围栏倍数（IQR 倍数）' : 'Z 分数阈值',
          {
            value: state.outlier.threshold,
            min: 1,
            max: 10,
            step: 0.5,
            onCommit: (value) => {
              state.outlier.threshold = value;
              state.outlier.payload = null;
              loadOutliers(ctx);
            },
          },
        ),
        el('div.field', null, [
          el('label', { text: '操作' }),
          el('div.card-actions', null, [
            button('重新分析', {
              kind: 'primary',
              onClick: () => {
                state.episode.payload = null;
                state.outlier.payload = null;
                loadEpisodes(ctx);
                loadOutliers(ctx);
              },
            }),
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
        '污染过程采用环保业务通行口径：允许用不超过「最大达标间隙」的时段把两次超标桥接为同一次过程。' +
        '所有时间为城市本地时间，页面不做时区换算。',
      actions: [badge(`近 ${state.days} 天`, 'badge-accent')],
    }),
  );
}

// ==================================================================
// 污染过程
// ==================================================================

async function loadEpisodes(ctx) {
  const panel = state.hosts?.episodes;
  if (!panel) return;
  panelLoading(panel, '正在识别污染过程…');

  const params = {
    cities: requestedCities(),
    days: state.days,
    threshold: state.episode.threshold,
    minGapHours: state.episode.minGap,
    minDurationHours: state.episode.minDuration,
    limit: state.episode.limit,
  };
  const key = JSON.stringify({ ...params, city: cityKey() });

  let payload;
  try {
    payload = await api.insightsEpisodes(params);
  } catch (error) {
    panelError(panel, error, () => loadEpisodes(ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  state.episode.payload = payload;
  state.episode.key = key;
  renderEpisodes(ctx);
}

function renderEpisodes(ctx) {
  const panel = state.hosts?.episodes;
  const payload = state.episode.payload;
  if (!panel || !payload) return;

  const episodes = payload.episodes || [];
  const levels = payload.peak_level_distribution || {};
  const worst = payload.worst_city;

  const kpis = [
    kpiCard({
      label: '污染过程次数',
      value: formatInt(payload.episode_count),
      unit: '次',
      foot: `阈值 ${fmt(payload.threshold, '', 0)}（${payload.threshold_label || '—'}）`,
      tone: payload.episode_count > 0 ? 'warn' : 'ok',
    }),
    kpiCard({
      label: '累计超标小时',
      value: formatInt(payload.total_hours),
      unit: '小时',
      foot: `合并间隙 ≤ ${formatInt(payload.min_gap_hours)} 小时`,
    }),
    kpiCard({
      label: '累计时长最长城市',
      value: worst?.city_name || '—',
      foot: worst ? `${formatInt(worst.hours)} 小时 · ${formatInt(worst.count)} 次 · 峰值 ${fmt(worst.peak, '', 0)}` : '窗口内无成案过程',
      tone: 'bad',
    }),
    kpiCard({
      label: '覆盖城市',
      value: formatInt(payload.city_count),
      unit: '个',
      foot: `近 ${formatInt(payload.window_days)} 天 · ${cityText()}`,
    }),
  ];

  const levelChips = LEVEL_ORDER.filter((level) => levels[level]).map((level) =>
    el('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', color: ink().dim } }, [
      el('span', { style: { width: '10px', height: '10px', borderRadius: '3px', background: levelColor(level) } }),
      el('span', { text: `${level} ${formatInt(levels[level])} 次` }),
    ]),
  );

  const kpiGrid = el('div.grid.grid-kpi', null, kpis);
  const levelCard = card('峰值等级分布', levelChips.length
    ? el('div.legend', null, levelChips)
    : emptyState({ title: '窗口内没有识别到污染过程', hint: '可尝试降低超标阈值，或延长分析窗口。', compact: true }), {
    subtitle: '按每次过程的峰值等级统计次数',
  });

  if (!episodes.length) {
    mount(
      panel.host,
      kpiGrid,
      levelCard,
      emptyState({
        title: '窗口内没有识别到污染过程',
        hint:
          `在阈值 ${fmt(payload.threshold, '', 0)}（${payload.threshold_label || '—'}）、最短持续 ` +
          `${formatInt(payload.min_duration_hours)} 小时的条件下，所选城市与窗口内没有成案的连续超标时段；` +
          '这说明窗口内空气质量整体处于达标水平。如需核对口径，可放宽阈值或延长分析窗口。',
        compact: true,
      }),
    );
    return;
  }

  const timelineId = nextId('episode-timeline');
  // 表与图共用同一份数据（接口已按峰值降序），图上 rank 1 位于最上方，便于对照
  const timelineRows = episodes;

  const columns = [
    { key: 'rank', title: '序号', align: 'right' },
    { key: 'city_name', title: '城市' },
    { key: 'start', title: '开始时间', render: (row) => formatDataTime(row.start) },
    { key: 'end', title: '结束时间', render: (row) => formatDataTime(row.end) },
    { key: 'duration_hours', title: '持续小时', align: 'right', render: (row) => formatInt(row.duration_hours) },
    { key: 'exceed_hours', title: '超标小时', align: 'right', render: (row) => formatInt(row.exceed_hours) },
    { key: 'peak_value', title: '峰值', align: 'right', render: (row) => fmt(row.peak_value, '', 1) },
    {
      key: 'peak_level',
      title: '峰值等级',
      render: (row) => el('span', {
        class: 'badge',
        style: { background: `${levelColor(row.peak_level)}22`, color: levelColor(row.peak_level), borderColor: `${levelColor(row.peak_level)}66` },
        text: row.peak_level || '—',
      }),
    },
    { key: 'mean_value', title: '过程均值', align: 'right', render: (row) => fmt(row.mean_value, '', 1) },
    { key: 'primary_pollutant', title: '首要污染物', render: (row) => row.primary_pollutant || '无' },
    {
      key: 'weather',
      title: '期间气象',
      wrap: true,
      render: (row) => el('span.text-dim', {
        text: `风速 ${fmt(row.weather?.wind_speed_avg, 'km/h', 1)} · 湿度 ${fmt(row.weather?.humidity_avg, '%', 0)} · 降水 ${fmt(row.weather?.precipitation_sum, 'mm', 1)} · 静稳 ${formatInt(row.weather?.stagnant_hours)} 小时`,
      }),
    },
    {
      key: 'severity',
      title: '严重度',
      render: (row) => badge(row.severity || '—', SEVERITY_TONE[row.severity] || 'badge-mute'),
    },
  ];

  const tableNode = table(columns, timelineRows, {
    onRowClick: (row) => {
      state.episode.expanded = state.episode.expanded === row.rank ? null : row.rank;
      applyEpisodeExpansion(tableNode, timelineRows);
    },
    rowKey: (row) => `episode-${row.rank}`,
  });

  mount(
    panel.host,
    kpiGrid,
    levelCard,
    card('污染过程时间轴', el('div.chart', { id: timelineId, style: { height: `${Math.max(280, timelineRows.length * 26 + 110)}px` } }), {
      subtitle: '横轴为城市本地时间；每一行是一次过程（自上而下按峰值降序），条带长度即持续时长，颜色对应峰值等级。',
    }),
    card('污染过程清单', tableNode, {
      subtitle: `共 ${formatInt(episodes.length)} 条（按峰值降序，最多返回 ${formatInt(state.episode.limit)} 条）· 点击任意行可展开该过程的污染物均值明细。`,
    }),
    note('严重度由峰值等级与持续时长共同决定；行内气象为过程期间（含桥接的达标小时）的均值或累计值。'),
  );

  window.requestAnimationFrame(() => {
    const target = document.getElementById(timelineId);
    if (!target || !target.isConnected) return;
    const data = timelineRows.map((row, index) => ({
      value: [index, localTime(row.start), localTime(row.end), row.duration_hours],
      itemStyle: { color: levelColor(row.peak_level) },
      episode: row,
    }));

    renderChart(target, {
      ...theme(),
      grid: { left: 12, right: 24, top: 28, bottom: 8, containLabel: true },
      tooltip: {
        ...tooltipStyle('item'),
        formatter: (params) => {
          const row = params.data?.episode;
          if (!row) return '';
          return [
            `第 ${row.rank} 次 · ${row.city_name}`,
            `${formatDataTime(row.start)} → ${formatDataTime(row.end)}`,
            `持续 ${formatInt(row.duration_hours)} 小时（超标 ${formatInt(row.exceed_hours)} 小时）`,
            `峰值 ${fmt(row.peak_value, '', 1)}（${row.peak_level || '—'}） · 均值 ${fmt(row.mean_value, '', 1)}`,
            `首要污染物 ${row.primary_pollutant || '无'}`,
            `严重度 ${row.severity || '—'}`,
          ].join('<br />');
        },
      },
      xAxis: {
        type: 'time',
        axisLabel: { color: ink().mute, fontSize: 11, hideOverlap: true },
        axisLine: { lineStyle: { color: ink().grid } },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'category',
        data: timelineRows.map((row) => `#${row.rank} ${row.city_name}`),
        axisLabel: { color: ink().mute, fontSize: 11, interval: 0 },
        axisLine: { lineStyle: { color: ink().grid } },
        axisTick: { show: false },
      },
      series: [
        {
          type: 'custom',
          name: '污染过程',
          renderItem: (params, apiRef) => {
            const categoryIndex = apiRef.value(0);
            const start = apiRef.coord([apiRef.value(1), categoryIndex]);
            const end = apiRef.coord([apiRef.value(2), categoryIndex]);
            const height = Math.max(6, Math.min(16, apiRef.size([0, 1])[1] * 0.55));
            if (!Number.isFinite(start[0]) || !Number.isFinite(end[0])) return null;
            return {
              type: 'rect',
              shape: {
                x: start[0],
                y: start[1] - height / 2,
                width: Math.max(2, end[0] - start[0]),
                height,
                r: 3,
              },
              style: apiRef.style(),
            };
          },
          encode: { x: [1, 2], y: 0 },
          data,
        },
      ],
    });
  });
}

/** 行点击后在对应行下方插入明细行；只重挂展开行，避免整表重建。 */
function applyEpisodeExpansion(tableNode, rows) {
  for (const node of [...tableNode.querySelectorAll('tr.detail-row')]) node.remove();
  for (const node of [...tableNode.querySelectorAll('tr.selected')]) node.classList.remove('selected');

  const rank = state.episode.expanded;
  if (!rank) return;
  const row = rows.find((item) => item.rank === rank);
  if (!row) return;

  const tr = tableNode.querySelector('tbody')?.querySelector(`tr[data-key="episode-${rank}"]`);
  if (!tr) return;
  tr.classList.add('selected');

  const cell = el('td', { colspan: String(tableNode.querySelectorAll('thead th').length) });
  cell.appendChild(renderEpisodeDetail(row));

  const detail = el('tr.detail-row', null, [cell]);
  tr.insertAdjacentElement('afterend', detail);
}

function renderEpisodeDetail(row) {
  const pollutants = row.pollutants || {};
  const entries = Object.keys(pollutants);
  const withUnit = (_name, value) => (isNum(value) ? `${formatFixed(value, 2)} μg/m³` : '—');

  const pollutantList = entries.length
    ? el('div.chip-row', null, entries.map((name) =>
        el('span', { class: `chip${name === row.primary_pollutant ? ' active' : ''}`, style: { cursor: 'default' } }, [
          el('span', { text: `${POLLUTANT_LABEL[name] || name} ${withUnit(name, pollutants[name])}` }),
        ]),
      ))
    : el('div.text-mute', { text: '该过程期间没有可用的污染物浓度明细。' });

  return el('div', { style: { display: 'flex', flexDirection: 'column', gap: '10px', padding: '12px', whiteSpace: 'normal' } }, [
    el('div.section-title', { text: `过程明细 · 第 ${row.rank} 次 · ${row.city_name}` }),
    el('div.grid.grid-2', null, [
      el('div', null, [
        el('div.card-sub', { text: '过程期间污染物平均浓度' }),
        pollutantList,
      ]),
      kvList([
        ['首要污染物', row.primary_pollutant || '无'],
        ['峰值时刻', formatDataTime(row.peak_time)],
        ['峰值 / 过程均值', `${formatFixed(row.peak_value, 1)} / ${formatFixed(row.mean_value, 1)}`],
        ['判定阈值', `${formatFixed(row.threshold, 1)}（${row.peak_level || '—'}）`],
        ['持续 / 超标小时', `${formatInt(row.duration_hours)} / ${formatInt(row.exceed_hours)} 小时`],
        ['平均风速', row.weather?.wind_speed_avg === null || row.weather?.wind_speed_avg === undefined ? '—' : `${formatFixed(row.weather.wind_speed_avg, 2)} km/h`],
        ['平均湿度', row.weather?.humidity_avg === null || row.weather?.humidity_avg === undefined ? '—' : `${formatFixed(row.weather.humidity_avg, 1)} %`],
        ['累计降水', row.weather?.precipitation_sum === null || row.weather?.precipitation_sum === undefined ? '—' : `${formatFixed(row.weather.precipitation_sum, 2)} mm`],
        ['静稳小时', row.weather?.stagnant_hours === null || row.weather?.stagnant_hours === undefined ? '—' : `${formatInt(row.weather.stagnant_hours)} 小时`],
      ]),
    ]),
  ]);
}

// ==================================================================
// 异常点检测
// ==================================================================

async function loadOutliers(ctx) {
  const panel = state.hosts?.outliers;
  if (!panel) return;
  panelLoading(panel, '正在检测异常点…');

  const params = {
    cities: requestedCities(),
    metric: state.outlier.metric,
    days: state.days,
    method: state.outlier.method,
    threshold: state.outlier.threshold,
  };

  let payload;
  try {
    payload = await api.insightsOutliers(params);
  } catch (error) {
    panelError(panel, error, () => loadOutliers(ctx));
    return;
  }

  if (!payload || payload.available === false) {
    panelUnavailable(panel, payload?.message, ctx);
    return;
  }

  state.outlier.payload = payload;
  renderOutliers(ctx);
}

function renderOutliers(ctx) {
  const panel = state.hosts?.outliers;
  const payload = state.outlier.payload;
  if (!panel || !payload) return;

  mergeMetric(payload.metric, payload.label, payload.unit);
  const unit = payload.unit || '';
  const byCity = payload.by_city || [];
  const points = payload.points || [];

  const worst = byCity[0] || null;
  const chartId = nextId('outlier-series');

  const columns = [
    { key: 'city_name', title: '城市' },
    { key: 'samples', title: '有效样本', align: 'right', render: (row) => formatInt(row.samples) },
    { key: 'flagged', title: '标记异常点', align: 'right', render: (row) => formatInt(row.flagged) },
    {
      key: 'flagged_ratio',
      title: '异常占比',
      align: 'right',
      render: (row) => el('span', {
        class: num(row.flagged_ratio) !== null && row.flagged_ratio >= 5 ? 'text-bad' : num(row.flagged_ratio) > 0 ? 'text-warn' : 'text-dim',
        text: formatPercent(row.flagged_ratio, 2),
      }),
    },
    { key: 'mean', title: '均值', align: 'right', render: (row) => fmt(row.mean, '', 2) },
    { key: 'std', title: '标准差', align: 'right', render: (row) => fmt(row.std, '', 2) },
    { key: 'max_z', title: '最大 |Z|', align: 'right', render: (row) => fmt(row.max_z, '', 2) },
    {
      key: 'top_points',
      title: '最异常时刻',
      render: (row) => {
        const top = (row.top_points || [])[0];
        return top ? `${formatDataTime(top.time, false)}（${fmt(top.value, '', 1)}）` : '—';
      },
    },
  ];

  const pointColumns = [
    { key: 'rank', title: '序号', align: 'right', render: (_row) => '' },
    { key: 'city_name', title: '城市' },
    { key: 'time', title: '时刻（城市本地）', render: (row) => formatDataTime(row.time) },
    { key: 'value', title: `数值${unit ? `（${unit}）` : ''}`, align: 'right', render: (row) => fmt(row.value, '', 2) },
    {
      key: 'z',
      title: '偏离分数 Z',
      align: 'right',
      render: (row) => el('span', { class: Math.abs(row.z) >= 4 ? 'text-bad' : 'text-warn', text: fmt(row.z, '', 2) }),
    },
  ];
  const rankedPoints = points.slice(0, 40).map((point, index) => ({ ...point, rank: index + 1 }));

  mount(
    panel.host,
    el('div.grid.grid-kpi', null, [
      kpiCard({
        label: '异常点占比',
        value: formatPercent(payload.flagged_ratio, 2),
        foot: `${formatInt(payload.flagged)} / ${formatInt(payload.samples)} 个逐小时记录`,
        tone: num(payload.flagged_ratio) !== null && payload.flagged_ratio >= 5 ? 'bad' : num(payload.flagged_ratio) > 0 ? 'warn' : 'ok',
      }),
      kpiCard({
        label: '异常最多的城市',
        value: worst?.city_name || '—',
        foot: worst ? `异常占比 ${formatPercent(worst.flagged_ratio, 2)} · 最大 |Z| ${fmt(worst.max_z, '', 2)}` : '无可用城市',
      }),
      kpiCard({
        label: '参与统计的城市',
        value: formatInt(byCity.length),
        unit: '个',
        foot: `近 ${formatInt(payload.window_days)} 天 · ${cityText()}`,
      }),
      kpiCard({
        label: '判定方法',
        value: payload.method === 'iqr' ? 'Tukey 围栏' : 'Z 分数',
        foot: payload.method === 'iqr' ? `围栏倍数 ${fmt(payload.threshold, '', 1)}` : `阈值 |Z| > ${fmt(payload.threshold, '', 1)}`,
      }),
    ]),
    card(`${payload.label || metricLabel(payload.metric)} 异常点时间分布`, el('div.chart', { id: chartId, style: { height: '360px' } }), {
      subtitle:
        `散点即接口标记出的异常时刻（全部城市合计，按时刻排序，最多 200 点）；` +
        `异常占比最高的城市为 ${worst?.city_name || '—'}（${worst ? formatPercent(worst.flagged_ratio, 2) : '—'}）。` +
        `横轴为城市本地时间（各城市时区不同，跨城市比较时请注意）。单位：${unit || '无量纲'}。`,
    }),
    card('逐城市异常统计', table(columns, byCity, { empty: '暂无逐城市异常统计' }), {
      subtitle: '按异常占比降序；占比高说明该城市窗口内波动或极端值明显。',
    }),
    card('异常时刻清单', table(pointColumns, rankedPoints, { empty: '窗口内没有标记出异常点' }), {
      subtitle: `按 |Z| 降序展示前 ${formatInt(rankedPoints.length)} 条（接口最多返回 200 条）`,
    }),
    note(
      payload.method === 'iqr'
        ? 'Tukey 围栏不假设分布形态，对长尾指标（PM2.5、AQI）更稳健，但会随窗口整体水平移动而改变标记结果。'
        : 'Z 分数的均值与标准差本身会被极端值拉偏；对长尾指标建议改用 Tukey 围栏方法复核。',
    ),
  );

  window.requestAnimationFrame(() => {
    const target = document.getElementById(chartId);
    if (!target || !target.isConnected) return;

    // 散点即接口返回的全部标记点（跨城市合并，已按 |Z| 降序取前 200），按时刻排序后绘制。
    const focus = points
      .map((point) => ({ ...point, stamp: localTime(point.time) }))
      .filter((point) => point.stamp !== null)
      .sort((a, b) => a.stamp - b.stamp);

    if (!focus.length) {
      renderChart(target, theme(), {
        emptyMessage: '窗口内没有标记出异常点：所选指标在当前方法下未出现显著偏离常态的时刻。',
      });
      return;
    }

    const flaggedData = focus.map((point) => [point.stamp, point.value]);
    const unitText = unit ? ` ${unit}` : '';

    renderChart(target, {
      ...theme(),
      grid: { left: 10, right: 22, top: 36, bottom: 8, containLabel: true },
      legend: { top: 0, right: 8, icon: 'roundRect', itemWidth: 10, itemHeight: 10, textStyle: { color: ink().dim, fontSize: 11.5 } },
      tooltip: {
        ...tooltipStyle('item'),
        formatter: (params) => {
          const stamp = params.value?.[0];
          const value = params.value?.[1];
          const z = params.data?.z;
          return [
            `${params.seriesName}`,
            `${formatDataTime(isoOf(stamp))}`,
            `${fmt(value, unitText, 2)}${isNum(z) ? ` · Z ${fmt(z, '', 2)}` : ''}`,
          ].join('<br />');
        },
      },
      xAxis: {
        type: 'time',
        axisLabel: { color: ink().mute, fontSize: 11, hideOverlap: true },
        axisLine: { lineStyle: { color: ink().grid } },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: {
        type: 'value',
        name: unit || '',
        nameTextStyle: { color: ink().mute, fontSize: 11 },
        axisLabel: { color: ink().mute, fontSize: 11 },
        splitLine: { lineStyle: { color: ink().grid, type: 'dashed' } },
      },
      series: [
        {
          name: '标记异常点',
          type: 'scatter',
          symbolSize: 9,
          data: flaggedData.map((item, index) => ({ value: item, z: focus[index].z })),
          itemStyle: { color: '#ef4444', borderColor: '#fecaca', borderWidth: 1 },
          z: 3,
        },
      ],
    });
  });
}

/** 本地时刻 → 与后端一致的裸时间字符串，用于与 points 列表对齐。 */
function isoOf(stamp) {
  if (!isNum(stamp)) return '';
  const date = new Date(stamp);
  const pad = (value) => String(value).padStart(2, '0');
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  );
}
