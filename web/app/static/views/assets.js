/**
 * assets.js —— 数据资产目录。
 *
 * 左侧为筛选与资产表格，点击任意资产打开右侧抽屉，展示：
 * 业务口径、来源与许可、主键/分区/存储路径、字段字典、检查项、版本快照、
 * 最近运行、样例数据与列级画像。
 *
 * 抽屉内的区块各自独立加载：即便某个接口报错（例如尚未采集导致无样例数据），
 * 其他区块仍然可用。
 */

import { api, exportUrl } from '/shared/api.js';
import {
  badge,
  button,
  card,
  chip,
  chipRow,
  closeDrawer,
  collectHint,
  el,
  emptyState,
  field,
  formatAuditTime,
  formatBytes,
  formatCompact,
  formatDataTime,
  formatFixed,
  formatInt,
  formatPercent,
  gradeBadge,
  input,
  kpiCard,
  kvList,
  layerBadge,
  loadingBlock,
  mount,
  note,
  openDrawer,
  select,
  table,
} from '/shared/ui.js';

const FRESHNESS_TONE = {
  healthy: { label: '符合 SLA', cls: 'badge-ok' },
  breached: { label: '超出 SLA', cls: 'badge-bad' },
  no_data: { label: '无数据', cls: 'badge-bad' },
  unknown: { label: '未定义 SLA', cls: 'badge-mute' },
};

const LAYER_ORDER = ['reference', 'raw', 'refined', 'serving'];

let state = {
  filters: { domain: '', layer: '', status: '', search: '' },
  payload: null,
  sort: { key: 'key', dir: 'asc' },
  cities: [],
  selectedKey: null,
};

export async function render(container, ctx, params) {
  mount(container, emptyState({ title: '正在加载资产目录…', compact: true }));

  const [payload, cityPayload, meta] = await Promise.all([
    api.assets(state.filters).catch((error) => ({ __error: error })),
    // 样例数据的城市筛选只应列出启用中的城市（配置中移除的城市会保留历史数据）
    api.cities(true).catch(() => ({ items: [] })),
    api.meta().catch(() => null),
  ]);

  state.cities = cityPayload?.items || [];
  state.meta = meta;

  if (payload?.__error) {
    mount(container, card('数据资产目录', emptyState({
      title: '资产目录读取失败',
      hint: payload.__error.message,
      actions: [button('重新加载', { kind: 'primary', onClick: () => ctx.refresh() })],
    })));
    return;
  }

  state.payload = payload;

  mount(container, el('div', { id: 'assets-toolbar' }), el('div', { id: 'assets-body' }));
  renderToolbar(container, ctx);
  renderBody(container, ctx);

  // 支持 #/assets/<asset_key> 直接打开某资产详情
  const initialKey = Array.isArray(params) ? params[0] : null;
  if (initialKey) openAssetDrawer(initialKey, ctx);
}

export function destroy() {
  state.selectedKey = null;
}

// ==================================================================
// 工具栏与筛选
// ==================================================================

function renderToolbar(container, ctx) {
  const host = container.querySelector('#assets-toolbar');
  const payload = state.payload || {};
  const facets = payload.facets || {};
  const enums = state.meta?.enums || {};

  const domainOptions = [{ value: '', label: '全部业务域' }, ...(enums.asset_domain || []).map((item) => ({ value: item.value, label: item.label }))];
  const layerOptions = [{ value: '', label: '全部数据分层' }, ...(enums.asset_layer || []).map((item) => ({ value: item.value, label: item.label }))];
  const statusOptions = [{ value: '', label: '全部状态' }, ...(enums.asset_status || []).map((item) => ({ value: item.value, label: item.label }))];

  const searchInput = input(
    { placeholder: '搜索资产名 / 字段名 / 标签', value: state.filters.search },
    (value) => {
      state.filters.search = value;
      scheduleSearch(ctx);
    },
  );

  const controls = el('div.toolbar', null, [
    field('业务域', select(domainOptions, { value: state.filters.domain, onChange: (value) => { state.filters.domain = value; reload(ctx); } })),
    field('数据分层', select(layerOptions, { value: state.filters.layer, onChange: (value) => { state.filters.layer = value; reload(ctx); } })),
    field('资产状态', select(statusOptions, { value: state.filters.status, onChange: (value) => { state.filters.status = value; reload(ctx); } })),
    el('div.field.field-grow', null, [el('label', { text: '关键字检索' }), searchInput]),
    el('div.field', null, [
      el('label', { text: '操作' }),
      el('div.card-actions', null, [
        button('重置筛选', {
          onClick: () => {
            state.filters = { domain: '', layer: '', status: '', search: '' };
            reload(ctx);
          },
        }),
        button('导出目录元数据', {
          onClick: () => window.open('/api/export/metadata', '_blank'),
          title: '导出全部资产定义、血缘与质量模型为 JSON',
        }),
      ]),
    ]),
  ]);

  const facetChips = [];
  const layerChips = LAYER_ORDER.filter((key) => facets.layer?.[key]).map((key) =>
    chip(`${state.meta?.enums?.asset_layer?.find((item) => item.value === key)?.label || key} ${facets.layer[key]}`, null, state.filters.layer === key, () => {
      state.filters.layer = state.filters.layer === key ? '' : key;
      reload(ctx);
    }),
  );
  if (layerChips.length) facetChips.push(chipRow('分层', layerChips));

  const domainChips = Object.entries(facets.domain || {}).map(([key, count]) => {
    const label = state.meta?.enums?.asset_domain?.find((item) => item.value === key)?.label || key;
    return chip(`${label} ${count}`, null, state.filters.domain === key, () => {
      state.filters.domain = state.filters.domain === key ? '' : key;
      reload(ctx);
    });
  });
  if (domainChips.length) facetChips.push(chipRow('业务域', domainChips));

  const ownerChips = Object.entries(facets.owner || {}).map(([key, count]) =>
    chip(`${key} ${count}`, null, false, () => {
      state.filters.search = key === '未指派' ? '' : key;
      reload(ctx);
    }),
  );
  if (ownerChips.length) facetChips.push(chipRow('责任人', ownerChips));

  mount(
    host,
    card('资产检索', el('div', null, [controls, el('div', { style: { marginTop: '14px' } }, facetChips)]), {
      subtitle: `目录共 ${formatInt(payload.total)} 条匹配结果，登记资产 ${formatInt(payload.registry_assets)} 项`,
      actions: [badge(state.filters.search ? `关键字：${state.filters.search}` : '未使用关键字', 'badge-mute')],
    }),
  );
}

let searchTimer = null;
function scheduleSearch(ctx) {
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = setTimeout(() => reload(ctx), 320);
}

async function reload(ctx) {
  const [payload, cityPayload] = await Promise.all([
    api.assets(state.filters).catch(() => null),
    Promise.resolve({ items: state.cities }),
  ]);
  if (payload) state.payload = payload;
  if (cityPayload?.items) state.cities = cityPayload.items;
  const container = document.querySelector('#view');
  if (!container) return;
  renderToolbar(container, ctx);
  renderBody(container, ctx);
}

// ==================================================================
// 资产表
// ==================================================================

function renderBody(container, ctx) {
  const host = container.querySelector('#assets-body');
  const payload = state.payload || {};
  let items = payload.items || [];

  if (!items.length) {
    mount(host, card('资产清单', emptyState({
      title: payload.registry_assets ? '当前筛选条件下没有匹配的资产' : '资产目录为空',
      hint: payload.registry_assets
        ? '可调整业务域、分层、状态或清空关键字后重试。'
        : collectHint('资产定义来自 config/*.yaml，若此处为空请先在「运行审计」中执行「同步元数据」。'),
      actions: [
        button('清空筛选', { onClick: () => { state.filters = { domain: '', layer: '', status: '', search: '' }; reload(ctx); } }),
        button('前往运行审计', { kind: 'primary', onClick: () => ctx.navigate('ops') }),
      ],
    })));
    return;
  }

  items = sortItems(items, state.sort);

  const columns = [
    {
      key: 'name',
      title: '资产',
      sortable: true,
      render: (row) => el('div', null, [
        el('div', { text: row.name || row.key }),
        el('div.mono.text-mute', { text: row.key }),
      ]),
    },
    { key: 'layer', title: '分层', sortable: true, render: (row) => layerBadge(row.layer, row.layer_label) },
    { key: 'domain', title: '业务域', sortable: true, render: (row) => row.domain_label || row.domain || '—' },
    { key: 'owner', title: '责任人', sortable: true, render: (row) => row.owner || '未指派' },
    {
      key: 'granularity',
      title: '粒度 / 频率',
      render: (row) => el('div', null, [
        el('div', { text: row.granularity_label || row.granularity || '—' }),
        el('div.text-mute', { text: row.update_frequency_label || row.update_frequency || '—', style: { fontSize: '11.5px' } }),
      ]),
    },
    { key: 'row_count', title: '行数', align: 'right', sortable: true, render: (row) => formatCompact(row.row_count) },
    { key: 'byte_size', title: '体积', align: 'right', sortable: true, render: (row) => formatBytes(row.byte_size) },
    { key: 'city_count', title: '城市', align: 'right', sortable: true, render: (row) => formatInt(row.city_count) },
    {
      key: 'freshness',
      title: '数据新鲜度',
      sortable: true,
      value: (row) => row.freshness?.delay_minutes ?? -1,
      render: (row) => {
        const info = FRESHNESS_TONE[row.freshness?.state] || FRESHNESS_TONE.unknown;
        return el('div', null, [
          badge(info.label, info.cls),
          el('div.text-mute', { text: row.latest_data_ago || '—', style: { fontSize: '11.5px' } }),
        ]);
      },
    },
    {
      key: 'latest_grade',
      title: '质量',
      sortable: true,
      render: (row) => el('div', { style: { display: 'flex', alignItems: 'center', gap: '8px' } }, [
        gradeBadge(row.latest_grade),
        el('span.text-dim', { text: row.latest_score === null || row.latest_score === undefined ? '未评测' : formatFixed(row.latest_score, 1) }),
      ]),
    },
    {
      key: 'actions',
      title: '导出',
      render: (row) => el('div.card-actions', null, [
        button('CSV', { size: 'sm', onClick: (event) => { event.stopPropagation(); window.open(exportUrl(row.key, { format: 'csv' }), '_blank'); } }),
        button('JSON', { size: 'sm', onClick: (event) => { event.stopPropagation(); window.open(exportUrl(row.key, { format: 'json' }), '_blank'); } }),
      ]),
    },
  ];

  const totals = items.reduce(
    (acc, row) => ({
      rows: acc.rows + (row.row_count || 0),
      bytes: acc.bytes + (row.byte_size || 0),
      breached: acc.breached + (row.freshness?.breached ? 1 : 0),
      graded: acc.graded + (row.latest_grade ? 1 : 0),
    }),
    { rows: 0, bytes: 0, breached: 0, graded: 0 },
  );

  mount(
    host,
    el('div.grid.grid-kpi', null, [
      kpiCard({ label: '匹配资产', value: formatInt(items.length), unit: '项', foot: `登记 ${formatInt(payload.registry_assets)} 项` }),
      kpiCard({ label: '合计行数', value: formatCompact(totals.rows), unit: '行' }),
      kpiCard({ label: '合计体积', value: formatBytes(totals.bytes) }),
      kpiCard({
        label: '超出 SLA',
        value: formatInt(totals.breached),
        unit: '项',
        tone: totals.breached ? 'bad' : 'ok',
        foot: '数据新鲜度不满足声明 SLA',
      }),
      kpiCard({ label: '已评测', value: formatInt(totals.graded), unit: '项', foot: '存在质量评分记录' }),
    ]),
    card('资产清单', table(columns, items, {
      onRowClick: (row) => openAssetDrawer(row.key, ctx),
      sortKey: state.sort.key,
      sortDir: state.sort.dir,
      onSort: (key) => {
        state.sort = { key, dir: state.sort.key === key && state.sort.dir === 'asc' ? 'desc' : 'asc' };
        renderBody(container, ctx);
      },
      empty: '暂无资产',
    }), {
      subtitle: '点击任意行打开资产详情；审计时间已转换为本地时区，数据时间为城市本地时间',
    }),
  );
}

function sortItems(items, sort) {
  const { key, dir } = sort;
  if (!key) return items;
  const factor = dir === 'asc' ? 1 : -1;
  return [...items].sort((a, b) => {
    const left = sortValue(a, key);
    const right = sortValue(b, key);
    if (left === right) return String(a.key).localeCompare(String(b.key));
    if (left === null || left === undefined) return 1;
    if (right === null || right === undefined) return -1;
    if (typeof left === 'number' && typeof right === 'number') return (left - right) * factor;
    return String(left).localeCompare(String(right), 'zh-CN') * factor;
  });
}

function sortValue(row, key) {
  if (key === 'freshness') return row.freshness?.delay_minutes ?? null;
  if (key === 'latest_grade') return row.latest_grade || null;
  return row[key] ?? null;
}

// ==================================================================
// 资产详情抽屉
// ==================================================================

function section(title, { subtitle = null, actions = [] } = {}) {
  const host = el('div');
  const node = card(title, host, { subtitle, actions });
  return {
    node,
    host,
    /** 渲染成功内容 */
    done(content) {
      mount(host, content);
    },
    /** 渲染失败内容 */
    fail(error) {
      mount(host, emptyState({ title: '区块加载失败', hint: error?.message || '未知错误', compact: true }));
    },
  };
}

/** 样例数据单元格格式化：对象/数组折叠显示。 */
function formatCell(value) {
  if (value === null || value === undefined || value === '') return '—';
  if (typeof value === 'object') {
    const text = JSON.stringify(value);
    return el('span.mono.text-dim', { text: text.length > 60 ? `${text.slice(0, 60)}…` : text, title: text });
  }
  if (typeof value === 'number') return Number.isInteger(value) ? formatInt(value) : formatFixed(value, 4);
  const text = String(value);
  return text.length > 40 ? el('span', { text: `${text.slice(0, 40)}…`, title: text }) : text;
}

async function openAssetDrawer(assetKey, ctx) {
  state.selectedKey = assetKey;

  const loading = el('div', null, loadingBlock('正在读取资产详情…'));

  const sections = {
    meta: section('资产卡片'),
    contract: section('业务口径与责任'),
    source: section('来源与许可'),
    storage: section('主键 / 分区 / 存储'),
    columns: section('字段字典'),
    checks: section('质量检查项'),
    versions: section('版本快照'),
    runs: section('最近运行'),
    sample: section('样例数据'),
    profiling: section('列级画像'),
  };

  const body = el('div', null, [
    loading,
    ...Object.values(sections).map((item) => item.node),
  ]);

  openDrawer({
    title: assetKey,
    subtitle: '数据资产详情',
    body,
    onClose: () => {
      state.selectedKey = null;
    },
  });

  let detail;
  try {
    detail = await api.assetDetail(assetKey);
  } catch (error) {
    loading.textContent = '';
    mount(loading, emptyState({
      title: '无法读取该资产详情',
      hint: error.message,
      actions: [button('关闭', { onClick: () => closeDrawer() })],
    }));
    for (const item of Object.values(sections)) item.node.remove();
    return;
  }

  loading.remove();
  const drawer = document.querySelector('.drawer');
  const titleNode = drawer?.querySelector('.drawer-head h2');
  if (titleNode) titleNode.textContent = detail.name || assetKey;
  const subNode = drawer?.querySelector('.drawer-head .card-sub');
  if (subNode) subNode.textContent = `${detail.layer_label || detail.layer || '—'} · ${detail.domain_label || detail.domain || '—'} · ${assetKey}`;

  renderMetaSection(sections.meta, detail);
  renderContractSection(sections.contract, detail);
  renderSourceSection(sections.source, detail);
  renderStorageSection(sections.storage, detail);
  renderColumnsSection(sections.columns, detail);
  renderChecksSection(sections.checks, detail);
  renderVersionsSection(sections.versions, detail);
  renderRunsSection(sections.runs, detail);
  renderSampleAndProfiling(sections, detail, ctx);
}

function renderMetaSection(block, detail) {
  const freshness = detail.freshness || {};
  const tone = FRESHNESS_TONE[freshness.state] || FRESHNESS_TONE.unknown;
  block.done(el('div', null, [
    kvList([
      ['资产名称', detail.name || '—'],
      ['资产标识', el('span.mono', { text: detail.key || '—' })],
      ['数据分层', layerBadge(detail.layer, detail.layer_label)],
      ['业务域', detail.domain_label || detail.domain || '—'],
      ['状态', badge(detail.status_label || detail.status || '—', detail.status === 'active' ? 'badge-ok' : 'badge-mute')],
      ['粒度 / 更新频率', `${detail.granularity_label || detail.granularity || '—'} · ${detail.update_frequency_label || detail.update_frequency || '—'}`],
      ['敏感级别', detail.sensitivity_label || detail.sensitivity || '—'],
      ['行数 / 体积', `${formatInt(detail.row_count)} 行 · ${formatBytes(detail.byte_size)}`],
      ['覆盖城市', `${formatInt(detail.city_count)} 个`],
      ['字段数', `${formatInt(detail.column_count)} 个`],
      ['最新数据时间', `${formatDataTime(detail.latest_data_time)}（城市本地时间，${detail.latest_data_ago || '—'}）`],
      ['数据新鲜度', badge(tone.label, tone.cls)],
      ['最近一次采集', detail.latest_run_at ? formatAuditTime(detail.latest_run_at) : '—'],
      ['最新质量分', detail.latest_score === null || detail.latest_score === undefined
        ? '未评测'
        : el('span', { style: { display: 'inline-flex', gap: '8px', alignItems: 'center' } }, [
            gradeBadge(detail.latest_grade),
            el('span', { text: formatFixed(detail.latest_score, 2) }),
          ])],
      ['近 24h 未通过检查', detail.open_issues_24h === undefined ? '—' : `${formatInt(detail.open_issues_24h)} 项`],
    ]),
    (detail.tags || []).length
      ? el('div', { style: { marginTop: '10px' } }, [el('div.chip-row', null, detail.tags.map((tag) => badge(tag, 'badge-mute')))])
      : null,
  ]));
}

function renderContractSection(block, detail) {
  block.done(el('div', null, [
    el('p', { text: detail.business_definition || '尚未写明业务口径，建议在资产配置中补充 business_definition。', class: detail.business_definition ? '' : 'text-warn' }),
    el('div', { style: { marginTop: '12px' } }, [
      kvList([
        ['责任人', detail.owner || '未指派'],
        ['数据管理员', detail.steward || '未指派'],
        ['SLA（新鲜度）', detail.sla_freshness_minutes ? `${formatInt(detail.sla_freshness_minutes)} 分钟` : '未声明'],
        ['SLA（完整性）', detail.sla_completeness === null || detail.sla_completeness === undefined ? '未声明' : formatPercent(detail.sla_completeness * 100)],
        ['资产说明', detail.description || '—'],
      ]),
    ]),
  ]));
}

function renderSourceSection(block, detail) {
  block.done(el('div', null, [
    kvList([
      ['来源系统', detail.source_system || '—'],
      ['来源产品', detail.source_product || '—'],
      ['数据接口', detail.source_endpoint ? el('span.mono', { text: detail.source_endpoint, style: { wordBreak: 'break-all' } }) : '—'],
      ['接口文档', detail.source_doc ? el('a', { href: detail.source_doc, target: '_blank', rel: 'noopener', text: detail.source_doc }) : '—'],
      ['许可协议', detail.license || '—'],
    ]),
  ]));
}

function renderStorageSection(block, detail) {
  block.done(el('div', null, [
    kvList([
      ['存储路径', detail.storage_uri ? el('span.mono', { text: detail.storage_uri }) : '—'],
      ['存储格式', detail.storage_format || '—'],
      ['时间字段', detail.time_column || '—'],
      ['主键', (detail.primary_key || []).length ? el('span.mono', { text: detail.primary_key.join(', ') }) : '未声明'],
      ['分区键', (detail.partition_keys || []).length ? el('span.mono', { text: detail.partition_keys.join(', ') }) : '未声明'],
      ['模式指纹', detail.schema_hash ? el('span.mono', { text: detail.schema_hash }) : '—'],
      ['创建时间', formatAuditTime(detail.created_at)],
      ['更新时间', formatAuditTime(detail.updated_at)],
    ]),
  ]));
}

function renderColumnsSection(block, detail) {
  const columns = detail.columns || [];
  if (!columns.length) {
    block.done(emptyState({ title: '暂无字段字典', compact: true }));
    return;
  }
  block.done(table(
    [
      { key: 'ordinal', title: '#', align: 'right', render: (row) => formatInt(row.ordinal) },
      { key: 'name', title: '字段名', render: (row) => el('span.mono', { text: row.name }) },
      { key: 'data_type', title: '类型', render: (row) => el('span.mono.text-dim', { text: row.data_type || '—' }) },
      { key: 'unit', title: '单位', render: (row) => row.unit || '—' },
      { key: 'description', title: '业务说明', wrap: true, render: (row) => row.description || el('span.text-warn', { text: '缺少说明' }) },
      {
        key: 'value_range',
        title: '值域',
        render: (row) => (Array.isArray(row.value_range) && row.value_range.length === 2
          ? `${row.value_range[0]} ~ ${row.value_range[1]}`
          : '—'),
      },
      {
        key: 'enum_values',
        title: '枚举值',
        render: (row) => (Array.isArray(row.enum_values) && row.enum_values.length
          ? el('span.mono.text-dim', { text: row.enum_values.join(' / '), title: row.enum_values.join(' / ') })
          : '—'),
      },
      { key: 'key_flags', title: '主键 / 派生', render: (row) => el('div', { style: { display: 'flex', gap: '6px' } }, [
          row.is_primary_key ? badge('主键', 'badge-accent') : null,
          row.is_derived ? badge('派生', 'badge-info') : null,
          row.is_pii ? badge('敏感', 'badge-warn') : null,
          !row.is_nullable ? badge('非空', 'badge-mute') : null,
        ].filter(Boolean)) },
    ],
    columns,
    { empty: '暂无字段' },
  ), note(`共 ${columns.length} 个字段。`));
}

function renderChecksSection(block, detail) {
  const checks = detail.checks || [];
  if (!checks.length) {
    block.done(emptyState({ title: '该资产未配置质量检查项', compact: true }));
    return;
  }
  block.done(el('div.check-list', null, checks.map((check) => el('div.check-item', null, [
    el('div.check-head', null, [
      el('div', null, [
        el('span.check-name', { text: check.name || check.check_key }),
        el('span.check-meta', { text: ` · ${check.dimension_label || check.dimension || ''}` }),
      ]),
      el('div.card-actions', null, [
        badge(check.severity === 'critical' ? '严重' : check.severity === 'major' ? '重要' : '次要',
          check.severity === 'critical' ? 'badge-bad' : check.severity === 'major' ? 'badge-warn' : 'badge-info'),
        check.is_enabled ? badge('已启用', 'badge-ok') : badge('已停用', 'badge-mute'),
      ]),
    ]),
    el('div.check-meta.mono', { text: check.check_key }),
    check.description ? el('div.check-msg', { text: check.description }) : null,
  ]))));
}

function renderVersionsSection(block, detail) {
  const versions = detail.versions || [];
  if (!versions.length) {
    block.done(emptyState({
      title: '暂无版本快照',
      hint: '版本快照在采集完成后由统计刷新生成。',
      compact: true,
    }));
    return;
  }
  block.done(table(
    [
      { key: 'version', title: '版本', render: (row) => el('span.mono', { text: row.version }) },
      { key: 'snapshot_at', title: '快照时间', render: (row) => formatAuditTime(row.snapshot_at) },
      { key: 'row_count', title: '行数', align: 'right', render: (row) => formatInt(row.row_count) },
      { key: 'byte_size', title: '体积', align: 'right', render: (row) => formatBytes(row.byte_size) },
      { key: 'file_count', title: '文件数', align: 'right', render: (row) => formatInt(row.file_count) },
      { key: 'city_count', title: '城市数', align: 'right', render: (row) => formatInt(row.city_count) },
      { key: 'snapshot_ago', title: '距现在', render: (row) => row.snapshot_ago || '—' },
    ],
    versions,
    { empty: '暂无版本' },
  ));
}

function renderRunsSection(block, detail) {
  const runs = detail.recent_runs || [];
  if (!runs.length) {
    block.done(emptyState({ title: '暂无运行记录', hint: '该资产尚未被采集管道写入。', compact: true }));
    return;
  }
  block.done(table(
    [
      { key: 'started_at', title: '开始时间', render: (row) => formatAuditTime(row.started_at) },
      { key: 'job', title: '作业', render: (row) => el('span.mono', { text: row.job || '—' }) },
      { key: 'city_name', title: '城市', render: (row) => row.city_name || row.city_slug || '—' },
      { key: 'status', title: '状态', render: (row) => badge(row.status_label || row.status, row.status === 'success' ? 'badge-ok' : row.status === 'failed' ? 'badge-bad' : 'badge-warn') },
      { key: 'rows_written', title: '写入行数', align: 'right', render: (row) => formatInt(row.rows_written) },
      { key: 'duration_ms', title: '耗时', align: 'right', render: (row) => (row.duration_ms ? `${(row.duration_ms / 1000).toFixed(1)} s` : '—') },
      { key: 'error_message', title: '错误信息', wrap: true, render: (row) => (row.error_message ? el('span.text-bad', { text: row.error_message }) : '—') },
    ],
    runs,
    { empty: '暂无运行记录' },
  ));
}

function renderSampleAndProfiling(sections, detail, ctx) {
  const cityOptions = [{ value: '', label: '全部城市（合并）' }, ...state.cities.map((city) => ({ value: city.slug, label: city.name_zh || city.slug }))];
  const sampleHost = el('div');
  const profileHost = el('div');
  const pageState = { page: 1, limit: 20 };

  const citySelect = select(cityOptions, {
    value: '',
    onChange: (value) => {
      pageState.page = 1;
      loadSample(value);
      loadProfiling(value);
    },
  });

  sections.sample.node.querySelector('.card-head').appendChild(
    el('div.card-actions', null, [citySelect]),
  );
  mount(sections.sample.host, sampleHost);
  mount(sections.profiling.host, profileHost);

  async function loadSample(city) {
    mount(sampleHost, loadingBlock('正在读取样例数据…'));
    let payload;
    try {
      payload = await api.assetSample(detail.key, { city: city || null, limit: 200 });
    } catch (error) {
      mount(sampleHost, emptyState({ title: '样例数据读取失败', hint: error.message, compact: true }));
      return;
    }

    const columns = payload?.columns || [];
    const rows = payload?.rows || [];
    if (!columns.length || !rows.length) {
      mount(sampleHost, emptyState({
        title: '暂无样例数据',
        hint: collectHint('该资产在数据湖中还没有分区文件。'),
        compact: true,
      }));
      return;
    }

    const totalPages = Math.max(1, Math.ceil(rows.length / pageState.limit));

    function renderPage() {
      pageState.page = Math.min(Math.max(1, pageState.page), totalPages);
      const start = (pageState.page - 1) * pageState.limit;
      const pageRows = rows.slice(start, start + pageState.limit);

      const tableNode = table(
        columns.map((name) => ({ key: name, title: name, render: (row) => formatCell(row[name]) })),
        pageRows,
        { empty: '暂无数据行' },
      );

      const pager = el('div.pager', null, [
        el('span', { text: `共 ${formatInt(rows.length)} 行（最多读取 200 行）· 第 ${pageState.page} / ${totalPages} 页` }),
        button('上一页', { size: 'sm', disabled: pageState.page <= 1, onClick: () => { pageState.page -= 1; renderPage(); } }),
        button('下一页', { size: 'sm', disabled: pageState.page >= totalPages, onClick: () => { pageState.page += 1; renderPage(); } }),
      ]);

      mount(sampleHost, tableNode, pager);
    }

    renderPage();
  }

  async function loadProfiling(city) {
    mount(profileHost, loadingBlock('正在计算列级画像…'));
    try {
      const payload = await api.assetProfiling(detail.key, { city: city || null });
      const columns = payload?.columns || [];
      if (!columns.length) {
        mount(profileHost, emptyState({
          title: '暂无画像数据',
          hint: '列级画像基于数据湖中的实际分区计算，无分区时不可用。',
          compact: true,
        }));
        return;
      }
      mount(profileHost, el('div', null, [
        el('div.text-mute', { text: `样本行数：${formatInt(payload.row_count)}${city ? ` · 城市：${city}` : ' · 全部城市合并'}`, style: { marginBottom: '8px' } }),
        table(
          [
            { key: 'name', title: '字段', render: (row) => el('span.mono', { text: row.name }) },
            { key: 'null_rate', title: '空值率', align: 'right', render: (row) => formatPercent(row.null_rate, 3) },
            { key: 'distinct', title: '唯一值', align: 'right', render: (row) => formatInt(row.distinct) },
            { key: 'min', title: '最小', align: 'right', render: (row) => formatFixed(row.min, 3) },
            { key: 'max', title: '最大', align: 'right', render: (row) => formatFixed(row.max, 3) },
            { key: 'mean', title: '均值', align: 'right', render: (row) => formatFixed(row.mean, 3) },
            { key: 'p50', title: '中位数', align: 'right', render: (row) => formatFixed(row.p50, 3) },
            { key: 'std', title: '标准差', align: 'right', render: (row) => formatFixed(row.std, 3) },
            {
              key: 'out_of_range',
              title: '越界值',
              align: 'right',
              render: (row) => (row.out_of_range === null || row.out_of_range === undefined
                ? '—'
                : row.out_of_range > 0
                  ? el('span.text-bad', { text: formatInt(row.out_of_range) })
                  : '0'),
            },
          ],
          columns,
          { empty: '暂无字段画像' },
        ),
      ]));
    } catch (error) {
      mount(profileHost, emptyState({ title: '列级画像读取失败', hint: error.message, compact: true }));
    }
  }

  loadSample('');
  loadProfiling('');
}
