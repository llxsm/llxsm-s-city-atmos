/**
 * digest.js —— 分析总览（分析门户首页）。
 *
 * 一次取三个接口拼出"今天该看什么"：
 *  1. `GET /api/insights/digest` —— 考核概况 + 污染过程 Top5 + 日内规律；
 *  2. `GET /api/overview`        —— 数据覆盖规模（行数 / 城市 / 覆盖时段）；
 *  3. `GET /api/env/current`     —— 城市当前实况。
 *
 * 三条数据线互不依赖，任一失败都不影响其余区块；平台完全无数据时
 * 每个区块各自给出中文空状态，页面不抛错。
 *
 * 时间口径：接口返回的 `time` / `date` 为城市本地裸时间字符串，本页只做格式化，
 * 不做任何时区换算；审计时间（带 Z）才转换为浏览器本地时区。
 */

import { api, analysisExportUrl } from '/shared/api.js';
import { lineChart, renderChart } from '/shared/charts.js';
import {
  badge,
  button,
  card,
  collectHint,
  el,
  emptyState,
  formatAuditTime,
  formatCompact,
  formatDataTime,
  formatFixed,
  formatInt,
  kpiCard,
  mount,
  table,
} from '/shared/ui.js';

/** 六个新分析页面的入口卡片（与 portal.config.js 中的路由一一对应）。 */
const QUICK_LINKS = [
  { route: 'distribution', title: '统计与分布', question: '数据长什么样？集中趋势、离散程度与长尾在哪？' },
  { route: 'profile', title: '时段规律', question: '一天之内什么时候最脏？工作日与周末有差别吗？' },
  { route: 'episodes', title: '污染过程', question: '共有几次污染过程？何时开始、持续多久、峰值多少？' },
  { route: 'relationship', title: '成因分析', question: '风速、湿度、降水对污染的影响有多大？' },
  { route: 'compliance', title: '达标考核', question: '优良天比例达标了吗？哪些城市拖了后腿？' },
  { route: 'clustering', title: '城市聚类', question: '哪些城市环境特征接近？可以归为一类管理？' },
];

const DIGEST_DAYS = 30;

let relays = [];

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在汇总分析摘要…', hint: '读取考核概况、污染过程与日内规律。' }));

  const [digest, overview, current] = await Promise.all([
    api.insightsDigest({ days: DIGEST_DAYS }).catch((error) => ({ __error: error })),
    api.overview().catch(() => null),
    api.envCurrent().catch(() => null),
  ]);

  relays = [];
  const conditions = current?.items || [];

  mount(
    container,
    renderHeader(digest, overview, ctx),
    renderKpis(digest, overview, conditions),
    renderEpisodesAndProfile(digest, ctx),
    renderConditions(conditions, ctx),
    renderQuickLinks(ctx),
    renderCoverage(overview),
  );

  // 卡片布局完成后图表容器尺寸才最终确定，这里补一次尺寸修正
  window.requestAnimationFrame(() => {
    for (const chart of relays) {
      if (chart && !chart.isDisposed?.()) chart.resize();
    }
  });
}

export function destroy() {
  relays = [];
}

// ==================================================================
// 头部
// ==================================================================

function renderHeader(digest, overview, ctx) {
  const failed = digest?.__error;
  const messages = Array.isArray(digest?.messages) ? digest.messages : [];
  const hint = failed
    ? `分析摘要接口调用失败（${failed.message}），下方各区块仍会独立加载。`
    : messages.length
      ? messages.join('；')
      : `统计窗口：近 ${formatInt(digest?.window_days || DIGEST_DAYS)} 天；所有分析在数据不足时给出说明而不是空图表。`;

  return card('分析总览', el('div', null, [
    el('div', { class: 'text-dim', text: '面向业务分析：先看结论，再跳转到对应的分析页面下钻。' }),
    el('div', { class: 'text-mute', style: { marginTop: '6px', fontSize: '12px' }, text: hint }),
    el('div.card-actions', { style: { marginTop: '12px' } }, [
      button('刷新摘要', { kind: 'primary', size: 'sm', onClick: () => ctx.refresh() }),
      button('实时监测', { size: 'sm', onClick: () => ctx.navigate('monitor') }),
      button('数据刷新', { size: 'sm', onClick: () => ctx.navigate('ops') }),
      overview?.has_data === false
        ? button('导出考核报表', { size: 'sm', disabled: true, title: '暂无数据可导出' })
        : button('导出考核报表', {
            size: 'sm',
            title: '下载近 90 天达标考核报表（CSV）',
            onClick: () => window.open(analysisExportUrl('compliance', { days: 90 }), '_blank'),
          }),
    ]),
  ]), {
    subtitle: overview?.generated_at ? `数据覆盖统计生成于 ${formatAuditTime(overview.generated_at)}（本地时区）` : '',
  });
}

// ==================================================================
// KPI
// ==================================================================

function renderKpis(digest, overview, conditions) {
  const compliance = digest?.compliance || {};
  const episodes = digest?.episodes || {};
  const goodRatio = compliance.good_day_ratio;
  const episodeCount = episodes.count;
  const cities = overview?.city_count ?? conditions.length;

  return el('div.grid.grid-kpi', null, [
    kpiCard({
      label: '监测城市',
      value: formatInt(cities || 0),
      unit: '个',
      foot: overview?.dataset_count ? `覆盖 ${formatInt(overview.dataset_count)} 个数据集` : '来自环境融合宽表',
      hint: '当前有可用数据的城市数量',
    }),
    kpiCard({
      label: '数据行数',
      value: formatCompact(overview?.total_rows),
      unit: '行',
      foot: windowLabel(overview),
      hint: '数据湖中可用于分析的记录总量',
    }),
    kpiCard({
      label: '优良天比例',
      value: goodRatio === null || goodRatio === undefined ? '—' : formatFixed(goodRatio, 1),
      unit: '%',
      tone: ratioTone(goodRatio, compliance.target_ratio),
      foot: goodRatio === null || goodRatio === undefined
        ? '需要日汇总数据才能考核'
        : `目标 ${formatFixed(compliance.target_ratio, 0)}% · 最好 ${compliance.best_city || '—'} / 最差 ${compliance.worst_city || '—'}`,
    }),
    kpiCard({
      label: '污染过程次数',
      value: formatInt(episodeCount),
      unit: '次',
      tone: episodeCount ? 'warn' : 'ok',
      foot: episodeCount
        ? `累计超标 ${formatInt(episodes.total_hours)} 小时 · 最重 ${episodes.worst_city || '—'}`
        : '窗口内未识别到连续超标过程',
    }),
    kpiCard({
      label: '平台平均 AQI',
      value: compliance.aqi_avg === null || compliance.aqi_avg === undefined ? '—' : formatFixed(compliance.aqi_avg, 1),
      foot: `按日均值统计 · 近 ${formatInt(digest?.window_days || DIGEST_DAYS)} 天`,
    }),
    kpiCard({
      label: '日内波动最大',
      value: digest?.profile?.most_volatile || '—',
      foot: '按日内小时均值振幅排序',
    }),
  ]);
}

function ratioTone(ratio, target) {
  if (typeof ratio !== 'number') return '';
  const goal = typeof target === 'number' ? target : 80;
  if (ratio >= goal) return 'ok';
  if (ratio >= goal - 10) return 'warn';
  return 'bad';
}

function windowLabel(overview) {
  const windows = overview?.analysis_windows || {};
  const parts = [];
  if (windows.hourly_days) parts.push(`逐小时 ${formatInt(windows.hourly_days)} 天`);
  if (windows.daily_days) parts.push(`逐日 ${formatInt(windows.daily_days)} 天`);
  if (windows.archive_days) parts.push(`归档 ${formatInt(windows.archive_days)} 天`);
  return parts.length ? parts.join(' · ') : '可分析窗口未知';
}

// ==================================================================
// 污染过程 + 日内规律
// ==================================================================

function renderEpisodesAndProfile(digest, ctx) {
  return el('div.grid.grid-sidebar', null, [
    renderEpisodes(digest, ctx),
    renderProfile(digest),
  ]);
}

function renderEpisodes(digest, ctx) {
  const episodes = digest?.episodes || {};
  const items = episodes.top || [];

  if (!items.length) {
    return card('重点污染过程', emptyState({
      title: digest?.__error ? '污染过程暂不可用' : '窗口内没有污染过程',
      hint: digest?.__error
        ? digest.__error.message
        : collectHint('污染过程按连续超标时段合并识别；窗口内全部达标时本表为空。'),
      actions: [button('查看污染过程分析', { kind: 'primary', onClick: () => ctx.navigate('episodes') })],
      compact: true,
    }), { subtitle: '按峰值 AQI 倒序，最多 5 条' });
  }

  return card('重点污染过程', table(
    [
      {
        key: 'city_name',
        title: '城市',
        render: (row) => el('div', null, [
          el('div', { text: row.city_name || row.city_slug || '—' }),
          el('div.mono.text-mute', { text: row.city_slug || '' }),
        ]),
      },
      {
        key: 'start',
        title: '起止',
        render: (row) => el('div', null, [
          el('div', { text: formatDataTime(row.start) }),
          el('div.text-mute', { text: `至 ${formatDataTime(row.end)}`, style: { fontSize: '11.5px' } }),
        ]),
      },
      { key: 'duration_hours', title: '持续', align: 'right', render: (row) => `${formatInt(row.duration_hours)} 小时` },
      {
        key: 'peak_value',
        title: '峰值 AQI',
        align: 'right',
        render: (row) => el('span', { class: 'text-bad', text: formatFixed(row.peak_value, 0) }),
      },
      { key: 'peak_level', title: '峰值等级', render: (row) => badge(row.peak_level || '—', levelClass(row.peak_level)) },
      { key: 'primary_pollutant', title: '首要污染物', render: (row) => row.primary_pollutant || '—' },
      { key: 'severity', title: '严重度', render: (row) => badge(row.severity || '—', severityClass(row.severity)) },
    ],
    items,
    { empty: '暂无污染过程' },
  ), {
    subtitle: '连续超标时段合并后的结果，点击下方按钮查看完整参数',
    actions: [button('查看全部', { size: 'sm', onClick: () => ctx.navigate('episodes') })],
  });
}

function levelClass(level) {
  if (!level) return 'badge-mute';
  if (level.includes('严重') || level.includes('重度')) return 'badge-bad';
  if (level.includes('中度')) return 'badge-warn';
  if (level.includes('轻度')) return 'badge-info';
  return 'badge-ok';
}

function severityClass(severity) {
  if (severity === '严重') return 'badge-bad';
  if (severity === '较重') return 'badge-warn';
  if (severity === '中等') return 'badge-info';
  return 'badge-mute';
}

function renderProfile(digest) {
  const profile = digest?.profile || {};
  const rows = profile.rows || [];
  const chartBox = el('div.chart', { style: { height: '300px' } });

  const node = card('日内规律', chartBox, {
    subtitle: '各城市逐小时均值中振幅最大的前 5 个城市',
    actions: [badge(profile.most_volatile ? `波动最大：${profile.most_volatile}` : '暂无画像', 'badge-accent')],
  });

  if (!rows.length) {
    window.requestAnimationFrame(() => {
      mount(chartBox, emptyState({
        title: '暂无日内规律数据',
        hint: '需要逐小时数据才能计算 24 小时均值曲线。',
        compact: true,
      }));
    });
    return node;
  }

  const hours = Array.from({ length: 24 }, (_item, index) => `${String(index).padStart(2, '0')}:00`);

  window.requestAnimationFrame(() => {
    if (!chartBox.isConnected) return;
    const chart = renderChart(chartBox, lineChart({
      categories: hours,
      series: rows.map((row) => ({
        name: row.city_name || row.city_slug,
        data: (row.values || []).map((value) => (typeof value === 'number' ? value : null)),
      })),
    }));
    if (chart) relays.push(chart);
  });

  return node;
}

// ==================================================================
// 当前实况
// ==================================================================

function renderConditions(conditions, ctx) {
  if (!conditions.length) {
    return card('城市当前实况', emptyState({
      title: '暂无环境实况数据',
      hint: collectHint('采集完成后本页会自动刷新。'),
      actions: [
        button('前往数据刷新', { kind: 'primary', onClick: () => ctx.navigate('ops') }),
        button('刷新', { onClick: () => ctx.refresh() }),
      ],
      compact: true,
    }));
  }

  const shown = conditions.slice(0, 8);
  return card('城市当前实况', el('div.cond-grid', null, shown.map((item) => el('div.cond', null, [
    el('div.cond-head', null, [
      el('div', null, [
        el('div.cond-city', { text: item.city_name || item.city_slug }),
        el('div.cond-time', { text: `${formatDataTime(item.time)} · 城市本地时间` }),
      ]),
      el('span.aqi-pill', { style: { background: item.aqi_color || '#64748b' }, text: item.aqi_level || 'AQI 未知' }),
    ]),
    el('div.cond-temp', null, [
      formatFixed(item.temperature_2m, 1),
      el('span', { text: ' °C', style: { fontSize: '14px', fontWeight: '500', color: '#a9b5c9' } }),
    ]),
    el('div.cond-metrics', null, [
      ['AQI', item.european_aqi === null || item.european_aqi === undefined ? '—' : formatFixed(item.european_aqi, 0)],
      ['PM2.5', `${formatFixed(item.pm2_5, 1)} μg/m³`],
      ['相对湿度', `${formatFixed(item.relative_humidity_2m, 0)} %`],
      ['风速', `${formatFixed(item.wind_speed_10m, 1)} km/h`],
    ].map(([label, value]) => el('div.m', null, [el('span', { text: label }), el('span', { text: value })]))),
    el('div.cond-time', { text: item.health_advice || `${item.data_kind || ''}数据` }),
  ]))), {
    subtitle: `共 ${formatInt(conditions.length)} 个城市有实况，此处展示空气质量最差的前 ${shown.length} 个`,
    actions: [button('进入环境监测', { size: 'sm', onClick: () => ctx.navigate('monitor') })],
  });
}

// ==================================================================
// 快捷入口
// ==================================================================

function renderQuickLinks(ctx) {
  return el('div.grid.grid-3', null, QUICK_LINKS.map((item) => card(item.title, el('div', null, [
    el('div.text-dim', { text: item.question, style: { minHeight: '44px' } }),
    el('div.card-actions', { style: { marginTop: '10px' } }, [
      button('打开', { kind: 'primary', size: 'sm', onClick: () => ctx.navigate(item.route) }),
    ]),
  ]), { subtitle: ctx.url(item.route), class: 'quick-link' })));
}

// ==================================================================
// 数据覆盖
// ==================================================================

function renderCoverage(overview) {
  if (!overview) {
    return card('数据覆盖', emptyState({ title: '数据覆盖信息读取失败', hint: '接口 /api/overview 暂时不可用。', compact: true }));
  }

  const datasets = overview.datasets || [];
  if (!datasets.length) {
    return card('数据覆盖', emptyState({
      title: '暂无数据集记录',
      hint: '资产目录为空或尚未采集，覆盖统计暂不可用。',
      compact: true,
    }));
  }

  return card('数据覆盖', table(
    [
      { key: 'name', title: '数据集', render: (row) => el('div', null, [el('div', { text: row.name || row.asset_key }), el('div.mono.text-mute', { text: row.asset_key })]) },
      { key: 'granularity', title: '粒度', render: (row) => row.granularity || '—' },
      { key: 'row_count', title: '行数', align: 'right', render: (row) => formatCompact(row.row_count) },
      { key: 'city_count', title: '城市', align: 'right', render: (row) => formatInt(row.city_count) },
      { key: 'latest_data_time', title: '最新数据时间', render: (row) => (row.latest_data_time ? formatDataTime(row.latest_data_time, true) : '—') },
    ],
    datasets,
    { empty: '暂无数据集' },
  ), {
    subtitle: `合计 ${formatCompact(overview.total_rows)} 行 · ${formatInt(overview.city_count)} 个城市`,
    actions: [el('span.text-mute', { text: overview.availability?.message || '' })],
  });
}
