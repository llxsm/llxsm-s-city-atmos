/**
 * refresh.js —— 数据刷新（分析门户路由 `#/analysis/ops`）。
 *
 * 文件名按用途取 refresh（治理门户的同类页面是 audit.js）：两个门户都有名为
 * `ops` 的路由，但归属不同门户，统一前端里必须靠"门户前缀 + 不同文件名"区分。
 *
 * 分析人员关心的是"让数据变新"，不关心运维审计，因此本页只保留四块：
 *  1. 「触发采集」表单：scope / cities / include_archive / quality_after；
 *  2. 后台任务进度面板：`onTasks()` 订阅 `GET /api/ops/tasks`，完成后自动刷新；
 *  3. 作业清单（说明这次采集会拉取什么）；
 *  4. 采集范围与作业阶段的映射说明。
 *
 * 与治理门户的分工：运行记录的逐条审计（行数 / 字节 / API 调用 / schema 漂移）
 * 与「元数据同步 / 统计刷新」按钮只存在于治理门户，本页刻意不提供。
 *
 * 城市清单来自 `/api/env/cities`（分析门户**没有** `/api/cities`），
 * 两者口径一致：都只返回启用中的城市。
 */

import { api } from '/shared/api.js';
import { onTasks, requestTaskRefresh, taskKindLabel, taskParamsText, waitForTask } from '/shared/tasks.js';
import {
  badge,
  button,
  card,
  checkbox,
  el,
  emptyState,
  formatAuditTime,
  formatDuration,
  formatInt,
  kpiCard,
  loadingBlock,
  mount,
  note,
  progressBar,
  runStatusBadge,
  select,
  table,
} from '/shared/ui.js';

const SCOPE_OPTIONS = [
  { value: 'all', label: '全量（参考层 + 采集 + 派生）' },
  { value: 'collect', label: '采集（参考层 + 采集）' },
  { value: 'derive', label: '派生（仅重建融合宽表）' },
  { value: 'reference', label: '参考层（仅地理编码）' },
];

const STAGE_LABEL = { reference: '参考层', collect: '采集', derive: '派生' };

let state = {
  cities: [],
  selectedCities: [],
  progressHost: null,
  unsubscribe: null,
};

export async function render(container, ctx) {
  mount(container, emptyState({ title: '正在读取采集作业定义…', compact: true }));

  const [jobs, cityPayload, tasks] = await Promise.all([
    api.opsJobs().catch(() => ({ items: [], scopes: {} })),
    api.envCities().catch(() => ({ items: [] })),
    api.opsTasks(20).catch(() => ({ items: [], active: 0 })),
  ]);

  state.cities = cityPayload?.items || [];
  state.selectedCities = state.selectedCities.filter((slug) => state.cities.some((city) => city.slug === slug));

  const taskHost = el('div');
  state.progressHost = el('div');

  mount(
    container,
    renderSummary(tasks, jobs),
    el('div.grid.grid-sidebar-left', null, [
      renderCollectForm(ctx),
      renderJobCatalog(jobs),
    ]),
    renderTaskPanel(taskHost, tasks),
  );

  // 任务面板由 tasks.js 统一轮询驱动，这里只订阅本视图需要的进度区
  state.onTasksUpdate = (snapshot) => {
    renderTaskPanel(taskHost, {
      items: snapshot.records,
      active: snapshot.records.filter((item) => !item.finished_at).length,
    });
  };
  state.unsubscribe = onTasks(state.onTasksUpdate);
  requestTaskRefresh();
}

export function destroy() {
  if (state.unsubscribe) {
    state.unsubscribe();
    state.unsubscribe = null;
  }
  state.progressHost = null;
  state.onTasksUpdate = null;
}

// ==================================================================
// 概览
// ==================================================================

function renderSummary(tasks, jobs) {
  const active = (tasks?.items || []).filter((item) => !item.finished_at);
  const finished = (tasks?.items || []).filter((item) => item.finished_at);
  const failed = finished.filter((item) => item.status === 'failed');
  const last = finished[0] || null;

  return el('div.grid.grid-kpi', null, [
    kpiCard({
      label: '可选城市',
      value: formatInt(state.cities.length),
      unit: '个',
      foot: '启用中的城市；不选表示全部',
    }),
    kpiCard({
      label: 'ETL 作业',
      value: formatInt(jobs?.items?.length),
      unit: '个',
      foot: '按阶段顺序执行',
    }),
    kpiCard({
      label: '进行中任务',
      value: formatInt(active.length),
      unit: '个',
      tone: active.length ? 'warn' : 'ok',
      foot: '进程内后台任务登记簿',
    }),
    kpiCard({
      label: '最近一次采集',
      value: last ? formatAuditTime(last.started_at) : '—',
      foot: last ? `状态 ${last.status || '—'}${last.duration_ms ? ` · 耗时 ${formatDuration(last.duration_ms)}` : ''}` : '本进程内暂无采集任务',
    }),
    kpiCard({
      label: '本进程失败任务',
      value: formatInt(failed.length),
      unit: '个',
      tone: failed.length ? 'bad' : 'ok',
      foot: '仅统计当前服务进程内的任务',
    }),
  ]);
}

// ==================================================================
// 触发采集
// ==================================================================

function renderCollectForm(ctx) {
  const scopeSelect = select(SCOPE_OPTIONS, { value: 'all' });

  const cityToggles = state.cities.length
    ? state.cities.map((city) => checkbox({
        label: city.name_zh || city.slug,
        checked: state.selectedCities.includes(city.slug),
        title: `时区：${city.timezone || '未知'}`,
        onChange: (checked) => {
          if (checked) {
            if (!state.selectedCities.includes(city.slug)) state.selectedCities.push(city.slug);
          } else {
            state.selectedCities = state.selectedCities.filter((slug) => slug !== city.slug);
          }
        },
      }))
    : [el('div.text-warn', { text: '未读取到启用城市，采集将使用配置文件中定义的城市。' })];

  const archiveToggle = checkbox({
    label: '包含历史归档回补（collect_weather_archive，耗时较长）',
    checked: true,
  });
  const qualityToggle = checkbox({ label: '采集完成后立即执行质量评测', checked: true });
  const waitToggle = checkbox({ label: '同步等待完成（不推荐，容易触发 HTTP 超时）', checked: false });

  const submit = button('启动采集管道', {
    kind: 'primary',
    onClick: async () => {
      submit.disabled = true;
      const progress = state.progressHost;
      mount(progress, loadingBlock('正在提交采集任务…'));
      try {
        const accepted = await api.opsCollect({
          scope: scopeSelect.value,
          cities: state.selectedCities,
          includeArchive: archiveToggle.querySelector('input').checked,
          qualityAfter: qualityToggle.querySelector('input').checked,
          wait: waitToggle.querySelector('input').checked,
        });

        if (accepted?.task_id) {
          requestTaskRefresh();
          mount(progress, el('div', null, [
            el('div.text-dim', { text: `任务已受理：${taskKindLabel('collect')}（${accepted.task_id}）` }),
            el('div', { style: { marginTop: '8px' } }, [progressBar(null)]),
          ]));
          const record = await waitForTask(accepted.task_id, {
            onProgress: (task) => {
              if (!task) return;
              const text = task.finished_at
                ? `任务结束：${task.status}${task.error ? ` · ${task.error}` : ''}`
                : '采集执行中，请稍候…';
              mount(progress, el('div', null, [
                el('div.text-dim', { text: `${taskKindLabel(task.kind)}（${task.task_id}）` }),
                el('div', { style: { marginTop: '6px' } }, [el('span', {
                  class: task.finished_at ? (task.status === 'failed' ? 'text-bad' : 'text-ok') : 'text-warn',
                  text,
                })]),
                el('div', { style: { marginTop: '8px' } }, [progressBar(task.finished_at ? 100 : null)]),
              ]));
            },
          });
          if (record && record.status !== 'failed') {
            mount(progress, el('div', null, [
              el('div.text-ok', { text: '采集已完成，正在刷新本页数据…' }),
              el('div', { style: { marginTop: '8px' } }, [progressBar(100)]),
            ]));
            setTimeout(() => ctx.refresh(), 600);
          }
        } else {
          mount(progress, el('div', null, [
            el('div.text-ok', { text: `采集同步执行完成，状态：${accepted?.status || '未知'}` }),
          ]));
          setTimeout(() => ctx.refresh(), 800);
        }
      } catch (error) {
        mount(progress, el('div.text-bad', { text: `采集触发失败：${error.message}` }));
      } finally {
        submit.disabled = false;
      }
    },
  });

  return card('触发采集', el('div', null, [
    el('div.toolbar', null, [
      el('div.field', { style: { minWidth: '260px' } }, [el('label', { text: '采集范围 scope' }), scopeSelect]),
      el('div.field', null, [el('label', { text: '执行方式' }), el('div.switch-row', null, [waitToggle])]),
    ]),
    el('div.section-title', { text: `限定城市（已选 ${state.selectedCities.length} 个，不选表示全部启用城市）`, style: { marginTop: '14px' } }),
    el('div.chip-row', null, cityToggles),
    el('div.section-title', { text: '附加选项', style: { marginTop: '14px' } }),
    el('div.switch-row', null, [archiveToggle, qualityToggle]),
    el('div.card-actions', { style: { marginTop: '14px' } }, [submit]),
    el('div', { style: { marginTop: '12px' } }, [state.progressHost]),
  ]), {
    subtitle: '采集默认在后台执行并立即返回任务 ID；本页轮询任务状态，完成后自动刷新',
  });
}

// ==================================================================
// 后台任务面板
// ==================================================================

function renderTaskPanel(host, payload) {
  const items = payload?.items || [];
  const wrap = el('div');

  if (!items.length) {
    mount(wrap, emptyState({
      title: '当前没有后台任务',
      hint: '触发采集后，这里会显示本次刷新的实时进度。',
      compact: true,
    }));
  } else {
    mount(wrap, el('div.check-list', null, items.slice(0, 12).map((task) => {
      const running = !task.finished_at;
      return el(`div.check-item.${task.status === 'failed' ? 'fail' : ''}`, null, [
        el('div.check-head', null, [
          el('div', null, [
            el('span.check-name', { text: taskKindLabel(task.kind) }),
            el('span.check-meta.mono', { text: `  ${task.task_id}` }),
          ]),
          el('div.card-actions', null, [
            running ? badge('进行中', 'badge-info') : runStatusBadge(task.status),
            task.duration_ms ? badge(formatDuration(task.duration_ms), 'badge-mute') : null,
          ]),
        ]),
        el('div.check-meta', {
          text: `开始于 ${formatAuditTime(task.started_at)} 本地时间 · ${taskParamsText(task.params) || '无参数'}`,
        }),
        el('div', { style: { marginTop: '8px' } }, [progressBar(running ? null : 100)]),
        task.error ? el('div.check-detail', { text: task.error }) : null,
        task.result ? el('div.check-detail', { text: summarizeResult(task.result) }) : null,
      ]);
    })));
  }

  mount(host, card('刷新任务进度', wrap, {
    subtitle: '任务列表每 4 秒自动刷新；任务结束后当前视图会自动重新加载',
    actions: [badge(`活动 ${formatInt(payload?.active || 0)} 个`, payload?.active ? 'badge-info' : 'badge-mute')],
  }));
}

function summarizeResult(result) {
  if (!result || typeof result !== 'object') return '';
  const parts = [];
  if (result.status) parts.push(`状态：${result.status}`);
  if (result.jobs?.length) parts.push(`作业 ${result.jobs.length} 个`);
  if (result.total_rows_written !== undefined) parts.push(`写入 ${formatInt(result.total_rows_written)} 行`);
  if (result.evaluated !== undefined) parts.push(`评测 ${formatInt(result.evaluated)} 个资产`);
  return parts.join(' · ') || JSON.stringify(result).slice(0, 300);
}

// ==================================================================
// 作业清单
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
    note(`采集范围映射：${scopeText}`),
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
  ]), {
    subtitle: `共 ${formatInt(items.length)} 个作业，按依赖顺序执行；运行记录审计见治理门户`,
  });
}
