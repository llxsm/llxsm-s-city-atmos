/**
 * charts.js —— ECharts 封装。
 *
 * 统一处理三件事：
 * 1. 实例生命周期（创建 / 尺寸自适应 / 销毁），避免视图切换时的内存与监听泄漏；
 * 2. 深色主题与"无图表垃圾"的默认样式（去动画、细网格、顶部图例、十字准星提示）；
 * 3. ECharts 缺失或容器尺寸为 0 时的降级：在容器内给出内联提示，绝不抛错。
 *
 * 注意：ECharts 通过 CDN 的 `<script>` 全局引入，本模块不 import 图表库；
 * 只从 theme.js 取配色。
 */

import { accent, accentSoft, ink, seriesPalette } from '/shared/theme.js';

const CDN_HINT =
  '图表库 ECharts 未能加载（CDN：cdn.jsdelivr.net/npm/echarts@5.5.1）。' +
  '请检查网络或代理设置后刷新页面；本页其余数据仍可正常查看。';

/**
 * 图表序列配色，取自当前主题（强调色打头，其后为固定分类色）。
 * 需要按下标轮换颜色时用它 —— 不再导出静态 ``PALETTE``，避免出现两套配色。
 * 视图侧若也要取色，统一从 theme.js 取，不要在这里再开一个入口。
 */
function palette() {
  return seriesPalette();
}

function lib() {
  const echarts = typeof window !== 'undefined' ? window.echarts : undefined;
  return echarts && typeof echarts.init === 'function' ? echarts : null;
}

/** ECharts 是否可用。 */
export function isAvailable() {
  return lib() !== null;
}

// ==================================================================
// 实例管理
// ==================================================================

/** container -> echarts instance */
const registry = new Map();
/** 已重试次数：用于容忍 CDN 脚本尚未执行完成的极短窗口 */
const retries = new Map();
const MAX_RETRIES = 40;

let resizeBound = false;

function bindResize() {
  if (resizeBound) return;
  resizeBound = true;
  window.addEventListener('resize', () => {
    for (const chart of registry.values()) {
      if (chart && !chart.isDisposed?.()) chart.resize();
    }
  });
}

function showFallback(container, message) {
  if (!container) return null;
  container.textContent = '';
  const box = document.createElement('div');
  box.className = 'chart-fallback';
  box.style.height = '100%';
  box.textContent = message;
  container.appendChild(box);
  return null;
}

function hasSize(container) {
  const rect = container.getBoundingClientRect();
  return rect.width > 8 && rect.height > 8;
}

/**
 * 在容器上创建（或复用）一个 ECharts 实例。
 * 若 ECharts 未加载或容器不可见，返回 null 并输出内联提示。
 */
export function createChart(container) {
  if (!container) return null;
  const echarts = lib();
  if (!echarts) {
    // CDN 脚本可能仍在加载：短暂重试，超过上限才提示不可用。
    const attempt = retries.get(container) || 0;
    if (attempt < MAX_RETRIES) {
      retries.set(container, attempt + 1);
      if (!container.dataset.chartPending) {
        container.dataset.chartPending = '1';
        container.textContent = '';
        const wait = document.createElement('div');
        wait.className = 'chart-fallback';
        wait.style.height = '100%';
        wait.textContent = '正在加载图表库…';
        container.appendChild(wait);
      }
      window.setTimeout(() => {
        if (!container.isConnected) return;
        delete container.dataset.chartPending;
        createChart(container);
      }, 60);
      return null;
    }
    retries.delete(container);
    return showFallback(container, CDN_HINT);
  }
  retries.delete(container);

  const existing = registry.get(container);
  if (existing && !existing.isDisposed?.()) return existing;
  registry.delete(container);

  if (!hasSize(container)) {
    // 容器刚插入 DOM 时尺寸可能尚未计算，延后一帧再试，避免渲染成 0×0。
    window.requestAnimationFrame(() => {
      if (registry.has(container)) return;
      if (!container.isConnected) return;
      const chart = echarts.init(container, null, { renderer: 'canvas' });
      registry.set(container, chart);
      chart.resize();
    });
    return null;
  }

  const chart = echarts.init(container, null, { renderer: 'canvas' });
  registry.set(container, chart);
  bindResize();
  return chart;
}

/** 销毁单个实例。 */
export function disposeChart(container) {
  retries.delete(container);
  const chart = registry.get(container);
  if (!chart) return;
  registry.delete(container);
  if (!chart.isDisposed?.()) chart.dispose();
}

/** 销毁容器子树内的全部实例（视图卸载时调用）。 */
export function disposeCharts(root) {
  if (!root) return;
  for (const container of [...registry.keys()]) {
    if (root === container || root.contains?.(container)) disposeChart(container);
  }
  for (const container of [...retries.keys()]) {
    if (root === container || root.contains?.(container)) retries.delete(container);
  }
}

/** 返回容器内是否已经初始化过图表。 */
export function hasChart(container) {
  return registry.has(container);
}

// ==================================================================
// 主题与基础配置
// ==================================================================

/** 所有图表共享的深色主题。 */
export function theme() {
  return {
    color: palette(),
    backgroundColor: 'transparent',
    animation: false,
    textStyle: { color: ink().dim, fontFamily: 'inherit', fontSize: 12 },
    title: { textStyle: { color: ink().text, fontSize: 13, fontWeight: 500 } },
    grid: { left: 12, right: 18, top: 34, bottom: 8, containLabel: true },
    tooltip: tooltipStyle('axis'),
    legend: legendStyle(),
    categoryAxis: {
      axisLine: { lineStyle: { color: ink().grid } },
      axisTick: { show: false },
      axisLabel: { color: ink().mute, fontSize: 11 },
      splitLine: { show: false },
    },
    valueAxis: {
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: ink().mute, fontSize: 11 },
      splitLine: { lineStyle: { color: ink().grid, type: 'dashed' } },
      nameTextStyle: { color: ink().mute, fontSize: 11 },
    },
    radar: {
      axisName: { color: ink().dim, fontSize: 11 },
      splitLine: { lineStyle: { color: ink().splitLine } },
      splitArea: { areaStyle: { color: [ink().radarA, ink().radarB] } },
      axisLine: { lineStyle: { color: ink().grid } },
    },
    line: { symbol: 'none', smooth: true, lineStyle: { width: 2 } },
    bar: { itemStyle: { borderRadius: [3, 3, 0, 0] } },
  };
}

export function tooltipStyle(trigger = 'axis') {
  return {
    trigger,
    axisPointer: { type: trigger === 'axis' ? 'cross' : 'shadow', label: { backgroundColor: ink().axisPointer } },
    backgroundColor: ink().tooltipBg,
    borderColor: ink().border,
    borderWidth: 1,
    padding: [8, 11],
    textStyle: { color: ink().text, fontSize: 12 },
    confine: true,
  };
}

export function legendStyle(extra = {}) {
  return {
    top: 0,
    right: 8,
    icon: 'roundRect',
    itemWidth: 10,
    itemHeight: 10,
    itemGap: 14,
    textStyle: { color: ink().dim, fontSize: 11.5 },
    ...extra,
  };
}

export function labelStyle(extra = {}) {
  return { color: ink().dim, fontSize: 11.5, ...extra };
}

/** 需要缩放/平移的横轴（长周期趋势）时使用的 dataZoom 配置。 */
export function zoomStyle({ start = 0, end = 100 } = {}) {
  return [
    {
      type: 'inside',
      start,
      end,
      zoomOnMouseWheel: true,
      moveOnMouseMove: true,
    },
    {
      type: 'slider',
      start,
      end,
      height: 16,
      bottom: 4,
      borderColor: 'transparent',
      backgroundColor: ink().zoomBg,
      fillerColor: accentSoft(),
      handleStyle: { color: accent() },
      textStyle: { color: ink().mute, fontSize: 10 },
    },
  ];
}

/** 实况 / 预报分界线。 */
export function boundaryMarkLine(time, label = '预报起点') {
  if (!time) return null;
  return {
    silent: true,
    symbol: 'none',
    lineStyle: { color: '#f59e0b', type: 'dashed', width: 1.4 },
    label: {
      formatter: label,
      color: ink().warnInk,
      fontSize: 11,
      position: 'insideEndTop',
    },
    data: [{ xAxis: time }],
  };
}

// ==================================================================
// 常用图表构建器
// ==================================================================

/**
 * 折线图。
 * @param {{categories:string[], series:Array<{name:string,data:Array<number|null>,area?:boolean,color?:string,yAxisIndex?:number,markLine?:object,dashed?:boolean}>, yAxis?:Array|object, zoom?:boolean, height?:number}} config
 */
export function lineChart(config) {
  const { categories = [], series = [], yAxis = null, zoom = false, legend = true } = config;
  return {
    ...theme(),
    grid: { left: 10, right: 22, top: legend ? 36 : 18, bottom: zoom ? 36 : 8, containLabel: true },
    ...(legend ? { legend: legendStyle() } : { legend: { show: false } }),
    tooltip: tooltipStyle('axis'),
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: categories,
      axisLabel: { color: ink().mute, fontSize: 11, hideOverlap: true },
      axisLine: { lineStyle: { color: ink().grid } },
      axisTick: { show: false },
    },
    yAxis: yAxis
      ? Array.isArray(yAxis)
        ? yAxis
        : [yAxis]
      : [
          {
            type: 'value',
            axisLabel: { color: ink().mute, fontSize: 11 },
            splitLine: { lineStyle: { color: ink().grid, type: 'dashed' } },
          },
        ],
    dataZoom: zoom ? zoomStyle() : undefined,
    series: series.map((item) => ({
      name: item.name,
      type: 'line',
      data: item.data,
      smooth: item.smooth ?? true,
      symbol: 'none',
      yAxisIndex: item.yAxisIndex || 0,
      connectNulls: false,
      lineStyle: {
        width: item.area ? 2 : 1.8,
        type: item.dashed ? 'dashed' : 'solid',
        ...(item.color ? { color: item.color } : {}),
      },
      itemStyle: item.color ? { color: item.color } : undefined,
      areaStyle: item.area
        ? {
            color: item.color ? item.color : accent(),
            opacity: 0.12,
          }
        : undefined,
      markLine: item.markLine || undefined,
      emphasis: { focus: 'series' },
    })),
  };
}

/** 环形图（等级分布、状态分布等占比场景）。 */
export function donutChart(items, { centerLabel = '', centerValue = '', unit = '' } = {}) {
  const data = (items || []).filter((item) => Number(item.value) > 0);
  const total = data.reduce((sum, item) => sum + Number(item.value || 0), 0);
  return {
    ...theme(),
    tooltip: { ...tooltipStyle('item'), formatter: '{b}：{c}（{d}%）' },
    legend: { show: false },
    series: [
      {
        type: 'pie',
        radius: ['58%', '78%'],
        center: ['50%', '52%'],
        avoidLabelOverlap: true,
        padAngle: 1.2,
        itemStyle: { borderColor: ink().donutBorder, borderWidth: 1.5 },
        label: {
          show: true,
          color: ink().dim,
          fontSize: 11.5,
          formatter: '{b} {c}',
        },
        labelLine: { length: 8, length2: 8, lineStyle: { color: ink().grid } },
        data: data.map((item) => ({
          name: item.name,
          value: item.value,
          itemStyle: item.color ? { color: item.color } : undefined,
        })),
      },
    ],
    graphic: total
      ? [
          {
            type: 'text',
            left: 'center',
            top: '42%',
            style: {
              text: String(centerValue || total),
              fill: ink().text,
              fontSize: 22,
              fontWeight: 600,
              textAlign: 'center',
            },
          },
          {
            type: 'text',
            left: 'center',
            top: '58%',
            style: { text: centerLabel || unit || '合计', fill: ink().mute, fontSize: 11.5, textAlign: 'center' },
          },
        ]
      : [],
  };
}

/**
 * 雷达图。
 * @param {string[]} indicators 维度名称
 * @param {number} max 量纲上限（质量分固定 100）
 * @param {Array<{name:string, values:number[], color?:string, area?:boolean}>} series
 */
export function radarChart(indicators, max, series) {
  const colors = palette();
  return {
    ...theme(),
    tooltip: tooltipStyle('item'),
    legend: series.length > 1 ? legendStyle() : { show: false },
    radar: {
      indicator: indicators.map((name) => ({ name, max })),
      radius: '64%',
      center: ['50%', '54%'],
      axisName: { color: ink().dim, fontSize: 11.5 },
      splitLine: { lineStyle: { color: ink().splitLine } },
      splitArea: { areaStyle: { color: [ink().radarA, ink().radarB] } },
      axisLine: { lineStyle: { color: ink().grid } },
    },
    series: [
      {
        type: 'radar',
        symbolSize: 4,
        data: series.map((item, index) => {
          const color = item.color || colors[index % colors.length];
          return {
            name: item.name,
            value: item.values,
            lineStyle: { color, width: 2 },
            itemStyle: { color },
            areaStyle: item.area === false ? undefined : { color, opacity: 0.16 },
          };
        }),
      },
    ],
  };
}

/**
 * 柱状图。
 * @param {{categories:string[], series:Array<{name:string,data:number[],color?:string}>, horizontal?:boolean, max?:number, valueLabel?:boolean, legend?:boolean}} config
 */
export function barChart(config) {
  const { categories = [], series = [], horizontal = false, max = null, valueLabel = true, legend = false } = config;
  const colors = palette();
  const categoryAxis = {
    type: 'category',
    data: categories,
    axisLabel: { color: ink().mute, fontSize: 11, interval: 0, hideOverlap: true },
    axisLine: { lineStyle: { color: ink().grid } },
    axisTick: { show: false },
  };
  const valueAxis = {
    type: 'value',
    max: max || undefined,
    axisLabel: { color: ink().mute, fontSize: 11 },
    splitLine: { lineStyle: { color: ink().grid, type: 'dashed' } },
  };

  return {
    ...theme(),
    grid: { left: 10, right: 26, top: legend ? 34 : 16, bottom: 6, containLabel: true },
    ...(legend ? { legend: legendStyle() } : { legend: { show: false } }),
    tooltip: tooltipStyle('axis'),
    xAxis: horizontal ? valueAxis : categoryAxis,
    yAxis: horizontal ? categoryAxis : valueAxis,
    series: series.map((item, index) => ({
      name: item.name,
      type: 'bar',
      data: item.data,
      barMaxWidth: 22,
      itemStyle: {
        color: item.color || colors[index % colors.length],
        borderRadius: horizontal ? [0, 3, 3, 0] : [3, 3, 0, 0],
      },
      label: valueLabel
        ? {
            show: true,
            position: horizontal ? 'right' : 'top',
            color: ink().dim,
            fontSize: 11,
          }
        : { show: false },
    })),
  };
}

// ==================================================================
// 便捷渲染
// ==================================================================

/**
 * 在容器中渲染图表；容器、ECharts 任一不可用时输出提示。
 * @returns {object|null} ECharts 实例
 */
export function renderChart(container, option, { emptyMessage = '', onEvents = null } = {}) {
  if (!container) return null;
  if (emptyMessage) {
    container.textContent = '';
    const box = document.createElement('div');
    box.className = 'empty';
    box.style.height = '100%';
    const text = document.createElement('div');
    text.className = 'empty-hint';
    text.innerHTML = emptyMessage;
    box.appendChild(text);
    container.appendChild(box);
    return null;
  }

  const chart = createChart(container);
  if (!chart) return null;
  chart.setOption(option, { notMerge: true });
  chart.resize();
  if (onEvents) {
    for (const [event, handler] of Object.entries(onEvents)) chart.on(event, handler);
  }
  return chart;
}

export const CHART_CDN_HINT = CDN_HINT;
