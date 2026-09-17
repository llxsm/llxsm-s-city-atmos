/**
 * audit.js —— 运行审计（治理门户路由 `#/governance/ops`）。
 *
 * 文件名按用途取 audit（分析门户的同类页面是 refresh.js）：两个门户都有名为
 * `ops` 的路由，但归属不同门户，统一前端里必须靠"门户前缀 + 不同文件名"区分。
 *
 * 治理部门关心"数据是怎么进来的、有没有出问题"，因此本页只保留：
 *  1. 运行态势 KPI（近 24 小时的运行次数、成功率、写入量、API 调用）；
 *  2. 运维动作：元数据同步（`POST /api/ops/bootstrap`）与统计刷新（`POST /api/ops/refresh`）；
 *  3. 运行记录审计表：逐条运行的行数 / 体积 / API 调用 / 分区数 / 错误信息；
 *  4. 作业清单与数据源契约（`GET /api/ops/specs`，用于核对上游字段是否变化）。
 *
 * 相对拆分前的运维页，这里**移除**了：
 *   * 「触发采集」表单与城市 / 范围选择（数据刷新由分析门户负责，
 *     避免两个门户都能改数据导致责任边界模糊）；
 *   * 采后质量评测的联动入口（改由「数据质量」页独立触发）。
 */

import { api } from '/shared/api.js';
import {
  badge,
  button,
  card,
  collectHint,
  el,
  emptyState,
  formatAuditTime,
  formatBytes,
  formatCompact,
  formatDuration,
  formatFixed,
  formatInt,
  kpiCard,
  loadingBlock,
  mount,
  note,
  runStatusBadge,
  table,
} from '/shared/ui.js';

const STAGE_LABEL = { reference: '参考层', collect: '采集', derive: '派生' };

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在加载运行审计信息…', compact: true }));

  const [jobs, runs, summary, specs] = await Promise.all([
    api.opsJobs().catch(() => ({ items: [], scopes: {} })),
    api.opsRuns({ limit: 100 }).catch((error) => ({ __error: error, items: [] })),
    api.opsSummary(24).catch(() => null),
    api.opsSpecs().catch(() => ({ items: [] })),
  ]);

  mount(
    container,
    renderSummary(summary),
    el('div.grid.grid-sidebar-left', null, [
      renderMaintenance(jobs),
      renderJobCatalog(jobs),
    ]),
    renderRuns(runs),
    renderSpecs(specs),
  );
}

export function destroy() {
  // 本页没有图表实例、定时器或外部订阅需要清理
}

// ==================================================================
// 运行态势
// ==================================================================

function renderSummary(summary) {
  const byStatus = summary?.by_status || {};
  return el('div.grid.grid-kpi', null, [
    kpiCard({
      label: `近 ${formatInt(summary?.window_hours ?? 24)} 小时运行`,
      value: formatInt(summary?.total),
      unit: '次',
      foot: `成功 ${formatInt(byStatus.success)} · 部分成功 ${formatInt(byStatus.partial)} · 失败 ${formatInt(byStatus.failed)}`,
    }),
    kpiCard({
      label: '运行成功率',
      value: summary?.success_rate === null || summary?.success_rate === undefined ? '—' : formatFixed(summary.success_rate, 1),
      unit: '%',
      tone: summary?.success_rate === null || summary?.success_rate === undefined ? '' : summary.success_rate >= 95 ? 'ok' : summary.success_rate >= 80 ? 'warn' : 'bad',
      foot: '以运行记录中的成功占比计算',
    }),
    kpiCard({
      label: '写入行数',
      value: formatCompact(summary?.total_rows_written),
      unit: '行',
      foot: `体积 ${formatBytes(summary?.total_bytes_written)}`,
    }),
    kpiCard({ label: '数据源 API 调用', value: formatInt(summary?.total_api_calls), unit: '次' }),
    kpiCard({
      label: '最近一次运行',
      value: summary?.last_run_at ? formatAuditTime(summary.last_run_at) : '—',
      foot: '审计时间已转换为本地时区',
    }),
    kpiCard({
      label: '运行记录总数',
      value: formatInt(summary?.total),
      unit: '条',
      foot: '窗口内按作业 × 城市展开',
    }),
  ]);
}

// ==================================================================
// 运维动作
// ==================================================================

function renderMaintenance(jobs) {
  const host = el('div', { style: { marginTop: '12px' } });

  async function runAction(label, action) {
    mount(host, loadingBlock(`正在执行：${label}…`));
    try {
      const result = await action();
      if (label === '同步元数据') {
        mount(host, el('div', null, [
          el('div.text-ok', { text: '元数据同步完成。' }),
          el('div.text-dim', {
            text: `资产 ${formatInt(result?.assets_created)} 新增 / ${formatInt(result?.assets_updated)} 更新`
              + ` · 城市 ${formatInt(result?.cities_created)} 新增 / ${formatInt(result?.cities_updated)} 更新`
              + ` · 血缘边 ${formatInt(result?.lineage_created)} 新增`,
          }),
        ]));
      } else {
        mount(host, el('div', null, [
          el('div.text-ok', { text: `统计刷新完成，共处理 ${formatInt(result?.assets_refreshed)} 个资产。` }),
        ]));
      }
    } catch (error) {
      mount(host, el('div.text-bad', { text: `${label}失败：${error.message}` }));
    }
  }

  return card('运维动作', el('div', null, [
    el('div.switch-row', null, [
      button('同步元数据（bootstrap）', {
        onClick: () => runAction('同步元数据', () => api.opsBootstrap()),
        title: '把 config/*.yaml 的资产、城市、血缘与质量规则幂等写入目录',
      }),
      button('刷新统计与版本快照', {
        onClick: () => runAction('刷新统计', () => api.opsRefresh()),
        title: '扫描数据湖重算行数/体积/覆盖城市，并生成版本快照',
      }),
    ]),
    host,
    note(`当前共定义 ${formatInt(jobs?.items?.length)} 个 ETL 作业。触发采集由分析门户负责，本页只做元数据与统计的维护。`),
  ]), {
    subtitle: '元数据同步与统计刷新为同步接口，通常在数秒内返回',
  });
}

// ==================================================================
// 运行记录（审计）
// ==================================================================

function renderRuns(payload) {
  const items = payload?.items || [];

  if (payload?.__error) {
    return card('运行记录', emptyState({
      title: '运行记录读取失败',
      hint: payload.__error.message,
      compact: true,
    }));
  }

  if (!items.length) {
    return card('运行记录', emptyState({
      title: '暂无运行记录',
      hint: collectHint('运行记录在采集管道执行时写入；采集由分析门户的「数据刷新」页触发。'),
      compact: true,
    }));
  }

  return card('运行记录', table(
    [
      { key: 'started_at', title: '开始时间', render: (row) => formatAuditTime(row.started_at) },
      { key: 'job', title: '作业', render: (row) => el('span.mono', { text: row.job || '—' }) },
      { key: 'asset_key', title: '资产', render: (row) => el('span.mono.text-dim', { text: row.asset_key || '—' }) },
      { key: 'city_name', title: '城市', render: (row) => row.city_name || row.city_slug || '—' },
      { key: 'status', title: '状态', render: (row) => runStatusBadge(row.status, row.status_label) },
      { key: 'rows_written', title: '写入行数', align: 'right', render: (row) => formatInt(row.rows_written) },
      { key: 'bytes_written', title: '体积', align: 'right', render: (row) => formatBytes(row.bytes_written) },
      { key: 'api_calls', title: 'API 调用', align: 'right', render: (row) => formatInt(row.api_calls) },
      { key: 'partitions_written', title: '分区数', align: 'right', render: (row) => formatInt(row.partitions_written) },
      { key: 'duration_ms', title: '耗时', align: 'right', render: (row) => formatDuration(row.duration_ms) },
      {
        key: 'error_message',
        title: '错误信息',
        wrap: true,
        render: (row) => (row.error_message
          ? el('div', null, [
              row.error_type ? el('div', { class: 'mono text-warn', text: row.error_type }) : null,
              el('span.text-bad', { text: row.error_message }),
            ])
          : '—'),
      },
    ],
    items,
    { empty: '暂无运行记录' },
  ), {
    subtitle: `最近 ${formatInt(items.length)} 条运行记录，按开始时间倒序；审计时间为 UTC，已转换为本地时区`,
  });
}

// ==================================================================
// 作业清单与上游契约
// ==================================================================

function renderJobCatalog(jobs) {
  const items = jobs?.items || [];
  const scopes = jobs?.scopes || {};

  if (!items.length) {
    return card('作业清单', emptyState({ title: '暂无作业定义', compact: true }));
  }

  const scopeText = Object.entries(scopes)
    .map(([name, stages]) => `${name} = ${stages.length ? stages.map((stage) => STAGE_LABEL[stage] || stage).join(' + ') : '空（仅查看状态）'}`)
    .join('；');

  return card('作业清单', el('div', null, [
    scopeText ? note(`采集范围映射：${scopeText}`) : null,
    table(
      [
        { key: 'name', title: '作业', render: (row) => el('span.mono', { text: row.name }) },
        { key: 'asset_key', title: '目标资产', render: (row) => el('span.mono.text-dim', { text: row.asset_key || '—' }) },
        { key: 'stage', title: '阶段', render: (row) => badge(STAGE_LABEL[row.stage] || row.stage, 'badge-accent') },
        { key: 'description', title: '说明', wrap: true, render: (row) => row.description || '—' },
      ],
      items,
      { empty: '暂无作业' },
    ),
  ]), { subtitle: `共 ${formatInt(items.length)} 个作业，按依赖顺序执行` });
}

function renderSpecs(payload) {
  const items = payload?.items || [];
  if (!items.length) {
    return card('数据源字段契约', emptyState({
      title: '暂无字段契约',
      hint: '作业契约来自 atmos/sources/specs.py，未能读取。',
      compact: true,
    }));
  }

  return card('数据源字段契约', table(
    [
      { key: 'name', title: '数据集', render: (row) => el('div', null, [el('div', { text: row.name || row.asset_key }), el('div.mono.text-mute', { text: row.asset_key })]) },
      { key: 'variable_count', title: '字段数', align: 'right', render: (row) => formatInt(row.variable_count) },
      {
        key: 'variables',
        title: '请求字段',
        wrap: true,
        render: (row) => el('span.mono.text-dim', {
          text: (row.variables || []).join(', ') || '—',
          title: (row.variables || []).join(', '),
        }),
      },
    ],
    items,
    { empty: '暂无字段契约' },
  ), { subtitle: '每个作业实际向 Open-Meteo 请求的字段清单，用于核对上游契约变化' });
}
