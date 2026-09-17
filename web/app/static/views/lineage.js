/**
 * lineage.js —— 数据血缘。
 *
 * 渲染方式：
 *  * 依据 `layer_order` 把节点分列成「参考层 → 原始层 → 精炼层 → 服务层」四列；
 *  * 用一层绝对定位的 SVG 叠加层绘制节点卡片之间的三次贝塞尔连线，
 *    连线标签标注 `transform_label` 与 `transform_ref`；
 *  * 节点左边框按质量等级着色，鼠标悬停高亮其直接上下游，点击打开影响分析面板。
 *
 * 坐标计算依赖 DOM 实际布局，因此所有连线绘制都放在 `requestAnimationFrame`
 * 或 `ResizeObserver` 回调中执行，保证在数据前后到达、窗口缩放时都正确。
 */

import { api } from '/shared/api.js';
import {
  badge,
  button,
  card,
  el,
  emptyState,
  formatInt,
  gradeBadge,
  gradeColor,
  kpiCard,
  layerBadge,
  legendItem,
  mount,
  note,
  openDrawer,
  select,
  table,
} from '/shared/ui.js';

const SVG_NS = 'http://www.w3.org/2000/svg';
const GRADE_LEGEND = [
  ['A', '#22c55e'],
  ['B', '#38bdf8'],
  ['C', '#f59e0b'],
  ['D', '#f97316'],
  ['E', '#ef4444'],
  ['未评测', '#64748b'],
];

let state = {
  graph: null,
  metrics: { nodes: new Map(), edges: [] },
  viewBox: { width: 0, height: 0 },
  selected: null,
  impact: null,
  impactDirection: 'both',
  impactDepth: 6,
  highlighted: new Set(),
  observer: null,
};

export async function render(container, ctx, params) {
  mount(container, emptyState({ title: '正在加载血缘图谱…', compact: true }));

  const [graph, orphans] = await Promise.all([
    api.lineageGraph().catch((error) => ({ __error: error })),
    api.lineageOrphans().catch(() => null),
  ]);

  if (graph?.__error) {
    mount(container, card('数据血缘', emptyState({
      title: '血缘图谱读取失败',
      hint: graph.__error.message,
      actions: [button('重新加载', { kind: 'primary', onClick: () => ctx.refresh() })],
    })));
    return;
  }

  const nodes = graph.nodes || [];
  const edges = graph.edges || [];

  state.graph = graph;
  state.metrics = {
    nodes: new Map(nodes.map((node) => [node.key, node])),
    edges,
  };

  const stats = graph.stats || {};
  const stageHost = el('div');
  const edgeInfo = el('div.text-mute', { text: '将鼠标移到连线上可查看转换说明；点击节点可做影响分析。', style: { fontSize: '12px' } });

  mount(
    container,
    el('div.grid.grid-6', null, [
      kpiCard({ label: '血缘节点', value: formatInt(stats.node_count), unit: '个', foot: `数据源 ${formatInt(stats.source_count)} 个 · 末端 ${formatInt(stats.sink_count)} 个` }),
      kpiCard({ label: '血缘连线', value: formatInt(stats.edge_count), unit: '条', foot: `平均上游 ${stats.avg_upstream ?? 0} 条` }),
      kpiCard({ label: '最大链路深度', value: formatInt(stats.max_depth), unit: '层' }),
      kpiCard({
        label: '孤立资产',
        value: formatInt(stats.isolated_count),
        unit: '项',
        tone: stats.isolated_count ? 'warn' : 'ok',
        foot: '既无上游也无下游',
      }),
      kpiCard({
        label: '未写说明的连线',
        value: formatInt(orphans?.edges_without_documentation?.length || 0),
        unit: '条',
        tone: (orphans?.edges_without_documentation?.length || 0) ? 'warn' : 'ok',
      }),
      kpiCard({
        label: '血缘健康度',
        value: orphans?.healthy ? '健康' : '待治理',
        tone: orphans?.healthy ? 'ok' : 'warn',
        foot: '孤立资产与连线文档的合并判定',
      }),
    ]),
    card('血缘图谱', el('div', null, [
      renderToolbar(ctx),
      stageHost,
      el('div', { style: { marginTop: '10px' } }, [edgeInfo]),
      el('div.legend', { style: { marginTop: '10px' } }, [
        el('span.text-mute', { text: '节点左边框颜色 = 最新质量等级：' }),
        ...GRADE_LEGEND.map(([label, color]) => legendItem(color, label)),
      ]),
    ]), {
      subtitle: '列顺序遵循数据湖分层；连线方向为上游 → 下游',
    }),
    renderTransformDistribution(graph),
  );

  if (!nodes.length) {
    mount(stageHost, emptyState({
      title: '暂无血缘数据',
      hint: '血缘边来自 config/lineage.yaml，资产目录为空时不会产生节点。<br />请先在「运行审计」中执行「同步元数据」。',
      actions: [button('前往运行审计', { kind: 'primary', onClick: () => ctx.navigate('ops') })],
    }));
    return;
  }

  renderStage(stageHost, ctx, edgeInfo);

  // 支持 #/lineage/<asset_key> 直接选中某节点
  const initialKey = Array.isArray(params) ? params[0] : null;
  if (initialKey && state.metrics.nodes.has(initialKey)) {
    selectNode(initialKey, ctx, edgeInfo);
  }

  bindResize(stageHost);
}
export function destroy() {
  if (state.observer) {
    state.observer.disconnect();
    state.observer = null;
  }
  state = {
    graph: null,
    metrics: { nodes: new Map(), edges: [] },
    viewBox: { width: 0, height: 0 },
    selected: null,
    impact: null,
    impactDirection: 'both',
    impactDepth: 6,
    highlighted: new Set(),
    observer: null,
  };
}

// ==================================================================
// 工具栏
// ==================================================================

function renderToolbar(ctx) {
  const directionSelect = select(
    [
      { value: 'both', label: '上下游双向' },
      { value: 'downstream', label: '仅下游（影响面）' },
      { value: 'upstream', label: '仅上游（问题溯源）' },
    ],
    {
      value: state.impactDirection,
      onChange: (value) => {
        state.impactDirection = value;
        if (state.selected) loadImpact(state.selected, ctx);
      },
    },
  );

  const depthSelect = select(
    [2, 3, 4, 6, 10, 20].map((value) => ({ value: String(value), label: `${value} 层` })),
    {
      value: String(state.impactDepth),
      onChange: (value) => {
        state.impactDepth = Number(value);
        if (state.selected) loadImpact(state.selected, ctx);
      },
    },
  );

  return el('div.toolbar', { style: { marginBottom: '12px' } }, [
    el('div.field', null, [el('label', { text: '影响分析方向' }), directionSelect]),
    el('div.field', null, [el('label', { text: '追溯深度' }), depthSelect]),
    el('div.field', null, [
      el('label', { text: '操作' }),
      el('div.card-actions', null, [
        button('刷新图谱', { onClick: () => ctx.refresh() }),
        button('查看质量', { onClick: () => ctx.navigate('quality') }),
      ]),
    ]),
  ]);
}

// ==================================================================
// 图谱主体
// ==================================================================

function renderStage(host, ctx, edgeInfo) {
  const graph = state.graph || {};
  const layerOrder = graph.layer_order?.length ? graph.layer_order : ['reference', 'raw', 'refined', 'serving'];
  const layerLabels = graph.layer_labels || {};
  const nodes = graph.nodes || [];

  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('class', 'lineage-edges');
  svg.setAttribute('width', '100%');
  svg.setAttribute('height', '100%');

  const defs = document.createElementNS(SVG_NS, 'defs');
  const marker = document.createElementNS(SVG_NS, 'marker');
  marker.setAttribute('id', 'lineage-arrow');
  marker.setAttribute('viewBox', '0 0 10 10');
  marker.setAttribute('refX', '8');
  marker.setAttribute('refY', '5');
  marker.setAttribute('markerWidth', '6');
  marker.setAttribute('markerHeight', '6');
  marker.setAttribute('orient', 'auto-start-reverse');
  const arrowPath = document.createElementNS(SVG_NS, 'path');
  arrowPath.setAttribute('d', 'M 0 0 L 10 5 L 0 10 z');
  arrowPath.setAttribute('fill', '#4a5b7a');
  marker.appendChild(arrowPath);
  defs.appendChild(marker);
  svg.appendChild(defs);

  const stage = el('div.lineage-stage');
  const columns = layerOrder.map((layer) => {
    const columnNodes = nodes.filter((node) => node.layer === layer);
    const column = el('div.lineage-col', null, [
      el('div.lineage-col-head', null, [
        el('span.swatch', { style: { background: layerColor(layer) } }),
        el('span', { text: `${layerLabels[layer] || layer}（${columnNodes.length}）` }),
      ]),
      ...columnNodes.map((node) => nodeCard(node, ctx, edgeInfo)),
    ]);
    return column;
  });

  // 未在 layer_order 中出现的分层（异常情况）兜底成额外一列
  const knownLayers = new Set(layerOrder);
  const extra = [...new Set(nodes.map((node) => node.layer).filter((layer) => layer && !knownLayers.has(layer)))];
  for (const layer of extra) {
    columns.push(el('div.lineage-col', null, [
      el('div.lineage-col-head', null, [el('span.swatch', { style: { background: layerColor(layer) } }), el('span', { text: `${layer}（${nodes.filter((item) => item.layer === layer).length}）` })]),
      ...nodes.filter((node) => node.layer === layer).map((node) => nodeCard(node, ctx, edgeInfo)),
    ]));
  }

  mount(stage, svg, ...columns);
  mount(host, stage);

  // 布局完成后绘制连线
  window.requestAnimationFrame(() => {
    if (stage.isConnected) drawEdges(stage, svg, edgeInfo);
  });
}

function bindResize(host) {
  if (state.observer) state.observer.disconnect();
  const stage = host.querySelector('.lineage-stage');
  const svg = host.querySelector('.lineage-edges');
  if (!stage || !svg) return;
  state.observer = new ResizeObserver(() => {
    if (stage.isConnected) drawEdges(stage, svg, null);
  });
  state.observer.observe(stage);
}

function nodeCard(node, ctx, edgeInfo) {
  const grade = node.latest_grade;
  const card_ = el('div.node', {
    class: state.selected === node.key ? 'selected' : '',
    dataset: { key: node.key },
    style: { borderLeftColor: gradeColor(grade) },
    onClick: () => selectNode(node.key, ctx, edgeInfo),
  }, [
    el('div.node-badge', null, [gradeBadge(grade)]),
    el('div.node-name', { text: node.name || node.key }),
    el('div.node-key', { text: node.key }),
    el('div.node-foot', null, [
      el('span', { text: `上游 ${node.upstream_count ?? 0} · 下游 ${node.downstream_count ?? 0}` }),
      el('span', { text: node.latest_data_ago || '无数据' }),
    ]),
  ]);

  // 悬停高亮直接上下游
  card_.addEventListener('mouseenter', () => {
    const related = new Set([node.key]);
    for (const edge of state.metrics.edges) {
      if (edge.source === node.key) related.add(edge.target);
      if (edge.target === node.key) related.add(edge.source);
    }
    state.highlighted = related;
    applyHighlight(node.key);
  });
  card_.addEventListener('mouseleave', () => {
    state.highlighted = new Set();
    applyHighlight(null);
  });

  return card_;
}

function applyHighlight(focusKey) {
  const stage = document.querySelector('.lineage-stage');
  const svg = document.querySelector('.lineage-edges');
  if (!stage || !svg) return;

  for (const node of stage.querySelectorAll('.node')) {
    const key = node.dataset.key;
    if (!focusKey) {
      node.classList.remove('muted', 'highlight');
      continue;
    }
    const related = state.highlighted.has(key);
    node.classList.toggle('muted', !related);
    node.classList.toggle('highlight', related && key !== focusKey);
  }
  for (const path of svg.querySelectorAll('path[data-edge]')) {
    if (!focusKey) {
      path.setAttribute('opacity', '1');
      continue;
    }
    path.setAttribute('opacity', path.dataset.source === focusKey || path.dataset.target === focusKey ? '1' : '0.15');
  }
}

// ==================================================================
// 连线绘制
// ==================================================================

function drawEdges(stage, svg, edgeInfo) {
  if (!stage || !svg) return;
  const box = stage.getBoundingClientRect();
  state.viewBox = { width: box.width, height: box.height };
  svg.setAttribute('viewBox', `0 0 ${Math.max(1, box.width)} ${Math.max(1, box.height)}`);

  for (const node of [...svg.querySelectorAll('g.edge-group')]) node.remove();

  const positions = new Map();
  for (const node of stage.querySelectorAll('.node')) {
    const rect = node.getBoundingClientRect();
    positions.set(node.dataset.key, {
      left: rect.left - box.left,
      top: rect.top - box.top,
      right: rect.right - box.left,
      bottom: rect.bottom - box.top,
      height: rect.height,
      width: rect.width,
    });
  }

  for (const edge of state.metrics.edges) {
    const source = positions.get(edge.source);
    const target = positions.get(edge.target);
    if (!source || !target) continue;

    // 同行（同列）相邻节点直接竖线；跨列则用水平方向的三次贝塞尔
    const sameColumn = Math.abs(source.right - target.right) < 2;
    const startX = sameColumn ? source.right - source.width / 2 : source.right;
    const startY = sameColumn ? source.bottom : source.top + source.height / 2;
    const endX = sameColumn ? target.right - target.width / 2 : target.left;
    const endY = sameColumn ? target.top : target.top + target.height / 2;

    const group = document.createElementNS(SVG_NS, 'g');
    group.setAttribute('class', 'edge-group');

    const visible = document.createElementNS(SVG_NS, 'path');
    visible.setAttribute('data-edge', '1');
    visible.dataset.source = edge.source;
    visible.dataset.target = edge.target;
    visible.setAttribute('d', bezier(startX, startY, endX, endY, sameColumn));
    visible.setAttribute('fill', 'none');
    visible.setAttribute('stroke', gradeColor(state.metrics.nodes.get(edge.source)?.latest_grade));
    visible.setAttribute('stroke-width', '1.6');
    visible.setAttribute('stroke-opacity', '0.7');
    visible.setAttribute('marker-end', 'url(#lineage-arrow)');

    // 加粗的透明命中区，便于悬停与点击
    const hit = document.createElementNS(SVG_NS, 'path');
    hit.setAttribute('d', bezier(startX, startY, endX, endY, sameColumn));
    hit.setAttribute('fill', 'none');
    hit.setAttribute('stroke', 'transparent');
    hit.setAttribute('stroke-width', '14');
    hit.setAttribute('class', 'edge-hit');
    hit.addEventListener('mouseenter', () => {
      visible.setAttribute('stroke-width', '3');
      visible.setAttribute('stroke-opacity', '1');
      if (edgeInfo) {
        edgeInfo.textContent = `${edge.source} → ${edge.target} · ${edge.transform_label || edge.transform_type || '未声明转换类型'}${edge.transform_ref ? ` · ${edge.transform_ref}` : ''}${edge.description ? ` · ${edge.description}` : ' · （未填写连线说明）'}`;
      }
    });
    hit.addEventListener('mouseleave', () => {
      visible.setAttribute('stroke-width', '1.6');
      visible.setAttribute('stroke-opacity', '0.7');
      if (edgeInfo) edgeInfo.textContent = '将鼠标移到连线上可查看转换说明；点击节点可做影响分析。';
    });
    hit.addEventListener('click', () => {
      if (edgeInfo) {
        edgeInfo.textContent = `${edge.source} → ${edge.target} · ${edge.transform_label || edge.transform_type || '未声明转换类型'}${edge.transform_ref ? ` · 参考：${edge.transform_ref}` : ''}${edge.description ? ` · 说明：${edge.description}` : ''}`;
      }
    });

    group.appendChild(visible);
    group.appendChild(hit);

    // 标签：只标注跨列连线，避免同列连线标签堆叠
    if (!sameColumn) {
      const midX = (startX + endX) / 2;
      const midY = (startY + endY) / 2;
      const label = edge.transform_label || edge.transform_type || '转换';
      const text = document.createElementNS(SVG_NS, 'text');
      text.setAttribute('x', String(midX));
      text.setAttribute('y', String(midY - 4));
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('class', 'edge-label');
      text.textContent = edge.transform_ref ? `${label}` : label;
      group.appendChild(text);

      if (edge.transform_ref) {
        const ref = document.createElementNS(SVG_NS, 'text');
        ref.setAttribute('x', String(midX));
        ref.setAttribute('y', String(midY + 10));
        ref.setAttribute('text-anchor', 'middle');
        ref.setAttribute('class', 'edge-label');
        ref.setAttribute('opacity', '0.72');
        ref.textContent = String(edge.transform_ref).length > 26
          ? `${String(edge.transform_ref).slice(0, 26)}…`
          : String(edge.transform_ref);
        group.appendChild(ref);
      }
    }

    svg.appendChild(group);
  }
}

function bezier(x1, y1, x2, y2, vertical) {
  if (vertical) {
    const midY = (y1 + y2) / 2;
    return `M ${x1} ${y1} C ${x1} ${midY}, ${x2} ${midY}, ${x2} ${y2}`;
  }
  // 控制点水平偏移取列间距的 40%（含上下限约束），
  // 避免列间距过小时出现回折、间距过大时曲线过平。
  const gap = Math.max(1, Math.abs(x2 - x1));
  const dx = Math.min(Math.max(12, gap * 0.4), gap);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

function layerColor(layer) {
  return { reference: '#8b9cb6', raw: '#38bdf8', refined: '#a78bfa', serving: '#22c55e' }[layer] || '#64748b';
}

// ==================================================================
// 影响分析
// ==================================================================

function selectNode(key, ctx, edgeInfo) {
  state.selected = key;

  for (const node of document.querySelectorAll('.lineage-stage .node')) {
    node.classList.toggle('selected', node.dataset.key === key);
  }

  loadImpact(key, ctx);
}

async function loadImpact(key, ctx) {
  const node = state.metrics.nodes.get(key);
  const host = el('div', null, emptyState({ title: '正在追溯血缘影响面…', compact: true }));

  openDrawer({
    title: node?.name || key,
    subtitle: `影响分析 · ${state.impactDirection === 'both' ? '上下游双向' : state.impactDirection === 'downstream' ? '仅下游' : '仅上游'} · 最大深度 ${state.impactDepth} 层`,
    body: host,
    headActions: [button('在目录中查看', {
      size: 'sm',
      onClick: () => ctx.navigate('assets', [key]),
    })],
  });

  let payload;
  try {
    payload = await api.lineageImpact(key, { direction: state.impactDirection, maxDepth: state.impactDepth });
  } catch (error) {
    mount(host, emptyState({ title: '影响分析失败', hint: error.message, compact: true }));
    return;
  }

  state.impact = payload;
  renderImpact(host, payload, ctx, key);
}

function renderImpact(host, payload, ctx, key) {
  const levels = payload?.levels || [];
  const edges = payload?.edges || [];
  const summary = payload?.summary || {};

  mount(
    host,
    !payload?.found
      ? emptyState({ title: '未找到该资产的血缘记录', compact: true })
      : el('div', null, [
          el('div.grid.grid-3', null, [
            kpiCard({ label: '关联资产', value: formatInt(summary.related_count), unit: '项' }),
            kpiCard({ label: '到达深度', value: formatInt(summary.depth_reached), unit: '层' }),
            kpiCard({ label: '经过连线', value: formatInt(edges.length), unit: '条' }),
          ]),
          el('div', { style: { marginTop: '16px' } }, [
            el('div.section-title', { text: '按层展开的关联资产' }),
            ...levels.map((level) => el('div', { style: { marginBottom: '14px' } }, [
              el('div', { style: { marginBottom: '6px' } }, [
                badge(level.depth === 0 ? '起点资产' : `第 ${level.depth} 层`, level.depth === 0 ? 'badge-accent' : 'badge-mute'),
              ]),
              table(
                [
                  {
                    key: 'name',
                    title: '资产',
                    render: (row) => el('div', null, [
                      el('div', { text: row.name || row.key }),
                      el('div.mono.text-mute', { text: row.key }),
                    ]),
                  },
                  { key: 'layer', title: '分层', render: (row) => layerBadge(row.layer) },
                  { key: 'owner', title: '责任人', render: (row) => row.owner || '未指派' },
                  { key: 'city_count', title: '城市', align: 'right', render: (row) => formatInt(row.city_count) },
                  { key: 'latest_grade', title: '质量', render: (row) => gradeBadge(row.latest_grade) },
                  { key: 'latest_data_ago', title: '数据新鲜度', render: (row) => row.latest_data_ago || '无数据' },
                ],
                level.nodes || [],
                {
                  empty: '无关联资产',
                  onRowClick: (row) => {
                    state.selected = row.key;
                    loadImpact(row.key, ctx);
                  },
                },
              ),
            ])),
          ]),
          el('div', { style: { marginTop: '12px' } }, [
            el('div.section-title', { text: '经过的血缘连线' }),
            table(
              [
                { key: 'source', title: '上游', render: (row) => el('span.mono', { text: row.source }) },
                { key: 'target', title: '下游', render: (row) => el('span.mono', { text: row.target }) },
                { key: 'transform_label', title: '转换类型', render: (row) => row.transform_label || row.transform_type || '—' },
                { key: 'transform_ref', title: '转换引用', render: (row) => row.transform_ref || '—' },
                { key: 'description', title: '说明', wrap: true, render: (row) => row.description || el('span.text-warn', { text: '未填写说明' }) },
              ],
              edges,
              { empty: '无连线' },
            ),
          ]),
          note('上游追溯用于定位"数据脏了问题出在哪"，下游追溯用于评估"这个资产坏了会影响谁"。'),
        ]),
  );
}

// ==================================================================
// 转换类型分布
// ==================================================================

function renderTransformDistribution(graph) {
  const stats = graph.stats || {};
  const labels = graph.transform_labels || {};
  const distribution = stats.transform_distribution || {};
  const rows = Object.entries(distribution)
    .sort((a, b) => b[1] - a[1])
    .map(([key, count]) => ({ key, label: labels[key] || key, count }));

  if (!rows.length) {
    return card('转换类型分布', emptyState({ title: '暂无血缘连线', compact: true }));
  }

  return card('转换类型分布', table(
    [
      { key: 'label', title: '转换类型' },
      { key: 'key', title: '标识', render: (row) => el('span.mono.text-mute', { text: row.key }) },
      { key: 'count', title: '连线数', align: 'right', render: (row) => formatInt(row.count) },
    ],
    rows,
    { empty: '暂无数据' },
  ), { subtitle: '转换类型取自资产定义中的血缘边声明' });
}
