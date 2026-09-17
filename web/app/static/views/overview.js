/**
 * overview.js —— 资产总览（治理门户首页）。
 *
 * 治理门户刻意保持精简，"资产总览"只回答四个问题：
 *   1. 有多少资产、多少字段（资产数 / 字段数 / 城市数 / 数据量）；
 *   2. 质量等级如何分布（等级环形图）与五维均分画像（雷达图）；
 *   3. 哪些资产超出了声明的 SLA（超期明细）；
 *   4. 最近跑了什么（运行记录摘要 + 最近运行与最近评分明细）。
 *
 * 相对拆分前的总览页，这里**移除**了：
 *   * 「治理体检 8 条线」面板（该能力已从产品中下线）；
 *   * 血缘健康度面板（血缘健康度改在「数据血缘」页内呈现）；
 *   * 业务域 / 数据分层分布表（属于目录探查，改由「资产目录」的筛选器承担）。
 */

import { api } from '/shared/api.js';
import { donutChart, radarChart, renderChart } from '/shared/charts.js';
import {
  badge,
  button,
  card,
  collectHint,
  DIMENSION_KEYS,
  DIMENSION_LABEL,
  el,
  emptyState,
  formatAuditTime,
  formatBytes,
  formatCompact,
  formatDuration,
  formatInt,
  GRADE_COLOR,
  GRADE_LABEL,
  GRADE_ORDER,
  gradeBadge,
  kpiCard,
  layerBadge,
  legendItem,
  mount,
  note,
  runStatusBadge,
  table,
} from '/shared/ui.js';

let relays = [];

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在加载资产总览…', hint: '汇总资产目录、质量评分与运行记录。' }));

  const [overview, runs, scores] = await Promise.all([
    api.overview().catch((error) => ({ __error: error })),
    api.opsRuns({ limit: 12 }).catch(() => ({ items: [] })),
    api.qualityScores({ limit: 12 }).catch(() => ({ items: [] })),
  ]);

  if (overview?.__error) {
    mount(
      container,
      card('资产总览', emptyState({
        title: '无法读取资产总览',
        hint: overview.__error.message,
        actions: [button('重新加载', { kind: 'primary', onClick: () => ctx.refresh() })],
      })),
    );
    return;
  }

  relays = [];
  const hasAssets = (overview.asset_count || 0) > 0;

  mount(
    container,
    hasAssets ? null : emptyState({
      title: '资产目录为空',
      hint: collectHint('资产目录由 config/*.yaml 定义，随服务启动幂等同步；若此处为空，请在「运行审计」页点击「同步元数据」。'),
      actions: [
        button('前往运行审计', { kind: 'primary', onClick: () => ctx.navigate('ops') }),
        button('刷新', { onClick: () => ctx.refresh() }),
      ],
    }),
    renderKpis(overview),
    renderChartsRow(overview),
    renderSlaAndRuns(overview, runs, scores, ctx),
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
// KPI
// ==================================================================

function renderKpis(overview) {
  const runs = overview.runs_24h || {};
  const successRate = runs.success_rate;
  const breach = overview.sla_breach_count || 0;

  return el('div.grid.grid-kpi', null, [
    kpiCard({
      label: '数据资产数',
      value: formatInt(overview.asset_count),
      unit: '项',
      foot: `字段 ${formatInt(overview.column_count)} 个`,
      hint: '资产目录中登记的受治理数据集数量',
    }),
    kpiCard({
      label: '登记字段数',
      value: formatInt(overview.column_count),
      unit: '个',
      foot: `平均每项 ${formatInt((overview.column_count || 0) / Math.max(1, overview.asset_count || 1))} 个字段`,
    }),
    kpiCard({
      label: '监测城市',
      value: formatInt(overview.city_count),
      unit: '个',
      foot: `启用城市 ${formatInt(overview.active_city_count)} 个`,
    }),
    kpiCard({
      label: '数据总行数',
      value: formatCompact(overview.total_rows),
      unit: '行',
      foot: `体积 ${formatBytes(overview.total_bytes)}`,
    }),
    kpiCard({
      label: 'SLA 超期资产',
      value: formatInt(breach),
      unit: '项',
      tone: breach > 0 ? 'bad' : 'ok',
      foot: breach > 0 ? '最新数据到达时间超出声明 SLA' : '全部在用资产均在 SLA 之内',
    }),
    kpiCard({
      label: '24h 运行成功率',
      value: successRate === null || successRate === undefined ? '—' : Number(successRate).toFixed(1),
      unit: '%',
      tone: successRate === null || successRate === undefined ? '' : successRate >= 95 ? 'ok' : successRate >= 80 ? 'warn' : 'bad',
      foot: `共 ${formatInt(runs.total)} 次运行 · 失败 ${formatInt(runs.failed)} · 部分成功 ${formatInt(runs.partial)}`,
    }),
  ]);
}

// ==================================================================
// 质量等级分布 + 五维画像
// ==================================================================

function renderChartsRow(overview) {
  const gradeBody = el('div.chart', { style: { height: '280px' } });
  const radarBody = el('div.chart', { style: { height: '280px' } });

  const gradeCard = card('质量等级分布', gradeBody, {
    subtitle: '按每个资产的最新评分等级统计',
    actions: [el('div.legend', null, GRADE_ORDER.map((key) => legendItem(GRADE_COLOR[key], `${key} ${GRADE_LABEL[key]}`)))],
  });

  const radarCard = card('五维质量画像', radarBody, { subtitle: '全部已评测资产的维度均分（满分 100）' });

  window.requestAnimationFrame(() => {
    const gradeItems = GRADE_ORDER
      .filter((key) => (overview.grade_distribution?.[key] || 0) > 0)
      .map((key) => ({
        name: `${key} ${GRADE_LABEL[key]}`,
        value: overview.grade_distribution[key],
        color: GRADE_COLOR[key],
      }));
    const evaluated = gradeItems.reduce((sum, item) => sum + item.value, 0);

    const donut = renderChart(
      gradeBody,
      donutChart(gradeItems, { centerLabel: '已评测资产', centerValue: String(evaluated) }),
      { emptyMessage: evaluated ? '' : '尚无质量评分记录，请在「数据质量」页触发一次评测。' },
    );

    const dimension = overview.quality_dimension_average || {};
    const hasDimension = DIMENSION_KEYS.some((key) => typeof dimension[key] === 'number');
    const values = DIMENSION_KEYS.map((key) => (typeof dimension[key] === 'number' ? Number(dimension[key]) : 0));
    const radar = renderChart(
      radarBody,
      radarChart(DIMENSION_KEYS.map((key) => DIMENSION_LABEL[key]), 100, [
        { name: '维度均分', values, color: '#3b82f6' },
      ]),
      { emptyMessage: hasDimension ? '' : '尚无质量评分记录，雷达图暂不可用。' },
    );

    relays.push(...[donut, radar].filter(Boolean));
  });

  return el('div.grid.grid-2', null, [gradeCard, radarCard]);
}

// ==================================================================
// SLA 超期 + 最近运行
// ==================================================================

function renderSlaAndRuns(overview, runs, scores, ctx) {
  const breaches = overview.sla_breaches || [];

  const slaBody = breaches.length
    ? table(
        [
          {
            key: 'name',
            title: '资产',
            render: (row) => el('div', null, [
              el('div', { text: row.name }),
              el('div.mono.text-mute', { text: row.key }),
            ]),
          },
          { key: 'layer', title: '分层', render: (row) => layerBadge(row.layer) },
          {
            key: 'delay_minutes',
            title: '延迟',
            align: 'right',
            render: (row) => el('span.text-bad', { text: formatDelay(row.delay_minutes) }),
          },
          {
            key: 'sla_minutes',
            title: 'SLA',
            align: 'right',
            render: (row) => (row.sla_minutes ? formatDelay(row.sla_minutes) : '未声明'),
          },
        ],
        breaches,
        { onRowClick: (row) => ctx.navigate('assets', [row.key]), empty: '无 SLA 超期资产' },
      )
    : emptyState({
        title: '无 SLA 超期资产',
        hint: '所有在用资产的最新数据到达时间均在声明的 SLA 之内。',
        compact: true,
      });

  const recentScores = scores?.items || [];
  const scoresBody = recentScores.length
    ? table(
        [
          { key: 'asset_key', title: '资产', render: (row) => el('span.mono', { text: row.asset_key || '—' }) },
          {
            key: 'overall',
            title: '总分',
            align: 'right',
            render: (row) => (typeof row.overall === 'number' ? row.overall.toFixed(2) : '—'),
          },
          { key: 'grade', title: '等级', render: (row) => gradeBadge(row.grade) },
          { key: 'scored_ago', title: '评测时间', render: (row) => row.scored_ago || formatAuditTime(row.scored_at) },
        ],
        recentScores,
        { empty: '暂无评分记录' },
      )
    : emptyState({
        title: '尚未执行质量评测',
        hint: '在「数据质量」页触发一次评测后，这里会显示最新评分。',
        compact: true,
      });

  const runItems = runs?.items || [];
  const runsBody = runItems.length
    ? table(
        [
          { key: 'started_at', title: '开始时间', render: (row) => formatAuditTime(row.started_at) },
          { key: 'job', title: '作业', render: (row) => el('span.mono', { text: row.job || '—' }) },
          { key: 'asset_key', title: '目标资产', render: (row) => el('span.mono.text-dim', { text: row.asset_key || '—' }) },
          { key: 'city_name', title: '城市', render: (row) => row.city_name || row.city_slug || '—' },
          { key: 'rows_written', title: '写入行数', align: 'right', render: (row) => formatInt(row.rows_written) },
          { key: 'duration_ms', title: '耗时', align: 'right', render: (row) => formatDuration(row.duration_ms) },
          { key: 'status', title: '状态', render: (row) => runStatusBadge(row.status) },
          {
            key: 'error_message',
            title: '错误信息',
            wrap: true,
            render: (row) => (row.error_message ? el('span.text-bad', { text: row.error_message }) : '—'),
          },
        ],
        runItems,
        { empty: '暂无运行记录' },
      )
    : emptyState({
        title: '暂无运行记录',
        hint: '采集由分析门户触发；运行完成后这里会显示逐作业的审计明细。',
        compact: true,
      });

  const meta = overview.meta || {};
  const metaCard = card('目录元数据', el('div', null, [
    table(
      [
        { key: 'term', title: '项目' },
        { key: 'value', title: '取值' },
      ],
      [
        { term: '目录同步时间', value: meta.bootstrapped_at ? formatAuditTime(meta.bootstrapped_at) : '—' },
        { term: '质量规则版本', value: meta.rules_version || '—' },
        { term: '登记资产数', value: meta.asset_count ? formatInt(Number(meta.asset_count)) : '—' },
        { term: '登记城市数', value: meta.city_count ? formatInt(Number(meta.city_count)) : '—' },
        { term: '统计生成时间', value: formatAuditTime(overview.generated_at) },
      ],
      { empty: '暂无目录元数据' },
    ),
    el('div', { style: { marginTop: '12px' } }, [
      badge(
        `待处理质量项 ${formatInt(overview.open_quality_issues)} 项`,
        (overview.open_quality_issues || 0) > 0 ? 'badge-warn' : 'badge-ok',
      ),
    ]),
    note('目录同步时间与运行记录时间为 UTC 审计时间，已按浏览器本地时区展示；数据本身的时间戳为城市本地时间。'),
  ]));

  return el('div', null, [
    el('div.grid.grid-sidebar', null, [
      card('SLA 超期明细', slaBody, { subtitle: '按延迟时长倒序，最多展示 10 项' }),
      metaCard,
    ]),
    el('div.grid.grid-sidebar', null, [
      card('最近运行记录', runsBody, {
        subtitle: '审计时间为 UTC，已转换为本地时区展示',
        actions: [button('查看全部', { size: 'sm', onClick: () => ctx.navigate('ops') })],
      }),
      card('最近质量评分', scoresBody, {
        subtitle: '最近一次评测的资产评分',
        actions: [button('质量详情', { size: 'sm', onClick: () => ctx.navigate('quality') })],
      }),
    ]),
  ]);
}

/** 分钟数人性化展示。 */
function formatDelay(minutes) {
  if (typeof minutes !== 'number' || Number.isNaN(minutes)) return '—';
  if (minutes < 60) return `${minutes.toFixed(0)} 分钟`;
  if (minutes < 60 * 24) return `${(minutes / 60).toFixed(1)} 小时`;
  return `${(minutes / 1440).toFixed(1)} 天`;
}
