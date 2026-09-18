/**
 * quality.js —— 数据质量。
 *
 * 结构：
 *  1. 平台级 KPI 与五维均分柱状图、等级分布；
 *  2. 每个资产的最新评分表（可排序、可按等级筛选）；
 *  3. 点击资产 → 通过 `GET /api/quality/evaluate/{key}` 拿到最完整的评测报告
 *     （维度分 + 逐项检查明细），并叠加评分历史趋势；
 *  4. 「触发评测」按钮调用 `POST /api/quality/evaluate`，随后轮询任务状态。
 */

import { api } from '/shared/api.js';
import { barChart, lineChart, radarChart, renderChart } from '/shared/charts.js';
import { accent } from '/shared/theme.js';
import { requestTaskRefresh, waitForTask, taskKindLabel } from '/shared/tasks.js';
import {
  badge,
  button,
  card,
  checkStatusBadge,
  collectHint,
  DIMENSION_KEYS,
  DIMENSION_LABEL,
  el,
  emptyState,
  formatAuditTime,
  formatFixed,
  formatInt,
  GRADE_COLOR,
  GRADE_LABEL,
  GRADE_ORDER,
  gradeBadge,
  kpiCard,
  legendItem,
  loadingBlock,
  mount,
  note,
  openDrawer,
  progressBar,
  select,
  table,
} from '/shared/ui.js';

let relays = [];
let state = {
  scores: [],
  sort: { key: 'overall', dir: 'asc' },
  gradeFilter: '',
  hosts: null,
  running: null,
};

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在加载质量数据…', compact: true }));

  const [summary, scores, checks] = await Promise.all([
    api.qualitySummary().catch((error) => ({ __error: error })),
    api.qualityScores({ limit: 100 }).catch(() => ({ items: [] })),
    api.qualityChecks({}).catch(() => ({ items: [] })),
  ]);

  if (summary?.__error) {
    mount(container, card('数据质量', emptyState({
      title: '质量数据读取失败',
      hint: summary.__error.message,
      actions: [button('重新加载', { kind: 'primary', onClick: () => ctx.refresh() })],
    })));
    return;
  }

  state.scores = scores?.items || [];
  state.checks = checks?.items || [];

  if (!state.scores.length) {
    mount(
      container,
      renderEvaluateCard(ctx),
      card('质量态势', emptyState({
        title: '尚未执行质量评测',
        hint: collectHint('也可以直接点击上方「触发质量评测」；采集管道在分析门户执行，采集完成后会自动评测。'),
        actions: [button('前往运行审计', { kind: 'primary', onClick: () => ctx.navigate('ops') })],
      })),
      card('质量检查项目录', renderCheckCatalog(state.checks), {
        subtitle: '来自 config/quality.yaml 的五维检查规则定义',
      }),
    );
    return;
  }

  mount(container, el('div', { id: 'quality-body', style: { display: 'flex', flexDirection: 'column', gap: '16px' } }));
  const host = container.querySelector('#quality-body');
  state.hosts = { body: host, ctx };

  renderBody(host, ctx, summary);
}

export function destroy() {
  relays = [];
  state.hosts = null;
  state.running = null;
}

// ==================================================================
// 主体
// ==================================================================

function renderBody(host, ctx, summary) {
  const dimension = summary.dimension_average || {};
  const distribution = summary.grade_distribution || {};
  const evaluated = summary.evaluated_assets || 0;

  const dimensionBox = el('div.chart', { style: { height: '300px' } });
  const gradeBox = el('div.chart', { style: { height: '300px' } });
  const scoreTableHost = el('div', { id: 'quality-score-table' });

  mount(
    host,
    renderEvaluateCard(ctx),
    el('div.grid.grid-kpi', null, [
      kpiCard({ label: '平均总分', value: formatFixed(summary.average_overall, 2), unit: '分', tone: scoreTone(summary.average_overall), foot: '已评测资产均分' }),
      kpiCard({ label: '已评测资产', value: formatInt(evaluated), unit: '项' }),
      kpiCard({
        label: '等级分布',
        value: GRADE_ORDER.filter((key) => distribution[key]).map((key) => `${key}:${distribution[key]}`).join('  ') || '—',
        foot: 'A 优秀 / B 良好 / C 关注 / D 较差 / E 待治理',
      }),
      kpiCard({
        label: '未通过 / 预警检查',
        value: formatInt((summary.status_distribution?.fail || 0) + (summary.status_distribution?.warn || 0)),
        unit: '项次',
        tone: (summary.status_distribution?.fail || 0) > 0 ? 'bad' : 'warn',
        foot: `通过 ${formatInt(summary.status_distribution?.pass || 0)} 项次`,
      }),
      kpiCard({
        label: '最差资产',
        value: summary.worst_assets?.[0]?.asset_key || '—',
        unit: '',
        tone: 'bad',
        foot: summary.worst_assets?.[0] ? `${formatFixed(summary.worst_assets[0].overall, 2)} 分 · ${summary.worst_assets[0].grade}` : '',
      }),
      kpiCard({ label: '评测规则版本', value: summary.rules_version || 'v1（见 /api/meta）', unit: '', foot: summary.generated_at ? `生成于 ${formatAuditTime(summary.generated_at)}` : '' }),
    ]),
    el('div.grid.grid-2', null, [
      card('五维质量均分', dimensionBox, {
        subtitle: '满分 100 分；缺失维度不参与加权，避免"少声明即低分"',
        actions: [el('div.legend', null, DIMENSION_KEYS.map((key) => legendItem(accent(), DIMENSION_LABEL[key])))],
      }),
      card('等级分布', gradeBox, {
        subtitle: 'Grading：A ≥ 90、B ≥ 80、C ≥ 70、D ≥ 60、E < 60',
        actions: [el('div.legend', null, GRADE_ORDER.map((key) => legendItem(GRADE_COLOR[key], `${key} ${GRADE_LABEL[key]}`)))],
      }),
    ]),
    el('div.grid.grid-sidebar', null, [
      scoreTableHost,
      el('div', { style: { display: 'flex', flexDirection: 'column', gap: '16px' } }, [
        renderWorstAssets(summary, ctx),
        renderFailingChecks(summary),
      ]),
    ]),
  );

  renderScoreTable(scoreTableHost, ctx);

  window.requestAnimationFrame(() => {
    const dimensionChart = renderChart(dimensionBox, barChart({
      categories: DIMENSION_KEYS.map((key) => DIMENSION_LABEL[key]),
      max: 100,
      series: [{
        name: '维度均分',
        color: accent(),
        data: DIMENSION_KEYS.map((key) => (typeof dimension[key] === 'number' ? dimension[key] : 0)),
      }],
    }));

    const gradeItems = GRADE_ORDER.filter((key) => distribution[key]).map((key) => ({
      name: `${key} ${GRADE_LABEL[key]}`,
      value: distribution[key],
      color: GRADE_COLOR[key],
    }));
    const gradeChart = renderChart(
      gradeBox,
      barChart({
        categories: gradeItems.map((item) => item.name),
        series: [{ name: '资产数', data: gradeItems.map((item) => item.value), color: '#a78bfa' }],
      }),
      { emptyMessage: gradeItems.length ? '' : '暂无等级分布数据。' },
    );

    relays.push(...[dimensionChart, gradeChart].filter(Boolean));
  });
}

// ==================================================================
// 触发评测
// ==================================================================

function renderEvaluateCard(ctx) {
  const windowSelect = select(
    [
      { value: '24', label: '近 24 小时' },
      { value: '168', label: '近 7 天（默认）' },
      { value: '720', label: '近 30 天' },
      { value: '2160', label: '近 90 天' },
    ],
    { value: '168' },
  );

  const scopeSelect = select(
    [
      { value: 'all', label: '全部资产' },
      ...(state.scores || []).slice(0, 20).map((item) => ({ value: item.asset_key, label: item.asset_key })),
    ],
    { value: 'all' },
  );

  const progressHost = el('div');

  const runButton = button('触发质量评测', {
    kind: 'primary',
    onClick: async () => {
      runButton.disabled = true;
      const assets = scopeSelect.value === 'all' ? null : [scopeSelect.value];
      mount(progressHost, loadingBlock('正在提交评测任务…'));
      try {
        const accepted = await api.evaluateQuality({
          assets,
          windowHours: Number(windowSelect.value),
          wait: false,
        });
        if (accepted?.task_id) {
          requestTaskRefresh();
          mount(progressHost, el('div', null, [
            el('div.text-dim', { text: `任务已受理：${taskKindLabel('quality')}（${accepted.task_id}）` }),
            el('div', { style: { marginTop: '8px' } }, [progressBar(null)]),
          ]));
          await waitForTask(accepted.task_id, {});
          mount(progressHost, el('div.text-ok', { text: '评测任务已结束，正在刷新数据…' }));
          setTimeout(() => ctx.refresh(), 400);
        } else {
          mount(progressHost, el('div', null, [
            el('div.text-ok', { text: `评测同步完成，共评测 ${formatInt(accepted?.evaluated || 0)} 个资产。` }),
          ]));
          setTimeout(() => ctx.refresh(), 600);
        }
      } catch (error) {
        mount(progressHost, el('div', null, [el('div.text-bad', { text: `评测触发失败：${error.message}` })]));
      } finally {
        runButton.disabled = false;
      }
    },
  });

  return card('质量评测', el('div', null, [
    el('div.toolbar', null, [
      el('div.field', null, [el('label', { text: '评测范围' }), scopeSelect]),
      el('div.field', null, [el('label', { text: '回溯窗口' }), windowSelect]),
      el('div.field', null, [el('label', { text: '操作' }), el('div.card-actions', null, [runButton])]),
    ]),
    el('div', { style: { marginTop: '12px' } }, [progressHost]),
  ]), {
    subtitle: '评测默认在后台执行；提交后本页会轮询任务状态并在完成后自动刷新',
  });
}

// ==================================================================
// 资产评分表
// ==================================================================

function renderScoreTable(host, ctx) {
  const scores = state.scores;

  const rows = scores
    .filter((item) => !state.gradeFilter || item.grade === state.gradeFilter)
    .sort((a, b) => {
      const factor = state.sort.dir === 'asc' ? 1 : -1;
      const left = a[state.sort.key];
      const right = b[state.sort.key];
      if (typeof left === 'number' && typeof right === 'number') return (left - right) * factor;
      return String(left ?? '').localeCompare(String(right ?? '')) * factor;
    });

  const gradeChips = el('div.chip-row', null, [
    el('span.chip-label', { text: '等级筛选' }),
    ...GRADE_ORDER.map((key) => el('button.chip', {
      type: 'button',
      class: state.gradeFilter === key ? 'active' : '',
      onClick: () => {
        state.gradeFilter = state.gradeFilter === key ? '' : key;
        renderScoreTable(host, ctx);
      },
    }, [el('span', { text: key }), el('span.chip-count', { text: String(scores.filter((item) => item.grade === key).length) })])),
  ]);

  const columns = [
    {
      key: 'asset_key',
      title: '资产',
      sortable: true,
      render: (row) => el('div', null, [
        el('div', { text: row.asset_key || '—' }),
        el('div.text-mute', { text: row.scored_ago || '—', style: { fontSize: '11.5px' } }),
      ]),
    },
    { key: 'overall', title: '总分', align: 'right', sortable: true, render: (row) => formatFixed(row.overall, 2) },
    { key: 'grade', title: '等级', sortable: true, render: (row) => gradeBadge(row.grade) },
    { key: 'completeness', title: '完整性', align: 'right', sortable: true, render: (row) => formatFixed(row.completeness, 1) },
    { key: 'timeliness', title: '时效性', align: 'right', sortable: true, render: (row) => formatFixed(row.timeliness, 1) },
    { key: 'validity', title: '有效性', align: 'right', sortable: true, render: (row) => formatFixed(row.validity, 1) },
    { key: 'consistency', title: '一致性', align: 'right', sortable: true, render: (row) => formatFixed(row.consistency, 1) },
    { key: 'uniqueness', title: '唯一性', align: 'right', sortable: true, render: (row) => formatFixed(row.uniqueness, 1) },
    {
      key: 'checks',
      title: '检查项',
      render: (row) => el('span', { text: `${formatInt(row.checks_total)} 项 · 失败 ${formatInt(row.checks_failed)} · 预警 ${formatInt(row.checks_warned)}` }),
    },
  ];

  mount(
    host,
    card('资产质量评分', el('div', null, [
      gradeChips,
      el('div', { style: { marginTop: '12px' } }, [
        table(columns, rows, {
          sortKey: state.sort.key,
          sortDir: state.sort.dir,
          onSort: (key) => {
            state.sort = { key, dir: state.sort.key === key && state.sort.dir === 'asc' ? 'desc' : 'asc' };
            renderScoreTable(host, ctx);
          },
          onRowClick: (row) => openAssetQuality(row.asset_key, ctx),
          empty: '当前筛选条件下没有资产',
        }),
      ]),
    ]), {
      subtitle: `共 ${formatInt(scores.length)} 个资产的最新评分；点击任意行查看五维雷达与逐项检查明细`,
    }),
  );
}

function renderWorstAssets(summary, ctx) {
  const items = summary.worst_assets || [];
  return card('质量最差资产', items.length
    ? el('div.check-list', null, items.map((item) => el('div.check-item', { class: item.grade === 'E' || item.grade === 'D' ? 'fail' : '' }, [
        el('div.check-head', null, [
          el('div', null, [
            el('span.check-name', { text: item.asset_key || '（已下线资产）' }),
            el('span.check-meta', { text: ` · ${formatFixed(item.overall, 2)} 分` }),
          ]),
          gradeBadge(item.grade),
        ]),
        el('div.check-meta', { text: `完整性 ${formatFixed(item.completeness, 1)} · 时效性 ${formatFixed(item.timeliness, 1)} · 有效性 ${formatFixed(item.validity, 1)} · 一致性 ${formatFixed(item.consistency, 1)} · 唯一性 ${formatFixed(item.uniqueness, 1)}` }),
        el('div.card-actions', { style: { marginTop: '8px' } }, [
          button('查看详情', {
            size: 'sm',
            onClick: () => openAssetQuality(item.asset_key, ctx),
          }),
        ]),
      ])))
    : emptyState({ title: '暂无最差资产排行', compact: true }));
}

function renderFailingChecks(summary) {
  const items = summary.top_failing_checks || [];
  return card('高频未通过检查项', items.length
    ? table(
        [
          { key: 'check_key', title: '检查项', render: (row) => el('span.mono', { text: row.check_key }) },
          { key: 'dimension_label', title: '维度', render: (row) => row.dimension_label || row.dimension || '—' },
          { key: 'fail', title: '不通过', align: 'right', render: (row) => el('span.text-bad', { text: formatInt(row.fail) }) },
          { key: 'warn', title: '预警', align: 'right', render: (row) => el('span.text-warn', { text: formatInt(row.warn) }) },
          { key: 'count', title: '合计', align: 'right', render: (row) => formatInt(row.count) },
        ],
        items,
        { empty: '暂无失败检查项' },
      )
    : emptyState({ title: '暂无未通过检查项', hint: '全部检查项均通过。', compact: true }));
}

function renderCheckCatalog(checks) {
  if (!checks.length) return emptyState({ title: '暂无检查项目录', compact: true });
  return table(
    [
      { key: 'check_key', title: '检查项', render: (row) => el('span.mono', { text: row.check_key }) },
      { key: 'name', title: '名称' },
      { key: 'dimension_label', title: '维度' },
      { key: 'severity_label', title: '严重级别' },
      { key: 'description', title: '说明', wrap: true, render: (row) => row.description || '—' },
    ],
    checks.filter((item, index, list) => list.findIndex((other) => other.check_key === item.check_key) === index),
    { empty: '暂无检查项' },
  );
}

// ==================================================================
// 单资产质量详情
// ==================================================================

async function openAssetQuality(assetKey, ctx) {
  const radarBox = el('div.chart', { style: { height: '300px' } });
  const historyBox = el('div.chart', { style: { height: '240px' } });
  const checksHost = el('div', null, loadingBlock('正在同步执行单资产评测…'));
  const summaryHost = el('div');

  openDrawer({
    title: assetKey || '（资产已下线）',
    subtitle: '质量详情 · 该数据来自接口同步评测，是当前最完整的口径',
    body: el('div', null, [
      summaryHost,
      el('div.grid.grid-2', null, [
        card('五维雷达', radarBox),
        card('评分趋势', historyBox, { subtitle: '最近 60 次评测的总分变化' }),
      ]),
      card('逐项检查结果', checksHost, { subtitle: '评测窗口与维度权重由 config/quality.yaml 定义' }),
    ]),
    headActions: [button('在目录中查看', { size: 'sm', onClick: () => ctx.navigate('assets', [assetKey]) })],
  });

  // 历史评分（可能不存在 —— 新资产尚无历史）
  api.qualityHistory(assetKey, 60).then((payload) => {
    const items = payload?.items || [];
    window.requestAnimationFrame(() => {
      if (!historyBox.isConnected) return;
      if (items.length < 2) {
        mount(historyBox, emptyState({ title: '暂无评分趋势', hint: '历史评分至少需要两次评测记录。', compact: true }));
        return;
      }
      const chart = renderChart(historyBox, lineChart({
        categories: items.map((item) => formatAuditTime(item.scored_at, false)),
        series: [{ name: '总分', data: items.map((item) => item.overall), color: '#22c55e', area: true }],
        yAxis: [{ type: 'value', min: 0, max: 100, axisLabel: { color: '#7b889e', fontSize: 11 } }],
      }));
      if (chart) relays.push(chart);
    });
  }).catch(() => {
    mount(historyBox, emptyState({ title: '暂无评分趋势', compact: true }));
  });

  let report;
  try {
    report = await api.qualityReport(assetKey, null);
  } catch (error) {
    mount(checksHost, emptyState({
      title: '无法执行单资产评测',
      hint: `${error.message}<br />若该资产在数据湖中还没有分区文件，评测会直接失败。`,
      compact: true,
    }));
    mount(summaryHost, emptyState({ title: '暂无评测结果', compact: true }));
    return;
  }

  const dimensions = report?.dimensions || [];
  const checks = report?.checks || [];
  const summary = report?.summary || {};

  mount(summaryHost, el('div', null, [
    el('div.grid.grid-4', null, [
      kpiCard({ label: '总分', value: formatFixed(report?.overall, 2), unit: '分' }),
      kpiCard({ label: '等级', value: `${report?.grade || '—'} ${report?.grade_label || ''}` }),
      kpiCard({ label: '检查项', value: formatInt(summary.checks_total), unit: '项', foot: `失败 ${formatInt(summary.checks_failed)} · 预警 ${formatInt(summary.checks_warned)} · 跳过 ${formatInt(summary.checks_skipped)}` }),
      kpiCard({ label: '参与评测数据', value: formatInt(summary.rows_evaluated), unit: '行', foot: `${formatInt(summary.cities_evaluated)} 个城市` }),
    ]),
    el('div', { style: { marginTop: '10px' } }, [
      note(`评测窗口：近 ${report?.window_hours ?? '—'} 小时 · 评测时间：${formatAuditTime(report?.scored_at)}（本地时区）`),
    ]),
  ]));

  window.requestAnimationFrame(() => {
    if (!radarBox.isConnected) return;
    const chart = renderChart(radarBox, radarChart(
      DIMENSION_KEYS.map((key) => DIMENSION_LABEL[key]),
      100,
      [{
        name: '维度得分',
        values: DIMENSION_KEYS.map((key) => {
          const hit = dimensions.find((item) => item.key === key);
          return hit && typeof hit.score === 'number' ? hit.score : 0;
        }),
        color: accent(),
      }],
    ));
    if (chart) relays.push(chart);
  });

  renderCheckResults(checksHost, checks, dimensions);
}

function renderCheckResults(host, checks, dimensions) {
  if (!checks.length) {
    mount(host, emptyState({
      title: '没有可执行的检查项',
      hint: '该资产未配置质量检查项，或全部检查因缺少数据被跳过。',
      compact: true,
    }));
    return;
  }

  const dimensionMap = new Map((dimensions || []).map((item) => [item.key, item]));

  const rows = [...checks].sort((a, b) => {
    const rank = { fail: 0, error: 1, warn: 2, skipped: 3, pass: 4 };
    return (rank[a.status] ?? 9) - (rank[b.status] ?? 9);
  });

  mount(host, el('div.check-list', null, rows.map((check) => {
    const dimension = dimensionMap.get(check.dimension);
    const cls = check.status === 'fail' || check.status === 'error' ? 'fail' : check.status === 'warn' ? 'warn' : '';
    return el(`div.check-item.${cls}`, null, [
      el('div.check-head', null, [
        el('div', null, [
          el('span.check-name', { text: check.name || check.check_key }),
          el('span.check-meta', { text: ` · ${check.dimension_label || DIMENSION_LABEL[check.dimension] || check.dimension || ''}${dimension ? ` · 维度得分 ${formatFixed(dimension.score, 1)}` : ''}` }),
        ]),
        el('div.card-actions', null, [
          checkStatusBadge(check.status, check.status_label),
          badge(`${formatFixed(check.score, 1)} 分`, 'badge-mute'),
          badge(check.severity === 'critical' ? '严重' : check.severity === 'major' ? '重要' : '次要',
            check.severity === 'critical' ? 'badge-bad' : check.severity === 'major' ? 'badge-warn' : 'badge-info'),
        ]),
      ]),
      el('div.check-msg', { text: check.message || '（无说明）' }),
      el('div.check-meta', {
        text: [
          `观测值 ${check.observed === null || check.observed === undefined ? '—' : formatFixed(check.observed, 3)}`,
          `期望值 ${check.expected === null || check.expected === undefined ? '—' : formatFixed(check.expected, 3)}`,
          `记录数 ${formatInt(check.records_checked)}`,
          `失败记录 ${formatInt(check.records_failed)}`,
        ].join(' · '),
      }),
      check.detail && Object.keys(check.detail).length
        ? el('div.check-detail', { text: JSON.stringify(check.detail, null, 2) })
        : null,
    ]);
  })));
}

function scoreTone(score) {
  if (typeof score !== 'number') return '';
  if (score >= 90) return 'ok';
  if (score >= 75) return '';
  if (score >= 60) return 'warn';
  return 'bad';
}
