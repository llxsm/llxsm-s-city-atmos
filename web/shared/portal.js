/**
 * portal.js —— 统一外壳的哈希路由、门户切换与外壳引导。
 *
 * 两个后端服务（分析 8000 / 治理 8001）托管**同一份**前端外壳，因此路由与
 * 外壳只有一份实现，门户差异全部收进 `startPortal(config)` 的 config：
 *
 *   config = {
 *     defaultPortal: 'analysis',
 *     appName: '城市环境数据平台',
 *     portals: {
 *       analysis: {
 *         name: '城市环境数据分析平台',
 *         shortName: '分析平台',                 // 顶栏切换器按钮文字
 *         subtitle: '从实时实况到归因与考核',
 *         defaultRoute: 'digest',
 *         nav: [{ section: '概览' },             // section 项只作为分组标题
 *               { route, title, subtitle, module }],
 *         tasks: { fetch, label, source },       // 省略表示该门户没有任务登记簿
 *         capabilities: { insights: true, ... }, // 覆盖 DEFAULT_CAPABILITIES
 *         sidebarMeta: [[term, value]],
 *       },
 *       governance: { … },
 *     },
 *   }
 *
 * 路由一律带门户前缀：`#/<portal>/<route>[/param…][?query]`。
 * 这是硬性约定而不是装饰：两个门户都有名为 `ops` 的路由（分析「数据刷新」、
 * 治理「运行审计」），不加前缀就会互相覆盖。
 *
 * 启动时门户的选取顺序：
 *   1. 地址栏 hash 中合法且已配置的门户；
 *   2. `localStorage['atmos.portal']`；
 *   3. 服务端注入的 `window.__ATMOS_PORTALS__.current`；
 *   4. `config.defaultPortal`。
 * 每个门户各自记住最后访问的路由（内存 + localStorage），来回切换会回到原处。
 *
 * 视图模块约定（视图无需感知门户细节）：
 *   export async function render(container, ctx, params)
 *   export function destroy()                      // 可选
 *
 * 生命周期：
 *   1. 路由变化或门户切换前调用上一视图的 destroy()，并销毁 #view 子树内的全部
 *      ECharts 实例；
 *   2. 视图模块用动态 import() 懒加载，加载失败时给出内联提示而不是抛出；
 *   3. 视图抛错只在 #view 内展示错误卡片，外壳（顶栏、导航、切换器）保持可用；
 *   4. 当前门户后端不可达时，同样只在本页内容区给出中文提示与「重试」，
 *      绝不白屏，也不影响另一个门户。
 *
 * 视图侧统一拿到 ctx：
 *   { portal, route, params, query, navigate(name, params, query), url(name, params, query),
 *     refresh(), isActive(), toast, can(kind) }
 *
 *   `ctx.navigate(name, …)` 与 `ctx.url(name, …)` **只在本门户内解析**：
 *   视图写 `ctx.navigate('ops')` 就跳到"本门户的 ops"，无需也不应写门户前缀。
 *
 * `ctx.can(kind)` 回答"本门户是否提供这类端点"：
 *   'assets'   —— 资产目录 /api/assets、/api/assets/{key}[/sample|/profiling]
 *   'cities'   —— 城市主数据 /api/cities
 *   'insights' —— /api/insights/*
 *   'quality'  —— /api/quality/*
 *   'lineage'  —— /api/lineage/*
 *   'opsAudit' —— /api/ops/runs、/api/ops/summary
 * 视图在调用前用 ctx.can() 判断，可以避免运行期 404（两个门户的 API 面并不相同）。
 */

import { api, onStatus, getStatus, clearLastError, setActivePortal, servedPortal } from '/shared/api.js';
import { disposeCharts } from '/shared/charts.js';
import {
  configureTasks,
  onTasks,
  requestTaskRefresh,
  taskKindLabel,
  taskParamsText,
} from '/shared/tasks.js';
import { banner, clearBanners, el, mount, progressBar, qs, runStatusBadge, toast } from '/shared/ui.js';

const PORTAL_IDS = ['analysis', 'governance'];
const PORTAL_KEY = 'atmos.portal';
const ROUTE_KEY = (portal) => `atmos.route.${portal}`;
const HEALTH_INTERVAL_MS = 60000;

const PORTAL_LABEL = { analysis: '分析平台', governance: '数据治理' };

/** 门户能力表：默认按门户名给出，可在 portal.capabilities 里覆盖。 */
const DEFAULT_CAPABILITIES = {
  analysis: {
    assets: false,
    cities: false,
    insights: true,
    quality: false,
    lineage: false,
    opsAudit: false,
    collect: true,
  },
  governance: {
    assets: true,
    cities: true,
    insights: false,
    quality: true,
    lineage: true,
    opsAudit: true,
    collect: false,
  },
};

// ==================================================================
// 小工具
// ==================================================================

function safeStorage() {
  try {
    return window.localStorage || null;
  } catch {
    // 隐私模式等场景下访问 localStorage 会抛错：退化为"不记忆"，不影响使用
    return null;
  }
}

function storageGet(key) {
  try {
    return safeStorage()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    safeStorage()?.setItem(key, String(value));
  } catch {
    /* 忽略写入失败 */
  }
}

function normalizeNav(nav) {
  const items = [];
  let section = '';
  for (const entry of nav || []) {
    if (entry.section) {
      section = entry.section;
      items.push({ kind: 'section', label: entry.section, key: `section:${entry.section}` });
      continue;
    }
    items.push({
      kind: 'route',
      route: entry.route,
      title: entry.title,
      subtitle: entry.subtitle || '',
      module: entry.module,
      section,
    });
  }
  return items;
}

/** 把 config 里的门户声明规范化：路由表、默认路由、能力表一次算清。 */
function buildPortalModel(config) {
  const declared = config.portals && typeof config.portals === 'object' ? config.portals : {};
  const models = new Map();

  for (const portal of PORTAL_IDS) {
    const entry = declared[portal];
    if (!entry) continue;
    const nav = normalizeNav(entry.nav);
    const routes = new Map(nav.filter((item) => item.kind === 'route').map((item) => [item.route, item]));
    const defaultRoute = routes.has(entry.defaultRoute) ? entry.defaultRoute : routes.keys().next().value;
    models.set(portal, {
      id: portal,
      name: entry.name || PORTAL_LABEL[portal],
      shortName: entry.shortName || PORTAL_LABEL[portal],
      subtitle: entry.subtitle || '',
      brandSub: entry.brandSub || '',
      brandMark: entry.brandMark || portal.slice(0, 2).toUpperCase(),
      defaultRoute,
      nav,
      routes,
      tasks: entry.tasks || null,
      sidebarMeta: Array.isArray(entry.sidebarMeta) ? entry.sidebarMeta : [],
      capabilities: { ...(DEFAULT_CAPABILITIES[portal] || {}), ...(entry.capabilities || {}) },
      documentTitle: entry.documentTitle || entry.name || PORTAL_LABEL[portal],
    });
  }

  return models;
}

// ==================================================================
// 启动
// ==================================================================

export function startPortal(config) {
  const MODELS = buildPortalModel(config);
  const AVAILABLE = [...MODELS.keys()];
  const APP_NAME = config.appName || '城市环境数据平台';
  const FALLBACK_PORTAL = AVAILABLE.includes(config.defaultPortal) ? config.defaultPortal : AVAILABLE[0];

  if (!FALLBACK_PORTAL) throw new Error('startPortal：config.portals 至少要声明一个门户');

  /** 当前门户：整份外壳（导航、视图、任务面板、健康状态）都以它为准。 */
  let portal = pickInitialPortal();
  /** 每个门户最后一次浏览的路由（切换回来时恢复）。 */
  const lastRoute = {};
  for (const id of AVAILABLE) lastRoute[id] = storageGet(ROUTE_KEY(id)) || null;

  let current = { name: null, instance: null }; // 当前已挂载的视图
  let autoRefreshAt = 0;
  let lastErrorText = '';
  let lastBannerError = '';
  let booted = false;

  /** 本页后端可用性（整个会话按门户缓存，避免每次渲染都探测）。 */
  const LIVE = new Map(AVAILABLE.map((id) => [id, null]));
  /** 各门户最近一次 health 载荷：切回某门户时顶栏信息立即可用。 */
  const healthByPortal = new Map();
  let activeLive = null;
  let activeLiveChecked = false;
  let healthBusy = false;

  setActivePortal(portal);

  // ==================================================================
  // 门户选取与路由
  // ==================================================================

  function pickInitialPortal() {
    const fromHash = portalFromHash();
    if (fromHash) return fromHash;
    const remembered = storageGet(PORTAL_KEY);
    if (AVAILABLE.includes(remembered)) return remembered;
    const injected = servedPortal();
    if (injected && AVAILABLE.includes(injected)) return injected;
    return FALLBACK_PORTAL;
  }

  function portalFromHash() {
    const raw = (window.location.hash || '').replace(/^#\/?/, '');
    const [pathPart] = raw.split('?');
    const segments = pathPart.split('/').filter(Boolean).map((item) => decodeURIComponent(item));
    return AVAILABLE.includes(segments[0]) ? segments[0] : null;
  }

  /** 解析当前 hash；门户总是取"当前门户"，路由取本门户路由表内的名字。 */
  function parseHash(activePortal) {
    const model = MODELS.get(activePortal);
    const raw = (window.location.hash || '').replace(/^#\/?/, '');
    const [pathPart, queryPart] = raw.split('?');
    const segments = pathPart.split('/').filter(Boolean).map((item) => decodeURIComponent(item));

    let rest = segments;
    if (AVAILABLE.includes(segments[0])) rest = segments.slice(1); // 丢掉门户段
    const known = model.routes.has(rest[0]);
    // 首段不是本门户的路由名时，整段都是无效输入：既回落默认路由，也不把
    // 垃圾段当成 params 传给视图（否则 #/bogus 会渲染成 "#/analysis/digest/bogus"）
    const name = known ? rest[0] : defaultRouteFor(activePortal);
    const params = known ? rest.slice(1) : [];

    const query = {};
    if (queryPart) {
      for (const [key, value] of new URLSearchParams(queryPart).entries()) query[key] = value;
    }
    return { name, segments: params, query };
  }

  function defaultRouteFor(activePortal) {
    const model = MODELS.get(activePortal);
    const remembered = lastRoute[activePortal];
    return remembered && model.routes.has(remembered) ? remembered : model.defaultRoute;
  }

  function hashUrl(activePortal, name, params = [], query = null) {
    const model = MODELS.get(activePortal);
    const route = model.routes.has(name) ? name : defaultRouteFor(activePortal);
    const path = [activePortal, route, ...params.map((item) => encodeURIComponent(String(item)))].join('/');
    const search = query && Object.keys(query).length ? `?${new URLSearchParams(query).toString()}` : '';
    return `#/${path}${search}`;
  }

  function navigate(name, params = [], query = null) {
    const hash = hashUrl(portal, name, params, query);
    if (window.location.hash === hash) {
      renderCurrent();
      return;
    }
    window.location.hash = hash;
  }

  /** 切换门户：改 hash，剩下的交给 hashchange → renderCurrent()。 */
  function switchPortal(next) {
    if (!MODELS.has(next) || next === portal) return;
    // 记下离开时的位置，回来时原地恢复
    lastRoute[portal] = parseHash(portal).name;
    window.location.hash = hashUrl(next, defaultRouteFor(next));
  }

  // ==================================================================
  // 外壳渲染
  // ==================================================================

  function renderShell() {
    const model = MODELS.get(portal);

    const brandTitle = qs('#brandTitle');
    if (brandTitle) brandTitle.textContent = model.name;
    const brandSub = qs('#brandSub');
    if (brandSub) brandSub.textContent = model.brandSub;
    const brandMark = qs('#brandMark');
    if (brandMark) brandMark.textContent = model.brandMark;

    const nav = qs('#nav');
    if (nav) {
      mount(nav, ...model.nav.map((item) => {
        if (item.kind === 'section') {
          return el('div.nav-section', { text: item.label });
        }
        return el('a.nav-item', { href: hashUrl(portal, item.route), title: item.subtitle || item.title }, [
          el('span.nav-dot'),
          el('span.nav-label', { text: item.title }),
        ]);
      }));
    }

    const footLeft = qs('#footLeft');
    if (footLeft) footLeft.textContent = model.name;

    const sidebarMeta = qs('#sidebarMeta');
    if (sidebarMeta) {
      mount(sidebarMeta, ...model.sidebarMeta.map(([term, value]) => el('div', null, [
        el('dt', { text: term }),
        el('dd', { text: value }),
      ])));
    }

    renderSwitcher();
    document.title = model.documentTitle;
  }

  function renderSwitcher() {
    const host = qs('#portalSwitch');
    if (!host) return;
    mount(host, ...AVAILABLE.map((id) => {
      const model = MODELS.get(id);
      const active = id === portal;
      return el('button.seg-btn', {
        type: 'button',
        class: active ? 'active' : '',
        text: model.shortName,
        title: `切换到${model.name}`,
        'aria-pressed': active ? 'true' : 'false',
        onClick: () => switchPortal(id),
      });
    }));
  }

  function markActiveNav(routeName) {
    const href = hashUrl(portal, routeName);
    for (const item of document.querySelectorAll('.nav-item')) {
      item.classList.toggle('active', item.getAttribute('href') === href);
    }
  }

  function setPageMeta(meta) {
    const model = MODELS.get(portal);
    const titleNode = qs('#pageTitle');
    const subNode = qs('#pageSubtitle');
    if (titleNode) titleNode.textContent = meta?.title || model.name;
    if (subNode) subNode.textContent = meta?.subtitle || model.subtitle;
    document.title = `${meta?.title || model.name} · ${model.name}`;
  }

  // ==================================================================
  // 视图生命周期
  // ==================================================================

  function destroyCurrent() {
    const { instance } = current;
    if (instance && typeof instance.destroy === 'function') {
      try {
        instance.destroy();
      } catch (error) {
        console.warn('[portal] 视图 destroy 失败', error);
      }
    }
    const view = qs('#view');
    if (view) disposeCharts(view);
    current = { name: null, instance: null };
  }

  function renderInto(view, ...children) {
    mount(view, el('div.view-body', null, children));
  }

  /** 模块加载失败（页面尚未安装或语法错误）时的内联提示。 */
  function showModuleNotice(view, meta, error) {
    const missing = error?.name === 'TypeError';
    renderInto(view, el('div.empty.notice', null, [
      el('div.empty-title', { text: `${meta?.title || '该页面'}尚未安装` }),
      el('div.empty-hint', {
        text: missing
          ? '该页面尚未安装（前端模块缺失）。页面入口与路由已就位，安装对应模块后刷新即可使用。'
          : `该页面暂时无法加载：${error?.message || '未知错误'}`,
      }),
      el('div.empty-actions', null, [
        el('button.btn.btn-ghost.btn-sm', {
          type: 'button',
          text: '重试',
          onClick: () => retryActive(),
        }),
      ]),
    ]));
  }

  /** 当前门户后端不可达：给出可操作的中文提示，另一个门户照常可用。 */
  function showOfflineNotice(view, meta, error) {
    const model = MODELS.get(portal);
    const detail = error?.message ? `接口异常：${error.message}` : '接口无响应。';
    renderInto(view, el('div.empty.notice.notice-offline', null, [
      el('div.empty-title', { text: `${model.name}未启动或接口不可达` }),
      el('div.empty-hint', {
        text: `${detail} 请运行 start.cmd 或 python -m atmos serve --portal both，`
          + `并确认 ${model.name}（${PORTAL_LABEL[portal]}）的服务已在监听。`,
      }),
      el('div.empty-actions', null, [
        el('button.btn.btn-primary.btn-sm', {
          type: 'button',
          text: '重试',
          onClick: () => retryActive(),
        }),
        el('span.text-mute', {
          text: `当前页面：${meta?.title || model.name}。顶栏可切换到另一个门户，其功能不受影响。`,
          style: { fontSize: '12px' },
        }),
      ]),
    ]));
  }

  function showViewError(view, error, meta) {
    renderInto(view, el('div.empty', null, [
      el('div.empty-title', { text: `${meta?.title || '视图'}加载失败` }),
      el('div.empty-hint', { text: error?.message || '未知错误' }),
      el('div.empty-actions', null, [
        el('button.btn.btn-ghost.btn-sm', {
          type: 'button',
          text: '重试',
          onClick: () => retryActive(),
        }),
      ]),
    ]));
  }

  async function renderCurrent() {
    const hashPortal = portalFromHash();
    const portalChanged = hashPortal !== null && hashPortal !== portal;
    if (hashPortal !== null) portal = hashPortal;

    const route = parseHash(portal);
    lastRoute[portal] = route.name;
    storageSet(PORTAL_KEY, portal);
    storageSet(ROUTE_KEY(portal), route.name);
    setActivePortal(portal);

    renderShell();
    setPageMeta(MODELS.get(portal).routes.get(route.name));
    markActiveNav(route.name);

    // 门户切换：任务面板换成另一台服务的登记簿，健康状态重新探测
    if (portalChanged) {
      lastErrorText = '';
      LIVE.set(portal, null); // 目标门户必须重新探测，不能沿用会话早先的结果
      configureTaskSource();
    }

    // 地址栏里的门户段缺失或不匹配时补正，保证路由永远是可分享的规范形式
    const canonical = hashUrl(portal, route.name, route.segments, route.query);
    if (window.location.hash !== canonical) {
      window.location.hash = canonical; // 触发下一次 hashchange，由它完成渲染
      return;
    }

    const view = qs('#view');
    if (!view) return;

    destroyCurrent();
    view.textContent = '';

    const meta = MODELS.get(portal).routes.get(route.name);
    if (!meta?.module) {
      showModuleNotice(view, meta, new TypeError('模块未声明'));
      return;
    }

    // 后端可用性：先探测一次（整会话按门户缓存），不可用就直接给出提示，
    // 而不是让视图逐个接口去撞网络错误、最后抛出异常卡片
    const live = await ensureLive();
    if (window.location.hash !== canonical || portalFromHash() !== portal) return;
    if (!live) {
      showOfflineNotice(view, meta, activeLiveError);
      return;
    }

    let module;
    try {
      module = await import(meta.module);
    } catch (error) {
      console.warn(`[portal] 视图模块加载失败：${meta.module} — ${error?.message || error}`);
      showModuleNotice(view, meta, error);
      return;
    }

    if (typeof module.render !== 'function') {
      showModuleNotice(view, meta, new TypeError(`视图 ${route.name} 未导出 render 函数`));
      return;
    }

    // 异步 import 期间用户可能已经切走：此时放弃本次渲染，避免污染新视图
    if (window.location.hash !== canonical || portalFromHash() !== portal) return;

    current = { name: route.name, instance: module };
    view.textContent = '';
    let completed = false;

    try {
      await module.render(view, ctxFor(route), route.segments);
      completed = true;
    } catch (error) {
      console.warn('[portal] 视图渲染异常', error);
      if (!completed && isNetworkError(error)) {
        // 视图在渲染过程中撞上不可达的后端：降级为可重试的内联提示
        LIVE.set(portal, false);
        activeLive = false;
        activeLiveChecked = true;
        activeLiveError = error;
        updateHealthChip();
        showOfflineNotice(view, meta, error);
      } else {
        showViewError(view, error, meta);
      }
    }
  }

  function portalFromHashOr(fallback) {
    return portalFromHash() ?? fallback;
  }

  function ctxFor(route) {
    const model = MODELS.get(portal);
    return {
      portal,
      portalName: model.name,
      route: route.name,
      params: route.segments,
      query: route.query,
      navigate: (name, params = [], query = null) => navigate(name, params, query),
      // 视图内不要自己拼 `#/...`：本门户前缀由外壳负责
      url: (name, params = [], query = null) => hashUrl(portal, name, params, query),
      refresh: () => renderCurrent(),
      isActive: () => current.name === route.name,
      can: (kind) => model.capabilities[kind] === true,
      toast,
    };
  }

  // ==================================================================
  // 后端可用性
  // ==================================================================

  let activeLiveError = null;

  /** 网络层失败（后端未启动 / 不可达）而不是接口返回的业务错误。 */
  function isNetworkError(error) {
    if (!error) return false;
    if (error.aborted) return false;
    return error.status === undefined; // HTTP 错误带 status，网络错误没有
  }

  /** 已知结果直接复用；未知则探测一次。 */
  async function ensureLive() {
    const cached = LIVE.get(portal);
    if (cached !== null && cached !== undefined) {
      activeLive = cached;
      activeLiveChecked = true;
      return cached;
    }
    return probeActive();
  }

  /** 探测当前门户的后端；失败静默记录，由调用方决定如何展示。 */
  async function probeActive() {
    if (healthBusy) return LIVE.get(portal);
    healthBusy = true;
    const target = portal;
    try {
      const health = await api.health({ silent: true });
      LIVE.set(target, true);
      healthByPortal.set(target, health);
      if (target === portal) {
        activeLive = true;
        activeLiveChecked = true;
        activeLiveError = null;
        renderHealth(health);
      }
      return true;
    } catch (error) {
      LIVE.set(target, false);
      if (target === portal) {
        activeLive = false;
        activeLiveChecked = true;
        activeLiveError = error;
        renderHealth(healthByPortal.get(target) || null, error);
      }
      return false;
    } finally {
      healthBusy = false;
    }
  }

  /** 「重试」：清掉缓存与不可用标记，重新探测后再渲染视图。 */
  async function retryActive() {
    clearBanners();
    clearLastError();
    activeLive = null;
    activeLiveChecked = false;
    activeLiveError = null;
    LIVE.set(portal, null);
    updateHealthChip();
    await probeActive();
    renderCurrent();
  }

  // ==================================================================
  // 顶部状态
  // ==================================================================

  function updateHealthChip() {
    const status = getStatus();
    const dot = qs('#healthDot');
    const text = qs('#healthText');
    if (!dot || !text) return;

    if (activeLive === false) {
      dot.className = 'dot dot-bad';
      text.textContent = '服务不可用';
      return;
    }

    if (status.lastError) {
      dot.className = 'dot dot-bad';
      text.textContent = '请求异常';
      const message = status.lastError.message;
      if (message !== lastErrorText) {
        lastErrorText = message;
        if (message !== lastBannerError) {
          lastBannerError = message;
          banner({ kind: 'error', title: '接口调用异常', message });
        }
      }
      return;
    }

    lastErrorText = '';
    if (status.pending > 0) {
      dot.className = 'dot dot-warn';
      text.textContent = `请求中（${status.pending}）`;
    } else if (activeLive === true) {
      dot.className = 'dot dot-ok';
      text.textContent = '服务正常';
    } else {
      dot.className = 'dot dot-idle';
      text.textContent = '正在检测服务状态';
    }
  }

  function renderHealth(health, error = null) {
    const chip = qs('#healthChip');
    if (!health) {
      updateHealthChip();
      if (chip) chip.title = error ? `接口探测失败：${error.message}` : '接口未连通';
      return;
    }

    const model = MODELS.get(portal);
    const ok = health?.status === 'ok';
    const registry = health?.registry || {};
    const rows = [
      `门户：${model.name}`,
      `服务状态：${health?.status || '未知'}`,
      `版本：${health?.version || '—'}`,
      `环境：${health?.env || '—'}`,
    ];
    if (registry.assets !== undefined) rows.push(`资产：${registry.assets} 项`);
    if (registry.cities !== undefined) rows.push(`城市：${registry.cities} 个`);
    if (registry.quality_checks !== undefined) rows.push(`质量检查项：${registry.quality_checks} 条`);
    if (registry.lineage_edges !== undefined) rows.push(`血缘边：${registry.lineage_edges} 条`);
    if (health?.lake_exists !== undefined) rows.push(`数据湖：${health.lake_exists ? '已就绪' : '未创建'}`);
    if (chip) chip.title = rows.join('\n');

    const dot = qs('#healthDot');
    const text = qs('#healthText');
    if (dot) dot.className = `dot ${ok ? 'dot-ok' : 'dot-warn'}`;
    if (text) text.textContent = ok ? '服务正常' : '服务降级';

    const footRight = qs('#footRight');
    if (footRight) {
      const counts = [];
      if (registry.assets !== undefined) counts.push(`目录 ${registry.assets} 项资产`);
      if (registry.cities !== undefined) counts.push(`${registry.cities} 个城市`);
      footRight.textContent = `v${health?.version || '—'}${counts.length ? ` · ${counts.join(' / ')}` : ''} · ${portal}`;
    }
  }

  /** 周期探测：只更新顶栏状态；可用性变化时才重渲染视图。 */
  async function healthTick() {
    if (activeLive === false) {
      // 已知不可用时才需要周期性地重新探测（另一个门户照常工作）
      const before = activeLive;
      await probeActive();
      if (activeLive !== before && current.name) renderCurrent();
      return;
    }
    const before = activeLive;
    const beforeChecked = activeLiveChecked;
    await probeActive();
    if (activeLiveChecked && (activeLive !== before || !beforeChecked) && current.name) {
      renderCurrent();
    }
  }

  // ==================================================================
  // 后台任务面板
  // ==================================================================

  const seenFinished = new Set();
  let firstTaskSnapshot = true;

  function renderTaskDock({ records }) {
    const dock = qs('#taskDock');
    const body = qs('#taskDockBody');
    const count = qs('#taskDockCount');
    if (!dock || !body) return;

    const active = records.filter((item) => !item.finished_at);
    dock.hidden = records.length === 0;
    if (count) count.textContent = String(active.length);

    mount(
      body,
      ...records.slice(0, 8).map((record) => {
        const running = !record.finished_at;
        return el('div.task-dock-item', { title: record.task_id }, [
          el('div.task-line', null, [
            el('span.task-name', { text: taskKindLabel(record.kind) }),
            running ? el('span.text-warn', { text: '进行中' }) : runStatusBadge(record.status),
          ]),
          el('div.text-mute', { text: taskParamsText(record.params) || record.task_id, style: { fontSize: '10.5px' } }),
          el('div', { style: { marginTop: '4px' } }, [progressBar(running ? null : 100)]),
        ]);
      }),
    );
  }

  function handleTaskUpdates(snapshot) {
    renderTaskDock(snapshot);

    const finished = snapshot.records.filter((item) => item.finished_at);
    if (firstTaskSnapshot) {
      // 首次快照里的历史任务不触发自动刷新，避免页面刚打开就重复请求
      finished.forEach((item) => seenFinished.add(item.task_id));
      firstTaskSnapshot = false;
      return;
    }

    let shouldRefresh = false;
    for (const record of finished) {
      if (seenFinished.has(record.task_id)) continue;
      seenFinished.add(record.task_id);
      const failed = record.status === 'failed';
      toast(
        `${taskKindLabel(record.kind)}${failed ? '失败' : '已完成'}${record.error ? `：${record.error}` : ''}`,
        failed ? 'bad' : 'ok',
        5000,
      );
      shouldRefresh = true;
    }

    if (shouldRefresh && current.name) {
      const now = Date.now();
      if (now - autoRefreshAt > 4000) {
        autoRefreshAt = now;
        renderCurrent();
      }
    }
  }

  /** 门户切换时换任务登记簿：分析轮询 /api/ops/tasks，治理轮询 /api/quality/tasks。 */
  function configureTaskSource() {
    const tasks = MODELS.get(portal).tasks;
    firstTaskSnapshot = true;
    seenFinished.clear();
    mount(qs('#taskDockBody') || document.createElement('div'));
    const dock = qs('#taskDock');
    if (dock) dock.hidden = true;
    const count = qs('#taskDockCount');
    if (count) count.textContent = '0';

    if (tasks && typeof tasks.fetch === 'function') {
      configureTasks(tasks);
      requestTaskRefresh();
    } else {
      configureTasks({ fetch: null });
    }
  }

  // ==================================================================
  // 事件与启动
  // ==================================================================

  function bindShellEvents() {
    const refreshBtn = qs('#refreshBtn');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', () => {
        clearBanners();
        clearLastError();
        if (activeLive === false) {
          void retryActive();
          return;
        }
        renderCurrent();
        void probeActive();
      });
    }
    window.addEventListener('hashchange', () => {
      void renderCurrent();
    });
  }

  function renderFatal(message) {
    const view = qs('#view');
    if (!view) return;
    view.textContent = '';
    view.appendChild(el('div.empty', null, [
      el('div.empty-title', { text: '页面初始化失败' }),
      el('div.empty-hint', { text: message }),
    ]));
  }

  async function boot() {
    if (booted) return;
    booted = true;

    renderShell();
    bindShellEvents();
    onStatus(updateHealthChip);

    configureTaskSource();
    onTasks(handleTaskUpdates);

    const route = parseHash(portal);
    const canonical = hashUrl(portal, route.name, route.segments, route.query);
    if (window.location.hash !== canonical) window.location.hash = canonical;

    await probeActive();
    await renderCurrent();
    window.setInterval(() => {
      void healthTick();
    }, HEALTH_INTERVAL_MS);

    console.info(
      `%c${APP_NAME} · 统一门户已启动（当前：${MODELS.get(portal).name}）`,
      'color:#3b82f6;font-weight:600',
      `\n门户：${AVAILABLE.map((id) => `${id}（${MODELS.get(id).name}）`).join(' / ')}`
        + `\n路由：${AVAILABLE.map((id) => `#/${id}/<route>`).join(' / ')}`
        + `\nECharts：${window.echarts ? '已加载 5.5.1' : '未加载（图表将显示内联提示）'}`,
    );
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      boot().catch((error) => {
        console.error('[portal] 启动失败', error);
        renderFatal(error?.message || '未知错误');
      });
    }, { once: true });
  } else {
    boot().catch((error) => {
      console.error('[portal] 启动失败', error);
      renderFatal(error?.message || '未知错误');
    });
  }

  return {
    navigate,
    renderCurrent,
    switchPortal,
    get portal() {
      return portal;
    },
    portals: AVAILABLE,
  };
}
