/**
 * monitor.js —— 环境监测。
 *
 * 城市选择器 + 当前实况卡片 + 逐小时曲线（含实况/预报分界）+ 预警清单 + 排名。
 * 数据时间戳为**城市本地时间**（后端返回裸时间字符串），页面只做格式化，不做时区换算。
 */

import { api } from '/shared/api.js';
import { boundaryMarkLine, lineChart, renderChart } from '/shared/charts.js';
import { accent, accentSoft } from '/shared/theme.js';
import {
  ALERT_SEVERITY,
  badge,
  button,
  card,
  collectHint,
  el,
  emptyState,
  formatDataTime,
  formatFixed,
  formatInt,
  mount,
  note,
  select,
  shortDataTime,
  table,
} from '/shared/ui.js';

const HOURLY_METRICS = ['temperature_2m', 'apparent_temperature', 'european_aqi', 'pm2_5', 'pm10'];

let relays = [];
let state = {
  cities: [],
  selected: null,
  hours: 72,
  autoRotate: false,
};
let rotateTimer = null;

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在加载环境监测数据…', hint: '读取城市主数据与融合宽表的实况记录。' }));

  const [cityPayload, currentPayload, ranking] = await Promise.all([
    api.envCities().catch(() => ({ items: [] })),
    api.envCurrent().catch((error) => ({ __error: error, items: [] })),
    api.envRanking(10).catch(() => null),
  ]);

  state.cities = cityPayload?.items || [];
  const conditions = currentPayload?.items || [];

  if (!conditions.length) {
    mount(
      container,
      renderToolbar(ctx, currentPayload),
      card(
        '当前实况',
        emptyState({
          title: '暂无环境实况数据',
          hint: collectHint('本门户负责数据刷新，可在「数据刷新」页触发采集管道，完成后本页会自动刷新。'),
          actions: [
            button('前往数据刷新', { kind: 'primary', onClick: () => ctx.navigate('ops') }),
            button('刷新', { onClick: () => ctx.refresh() }),
          ],
        }),
      ),
    );
    return;
  }

  const selected = conditions.find((item) => item.city_slug === state.selected) || conditions[0];
  state.selected = selected.city_slug;

  const section = el('div', { style: { display: 'flex', flexDirection: 'column', gap: '16px' } });
  mount(container, section);

  renderBody(section, ctx, conditions, ranking);

  // 城市切换只重绘曲线区，避免整页闪烁
  state.onSelect = (slug) => {
    state.selected = slug;
    const target = conditions.find((item) => item.city_slug === slug);
    renderConditions(section, ctx, conditions);
    renderHourly(section, ctx, target || selected);
    renderAlerts(section, ctx, target || selected);
  };

  renderHourly(section, ctx, selected);
  renderAlerts(section, ctx, selected);
}

export function destroy() {
  relays = [];
  if (rotateTimer) {
    clearInterval(rotateTimer);
    rotateTimer = null;
  }
  state = { cities: [], selected: null, hours: 72, autoRotate: false };
}

// ==================================================================
// 工具栏
// ==================================================================

function renderToolbar(ctx, currentPayload) {
  const cityOptions = state.cities.map((city) => ({
    value: city.slug,
    label: `${city.name_zh}（${city.timezone || '未知时区'}）`,
  }));

  const citySelect = select(cityOptions, {
    value: state.selected || cityOptions[0]?.value || '',
    onChange: (value) => {
      state.selected = value;
      state.autoRotate = false;
      if (state.onSelect) state.onSelect(value);
    },
  });

  const hourSelect = select(
    [
      { value: '24', label: '最近 24 小时' },
      { value: '48', label: '最近 48 小时' },
      { value: '72', label: '最近 72 小时（默认）' },
      { value: '168', label: '最近 7 天' },
    ],
    {
      value: String(state.hours),
      onChange: (value) => {
        state.hours = Number(value);
        ctx.refresh();
      },
    },
  );

  return card('监测范围', el('div.toolbar', null, [
    el('div.field', null, [el('label', { text: '城市' }), citySelect]),
    el('div.field', null, [el('label', { text: '时间窗口' }), hourSelect]),
    el('div.field', null, [
      el('label', { text: '操作' }),
      el('div.card-actions', null, [
        button('刷新数据', { kind: 'primary', onClick: () => ctx.refresh() }),
        button('多城市对比', { onClick: () => ctx.navigate('compare') }),
      ]),
    ]),
  ]), {
    subtitle: currentPayload?.note || '时间戳为城市本地时间，未做时区换算',
    actions: [badge(`${formatInt(currentPayload?.total || 0)} 个城市有实况`, 'badge-accent')],
  });
}

// ==================================================================
// 主体
// ==================================================================

function renderBody(section, ctx, conditions, ranking) {
  const rankHost = el('div', { id: 'monitor-rank-host' });

  mount(
    section,
    renderToolbar(ctx, { total: conditions.length, note: null }),
    el('div', { id: 'monitor-conditions' }),
    el('div', { id: 'monitor-hourly' }),
    el('div', { id: 'monitor-alerts' }),
    rankHost,
  );

  renderConditions(section, ctx, conditions);
  rankHost.replaceWith(renderRankings(ranking));
}

function renderConditions(section, ctx, conditions) {
  const host = section.querySelector('#monitor-conditions');
  if (!host) return;

  const grid = el('div.cond-grid', null, conditions.map((item) => conditionCard(item, ctx)));

  mount(
    host,
    card('当前实况', grid, {
      subtitle: '优先展示最新实况记录；若该城市仅有预报数据，则退化为最新预报值',
      actions: [el('span.text-mute', { text: `共 ${conditions.length} 个城市` })],
    }),
  );
}

function conditionCard(item, ctx) {
  const isForecast = item.is_forecast === 1;
  const aqiColor = item.aqi_color || '#64748b';

  const head = el('div.cond-head', null, [
    el('div', null, [
      el('div.cond-city', { text: item.city_name || item.city_slug }),
      el('div.cond-time', {
        text: `${formatDataTime(item.time)} · 城市本地时间`,
      }),
    ]),
    el('div', { style: { textAlign: 'right' } }, [
      el('span.aqi-pill', {
        style: { background: aqiColor },
        text: item.aqi_level || 'AQI 未知',
      }),
      el('div.cond-time', { text: `${isForecast ? '预报' : '实况'}数据` }),
    ]),
  ]);

  const tempBlock = el('div', null, [
    el('div.cond-temp', null, [
      formatFixed(item.temperature_2m, 1),
      el('span', { text: ' °C', style: { fontSize: '14px', fontWeight: '500', color: '#a9b5c9' } }),
    ]),
    el('div.cond-time', {
      text: `体感 ${formatFixed(item.apparent_temperature, 1)} °C · ${item.weather_label || '天气未知'}`,
    }),
  ]);

  const metrics = [
    ['相对湿度', `${formatFixed(item.relative_humidity_2m, 0)} %`],
    ['风速', `${formatFixed(item.wind_speed_10m, 1)} km/h`],
    ['降水', `${formatFixed(item.precipitation, 1)} mm`],
    ['气压', `${formatFixed(item.pressure_msl, 0)} hPa`],
    ['欧洲 AQI', item.european_aqi === null || item.european_aqi === undefined ? '—' : formatFixed(item.european_aqi, 0)],
    ['美国 AQI', item.us_aqi === null || item.us_aqi === undefined ? '—' : formatFixed(item.us_aqi, 0)],
    ['PM2.5', `${formatFixed(item.pm2_5, 1)} μg/m³`],
    ['PM10', `${formatFixed(item.pm10, 1)} μg/m³`],
  ];

  const meta = [
    ['首要污染物', item.primary_pollutant || '无'],
    ['舒适度指数', item.comfort_index === null || item.comfort_index === undefined ? '—' : formatFixed(item.comfort_index, 0)],
    ['通风指数', item.ventilation_index === null || item.ventilation_index === undefined ? '—' : formatFixed(item.ventilation_index, 0)],
    ['静稳天气', item.is_stagnant === 1 ? '是（易累积）' : item.is_stagnant === 0 ? '否' : '—'],
  ];

  const alerts = item.alerts || [];

  const node = el('div.cond', {
    class: item.city_slug === state.selected ? 'selected' : '',
    style: state.selected === item.city_slug
      ? { borderColor: accent(), boxShadow: `0 0 0 2px ${accentSoft()}` }
      : null,
    onClick: () => {
      state.selected = item.city_slug;
      if (state.onSelect) state.onSelect(item.city_slug);
    },
  }, [
    head,
    tempBlock,
    el('div.cond-metrics', null, metrics.map(([label, value]) =>
      el('div.m', null, [el('span', { text: label }), el('span', { text: value })]),
    )),
    el('div.cond-metrics', null, meta.map(([label, value]) =>
      el('div.m', null, [el('span', { text: label }), el('span', { text: String(value) })]),
    )),
    alerts.length
      ? el('div.chip-row', null, alerts.slice(0, 3).map((alert) => {
          const meta2 = ALERT_SEVERITY[alert.severity] || ALERT_SEVERITY.info;
          return badge(`${alert.name} ${formatFixed(alert.value, 1)}${alert.unit || ''}`, meta2.cls, alert.advice || '');
        }))
      : el('div.text-mute', { text: '当前无触发预警', style: { fontSize: '11.5px' } }),
    item.health_advice ? el('div.cond-time', { text: item.health_advice }) : null,
    el('div.card-actions', null, [
      button('查看历史趋势', {
        size: 'sm',
        onClick: (event) => {
          event.stopPropagation();
          ctx.navigate('trend');
        },
      }),
      button('加入多城市对比', {
        size: 'sm',
        onClick: (event) => {
          event.stopPropagation();
          ctx.navigate('compare');
        },
      }),
    ]),
  ]);

  return node;
}

// ==================================================================
// 逐小时曲线
// ==================================================================

function renderHourly(section, ctx, condition) {
  const host = section.querySelector('#monitor-hourly');
  if (!host) return;

  const slug = condition?.city_slug;
  const cityName = condition?.city_name || slug;

  const weatherBox = el('div.chart', { style: { height: '300px' } });
  const airBox = el('div.chart', { style: { height: '300px' } });

  mount(
    host,
    el('div.grid.grid-2', null, [
      card('气温与体感温度', weatherBox, { subtitle: `${cityName} · 逐小时 · 城市本地时间` }),
      card('空气质量：AQI 与颗粒物', airBox, { subtitle: `${cityName} · 逐小时 · 城市本地时间` }),
    ]),
    note('图上虚线为「实况 / 预报」分界时刻；分界右侧为数值预报结果，参考时请注意其不确定性。'),
  );

  if (!slug) return;

  loadHourly(slug, cityName, weatherBox, airBox);
}

async function loadHourly(slug, cityName, weatherBox, airBox) {
  let payload;
  try {
    payload = await api.envHourly({ city: slug, hours: state.hours, metrics: HOURLY_METRICS });
  } catch (error) {
    const message = `逐小时数据读取失败：${error.message}`;
    mount(weatherBox, emptyState({ title: '曲线不可用', hint: message, compact: true }));
    airBox.textContent = '';
    return;
  }

  const points = payload?.points || [];
  if (!points.length) {
    const empty = '该城市在当前时间窗口内没有逐小时记录，请先执行采集管道。';
    mount(weatherBox, emptyState({ title: '暂无逐小时数据', hint: empty, compact: true }));
    airBox.textContent = '';
    return;
  }

  const categories = points.map((point) => shortDataTime(point.time));
  const boundary = payload?.boundary?.time ? shortDataTime(payload.boundary.time) : null;

  window.requestAnimationFrame(() => {
    if (!weatherBox.isConnected) return; // 视图已切换，跳过渲染
    const weatherChart = renderChart(weatherBox, lineChart({
      categories,
      series: [
        { name: '气温 °C', data: points.map((point) => num(point.temperature_2m)), color: '#f59e0b', area: true, markLine: boundaryMarkLine(boundary) },
        { name: '体感温度 °C', data: points.map((point) => num(point.apparent_temperature)), color: '#fb923c', dashed: true },
      ],
    }));

    const airChart = renderChart(airBox, lineChart({
      categories,
      series: [
        { name: '欧洲 AQI', data: points.map((point) => num(point.european_aqi)), color: '#a78bfa', markLine: boundaryMarkLine(boundary) },
        { name: 'PM2.5 μg/m³', data: points.map((point) => num(point.pm2_5)), color: '#ef4444' },
        { name: 'PM10 μg/m³', data: points.map((point) => num(point.pm10)), color: '#38bdf8' },
      ],
    }));

    relays.push(...[weatherChart, airChart].filter(Boolean));
    if (boundary === null) {
      const host = weatherBox.parentElement;
      if (host && !host.querySelector('.chart-note.boundary-note')) {
        const extra = note('当前窗口内全部为同一类型数据（全部实况或全部预报），图上无分界点。');
        extra.classList.add('boundary-note');
        host.appendChild(extra);
      }
    }
  });
}

// ==================================================================
// 预警
// ==================================================================

function renderAlerts(section, ctx, condition) {
  const host = section.querySelector('#monitor-alerts');
  if (!host) return;

  const alerts = condition?.alerts || [];
  if (!alerts.length) {
    mount(host, card('环境预警', emptyState({
      title: '当前无生效预警',
      hint: `${condition?.city_name || '所选城市'} 的各项指标均未触发阈值规则。`,
      compact: true,
    })));
    return;
  }

  mount(host, card('环境预警', el('div.alert-list', null, alerts.map((alert) => {
    const meta = ALERT_SEVERITY[alert.severity] || ALERT_SEVERITY.info;
    return el('div.alert-item', null, [
      el('div', { class: `alert-sev ${alert.severity}` }),
      el('div.alert-main', null, [
        el('div.alert-title', null, [
          badge(meta.label, meta.cls),
          el('span', { text: alert.name || alert.key }),
          el('span.text-mute', {
            text: `${formatFixed(alert.value, 1)}${alert.unit || ''} ${alert.operator || '≥'} 阈值 ${formatFixed(alert.threshold, 1)}${alert.unit || ''}`,
          }),
        ]),
        el('div.alert-advice', { text: alert.advice || '' }),
      ]),
    ]);
  })), { subtitle: `${condition?.city_name || ''} · ${formatDataTime(condition?.time)}` }));
}

// ==================================================================
// 排名
// ==================================================================

function renderRankings(ranking) {
  if (!ranking) {
    return card('排名', emptyState({ title: '暂无排名数据', compact: true }));
  }

  const columns = [
    { key: 'rank', title: '#', align: 'right', render: (_row) => '' },
    { key: 'city_name', title: '城市' },
    { key: 'european_aqi', title: 'AQI', align: 'right', render: (row) => formatFixed(row.european_aqi, 0) },
    { key: 'aqi_level', title: '等级', render: (row) => el('span', {
        class: 'aqi-pill',
        style: { background: row.aqi_color || '#64748b' },
        text: row.aqi_level || '—',
      }) },
    { key: 'pm2_5', title: 'PM2.5', align: 'right', render: (row) => formatFixed(row.pm2_5, 1) },
    { key: 'temperature_2m', title: '气温 °C', align: 'right', render: (row) => formatFixed(row.temperature_2m, 1) },
    { key: 'weather_label', title: '天气', render: (row) => row.weather_label || '—' },
  ];

  const withRank = (items) => (items || []).map((item, index) => ({ ...item, rank: index + 1 }));

  return el('div.grid.grid-4.rank-table', null, [
    card('空气质量最差', table(columns, withRank(ranking.worst_air), { empty: '暂无数据' })),
    card('空气质量最佳', table(columns, withRank(ranking.best_air), { empty: '暂无数据' })),
    card('气温最高', table(columns, withRank(ranking.hottest), { empty: '暂无数据' })),
    card('降水最多', table(columns, withRank(ranking.wettest), { empty: '暂无数据' })),
  ]);
}

function num(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}
