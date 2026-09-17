/**
 * api.js —— 平台 REST 接口客户端（两个门户共用一份）。
 *
 * 设计要点：
 * 1. 单一 `request()` 入口，统一 JSON 解析与错误处理（读取 FastAPI 的 `detail` 字段）；
 * 2. 只读 GET 带内存级 TTL 缓存，避免同一视图内多个组件重复拉取同一接口；
 *    任何写操作（POST）自动失效相关缓存；
 * 3. 暴露简易的加载/错误状态订阅，供 portal.js 在顶部展示统一提示；
 * 4. **按门户选择基地址**：两个后端服务各挂载一份相同的前端，因此同一个方法
 *    需要知道自己该发往哪台服务（见下）。
 *
 * 时区约定：本文件不做任何时间转换；时间语义由 ui.js 的格式化函数负责。
 */

// ==================================================================
// 门户与基地址
// ==================================================================
//
// 服务端在每个页面注入 /portal-config.js：
//
//   window.__ATMOS_PORTALS__ = {
//     current: 'analysis',                              // 本次由哪台服务托管页面
//     endpoints: {
//       analysis: 'http://127.0.0.1:8000',              // 浏览器可达地址，无尾斜杠
//       governance: 'http://127.0.0.1:8001',
//     },
//     environment: 'dev',
//   }
//
// 约定：`endpoints[portal]` 为空字符串时表示"同源 /api"，因此缺少
// portal-config.js（脚本被拦截、离线打开页面等）时前端仍能退回单服务模式。
//
// 方法归属：
//   * 治理专属方法显式标注 `portal: 'governance'`；
//   * 分析方法不标注（默认 `'analysis'`）；
//   * 两个门户都提供但语义不同的路径（/health、/meta、/overview）标注
//     `portal: 'current'`，即打往当前正在浏览的那个门户 —— 否则在治理页面
//     上会读到分析平台的健康状态与总览口径。
//
// 缓存键包含解析后的基地址（`方法:基地址|路径`），因此切换门户绝不会
// 命中另一个门户的缓存；写操作后的按路径失效仍只匹配 `|` 之后的路径部分。

const PORTALS = ['analysis', 'governance'];
const DEFAULT_PORTAL = 'analysis';
/** 当前门户：由 portal.js 在切换页签时同步。 */
const ACTIVE = { portal: DEFAULT_PORTAL };

function readConfig() {
  const injected = typeof window !== 'undefined' ? window.__ATMOS_PORTALS__ : null;
  if (!injected || typeof injected !== 'object') return null;
  return injected;
}

/** 本次页面由哪台服务托管（与"当前浏览哪个门户"是两件事）。 */
export function servedPortal() {
  const current = readConfig()?.current;
  return PORTALS.includes(current) ? current : null;
}

/** 两个后端的浏览器可达基地址；缺省为空字符串（同源）。 */
export function portalEndpoints() {
  const endpoints = readConfig()?.endpoints;
  const result = {};
  for (const portal of PORTALS) {
    const base = endpoints?.[portal];
    result[portal] = typeof base === 'string' ? stripTrailingSlash(base) : '';
  }
  return result;
}

function stripTrailingSlash(base) {
  return base.endsWith('/') ? base.slice(0, -1) : base;
}

/** 当前生效的门户（页签）。 */
export function getActivePortal() {
  return ACTIVE.portal;
}

/**
 * 设置当前门户。由 portal.js 在启动与切换页签时调用；
 * 单独使用时也安全（页签切换之外没有别的地方会改它）。
 */
export function setActivePortal(portal) {
  if (PORTALS.includes(portal)) ACTIVE.portal = portal;
  return ACTIVE.portal;
}

/**
 * 解析一次请求实际使用的基地址。
 * @param {'analysis'|'governance'|'current'|undefined} portal
 */
export function resolveBase(portal = DEFAULT_PORTAL) {
  const key = !portal || portal === 'current' ? ACTIVE.portal : portal;
  return portalEndpoints()[key] ?? '';
}

/** 拼出完整请求 URL（基地址 + `/api` + 路径 + 查询串）。 */
export function portalUrl(portal, path, params = null) {
  return `${resolveBase(portal)}/api${path}${buildQuery(params)}`;
}

/** 默认缓存时长：环境类接口变化快，目录/元数据慢。 */
const TTL = {
  short: 15 * 1000,
  medium: 45 * 1000,
  long: 5 * 60 * 1000,
};

// ==================================================================
// 缓存
// ==================================================================

const cache = new Map(); // key -> { data, expires }

function cacheGet(key) {
  const hit = cache.get(key);
  if (!hit) return undefined;
  if (hit.expires < Date.now()) {
    cache.delete(key);
    return undefined;
  }
  return hit.data;
}

function cacheSet(key, data, ttl) {
  if (!ttl) return data;
  cache.set(key, { data, expires: Date.now() + ttl });
  return data;
}

/** 缓存键中分隔「基地址」与「API 路径」的记号。 */
const KEY_SEP = '|';

/** 键形如 `GET:http://127.0.0.1:8001|/api/assets`。 */
function cacheKey(method, url) {
  const index = url.indexOf('/api');
  if (index <= 0) return `${method}:${url}`;
  return `${method}:${url.slice(0, index)}${KEY_SEP}${url.slice(index)}`;
}

/** 取出缓存键里的 API 路径部分（`|` 之后）。 */
function keyPath(key) {
  const index = key.indexOf(KEY_SEP);
  return index < 0 ? key : key.slice(index + 1);
}

/** 按 API 路径前缀失效缓存；不传前缀则全量清空。 */
export function invalidateCache(prefix = '') {
  if (!prefix) {
    cache.clear();
    return;
  }
  for (const key of [...cache.keys()]) {
    if (keyPath(key).startsWith(prefix)) cache.delete(key);
  }
}

// ==================================================================
// 状态订阅（加载中 / 最近一次错误）
// ==================================================================

const status = { pending: 0, lastError: null };
const statusListeners = new Set();

function notifyStatus() {
  for (const listener of statusListeners) listener({ ...status });
}

/** 订阅请求状态；返回取消订阅函数。 */
export function onStatus(listener) {
  statusListeners.add(listener);
  listener({ ...status });
  return () => statusListeners.delete(listener);
}

export function getStatus() {
  return { ...status };
}

export function clearLastError() {
  status.lastError = null;
  notifyStatus();
}

// ==================================================================
// 核心请求
// ==================================================================

function buildQuery(params) {
  if (!params) return '';
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === null || value === undefined || value === '') continue;
    search.append(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

/**
 * 发起一次 API 请求。
 *
 * @param {string} path 以 `/` 开头的 API 路径（不含 `/api` 前缀）
 * @param {object} [options]
 * @param {'analysis'|'governance'|'current'} [options.portal='analysis']
 *        目标门户；`'current'` 表示"当前正在浏览的门户"
 * @param {string} [options.method='GET']
 * @param {object} [options.params] 查询参数（空值自动剔除）
 * @param {object|Array} [options.body] JSON 请求体
 * @param {number} [options.ttl=0] GET 缓存时长（毫秒）；0 表示不缓存
 * @param {AbortSignal} [options.signal]
 * @param {boolean} [options.silent=false] 为 true 时不更新全局错误提示
 * @returns {Promise<any>}
 */
export async function request(path, options = {}) {
  const {
    portal = DEFAULT_PORTAL,
    method = 'GET',
    params = null,
    body = null,
    ttl = 0,
    signal = null,
    silent = false,
  } = options;
  const url = portalUrl(portal, path, params);
  const key = cacheKey(method, url);

  if (method === 'GET' && ttl > 0) {
    const hit = cacheGet(key);
    if (hit !== undefined) return hit;
  }

  status.pending += 1;
  notifyStatus();

  try {
    const response = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: signal || undefined,
      credentials: 'same-origin',
    });

    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = { detail: text.slice(0, 400) };
      }
    }

    if (!response.ok) {
      throw createError(response.status, payload, url);
    }

    if (method === 'GET' && ttl > 0) cacheSet(key, payload, ttl);
    if (method !== 'GET') invalidateAfterWrite(path);
    return payload;
  } catch (error) {
    const normalized = normalizeError(error, url);
    if (!silent && !isAbort(normalized)) {
      status.lastError = normalized;
      notifyStatus();
    }
    throw normalized;
  } finally {
    status.pending = Math.max(0, status.pending - 1);
    notifyStatus();
  }
}

function createError(httpStatus, payload, url) {
  const detail = payload?.detail;
  let message = '';
  if (typeof detail === 'string') message = detail;
  else if (Array.isArray(detail)) {
    // FastAPI 校验错误结构：[{loc, msg, type}]
    message = detail
      .map((item) => {
        const where = Array.isArray(item?.loc) ? item.loc.slice(1).join('.') : '';
        return where ? `${where}: ${item?.msg ?? ''}` : String(item?.msg ?? '');
      })
      .join('；');
  } else if (detail && typeof detail === 'object') {
    message = JSON.stringify(detail);
  }
  if (!message) message = payload?.message || `请求失败（HTTP ${httpStatus}）`;
  const error = new Error(message);
  error.status = httpStatus;
  error.url = url;
  return error;
}

function normalizeError(error, url) {
  if (error instanceof Error && error.status) return error;
  if (isAbort(error)) {
    const abortError = new Error('请求已取消');
    abortError.aborted = true;
    return abortError;
  }
  const wrapped = new Error(error?.message || '网络请求失败，请确认后端服务是否已启动');
  wrapped.cause = error;
  wrapped.url = url;
  return wrapped;
}

function isAbort(error) {
  return error?.name === 'AbortError' || error?.aborted === true;
}

/** 写操作后按资源前缀失效缓存，保证刷新后立即看到新数据（两个门户都覆盖）。 */
function invalidateAfterWrite(path) {
  const rules = [
    ['/ops', ['/overview', '/runtime', '/ops', '/env', '/insights', '/quality', '/assets']],
    ['/quality', ['/quality', '/overview', '/assets', '/insights']],
    ['/cities', ['/cities', '/overview', '/env']],
    ['/env', ['/env', '/insights', '/overview']],
  ];
  for (const [prefix, targets] of rules) {
    if (path.startsWith(prefix)) {
      for (const target of targets) invalidateCache(target);
      return;
    }
  }
  cache.clear();
}

// ==================================================================
// 接口封装
// ==================================================================

const j = (value) => (Array.isArray(value) ? value.filter(Boolean).join(',') : value);
/** 两个门户都提供、但语义不同的路径：跟随当前浏览的门户。 */
const CURRENT = 'current';

export const api = {
  // ---------- 元信息与总览（两个门户都有，语义不同 → 跟随当前门户） ----------
  health: ({ silent = false } = {}) => request(`/health`, { portal: CURRENT, ttl: TTL.short, silent }),
  meta: () => request(`/meta`, { portal: CURRENT, ttl: TTL.long }),
  overview: () => request(`/overview`, { portal: CURRENT, ttl: TTL.short }),

  // ---------- 数据可用性（仅分析门户） ----------
  availability: () => request(`/availability`, { ttl: TTL.medium }),

  // ---------- 资产目录（仅治理门户） ----------
  assets: (params = {}) =>
    request(`/assets`, {
      portal: 'governance',
      params: {
        domain: params.domain || null,
        layer: params.layer || null,
        status: params.status || null,
        owner: params.owner || null,
        search: params.search || null,
      },
      ttl: TTL.medium,
    }),
  assetDetail: (key) => request(`/assets/${encodeURIComponent(key)}`, { portal: 'governance', ttl: TTL.short }),
  assetSample: (key, { city = null, limit = 20 } = {}) =>
    request(`/assets/${encodeURIComponent(key)}/sample`, {
      portal: 'governance',
      params: { city, limit },
      ttl: TTL.short,
    }),
  assetProfiling: (key, { city = null } = {}) =>
    request(`/assets/${encodeURIComponent(key)}/profiling`, {
      portal: 'governance',
      params: { city },
      ttl: TTL.short,
    }),

  // ---------- 城市主数据（仅治理门户） ----------
  cities: (activeOnly = false) =>
    request(`/cities`, { portal: 'governance', params: { active_only: activeOnly || null }, ttl: TTL.long }),

  // ---------- 血缘（仅治理门户） ----------
  lineageGraph: () => request(`/lineage/graph`, { portal: 'governance', ttl: TTL.medium }),
  lineageOrphans: () => request(`/lineage/orphans`, { portal: 'governance', ttl: TTL.medium }),
  lineageImpact: (key, { direction = 'both', maxDepth = 10 } = {}) =>
    request(`/lineage/${encodeURIComponent(key)}/impact`, {
      portal: 'governance',
      params: { direction, max_depth: maxDepth },
      ttl: TTL.short,
    }),

  // ---------- 数据质量（仅治理门户） ----------
  qualitySummary: () => request(`/quality/summary`, { portal: 'governance', ttl: TTL.short }),
  qualityScores: ({ asset = null, limit = 50 } = {}) =>
    request(`/quality/scores`, { portal: 'governance', params: { asset, limit }, ttl: TTL.short }),
  qualityHistory: (key, limit = 60) =>
    request(`/quality/scores/${encodeURIComponent(key)}/history`, {
      portal: 'governance',
      params: { limit },
      ttl: TTL.short,
      silent: true,
    }),
  qualityResults: ({ asset = null, status = null, dimension = null, limit = 100 } = {}) =>
    request(`/quality/results`, { portal: 'governance', params: { asset, status, dimension, limit }, ttl: TTL.short }),
  qualityChecks: ({ asset = null } = {}) =>
    request(`/quality/checks`, { portal: 'governance', params: { asset }, ttl: TTL.medium }),
  qualityReport: (key, windowHours = null) =>
    request(`/quality/evaluate/${encodeURIComponent(key)}`, {
      portal: 'governance',
      params: { window_hours: windowHours },
      ttl: 0,
    }),
  evaluateQuality: ({ assets = null, windowHours = null, wait = false } = {}) =>
    request(`/quality/evaluate`, {
      portal: 'governance',
      method: 'POST',
      body: { assets: assets && assets.length ? assets : null, window_hours: windowHours, wait },
    }),
  qualityTasks: (limit = 20) => request(`/quality/tasks`, { portal: 'governance', params: { limit }, ttl: 0 }),

  // ---------- 环境数据（仅分析门户） ----------
  envCities: () => request(`/env/cities`, { ttl: TTL.long }),
  envMetrics: () => request(`/env/metrics`, { ttl: TTL.long }),
  envCurrent: (cities = null) => request(`/env/current`, { params: { cities: j(cities) }, ttl: TTL.short }),
  envHourly: ({ city, hours = 72, metrics = null }) =>
    request(`/env/hourly`, { params: { city, hours, metrics: j(metrics) }, ttl: TTL.short }),
  envDaily: ({ city, days = 30 }) => request(`/env/daily`, { params: { city, days }, ttl: TTL.medium }),
  envCompare: ({ cities, metric = 'european_aqi', hours = 168, aggregate = 'hourly' }) =>
    request(`/env/compare`, { params: { cities: j(cities), metric, hours, aggregate }, ttl: TTL.short }),
  envRanking: (limit = 20) => request(`/env/ranking`, { params: { limit }, ttl: TTL.short }),
  envAlerts: (cities = null) => request(`/env/alerts`, { params: { cities: j(cities) }, ttl: TTL.short }),
  envTrend: ({ city, days = 90, metric = 'temperature_2m', window = 'day' }) =>
    request(`/env/trend`, { params: { city, days, metric, window }, ttl: TTL.medium }),

  // ---------- 深度分析（仅分析门户） ----------
  // 所有 insights 接口在数据不足时返回 {available:false, message:'…'} 而不是报错，
  // 视图据此渲染中文引导，绝不画空图表。
  insightsCatalog: () => request(`/insights/catalog`, { ttl: TTL.long }),
  insightsDigest: ({ cities = null, days = 30 } = {}) =>
    request(`/insights/digest`, { params: { cities: j(cities), days }, ttl: TTL.short }),
  insightsDistribution: ({ cities = null, metrics = null, days = 30, bins = 20 } = {}) =>
    request(`/insights/distribution`, { params: { cities: j(cities), metrics: j(metrics), days, bins }, ttl: TTL.short }),
  insightsCorrelation: ({ cities = null, metrics = null, days = 30 } = {}) =>
    request(`/insights/correlation`, { params: { cities: j(cities), metrics: j(metrics), days }, ttl: TTL.short }),
  insightsHourlyProfile: ({ cities = null, metric = 'european_aqi', days = 30 } = {}) =>
    request(`/insights/hourly-profile`, { params: { cities: j(cities), metric, days }, ttl: TTL.short }),
  insightsWeeklyProfile: ({ cities = null, metrics = null, days = 90 } = {}) =>
    request(`/insights/weekly-profile`, { params: { cities: j(cities), metrics: j(metrics), days }, ttl: TTL.short }),
  insightsMonthlyTrend: ({ cities = null, metric = 'european_aqi', days = 92 } = {}) =>
    request(`/insights/monthly-trend`, { params: { cities: j(cities), metric, days }, ttl: TTL.short }),
  insightsOutliers: ({ cities = null, metric = 'european_aqi', days = 30, method = 'zscore', threshold = 3 } = {}) =>
    request(`/insights/outliers`, { params: { cities: j(cities), metric, days, method, threshold }, ttl: TTL.short }),
  insightsEpisodes: ({ cities = null, days = 92, threshold = 40, minGapHours = 3, minDurationHours = 2, limit = 30 } = {}) =>
    request(`/insights/episodes`, {
      params: {
        cities: j(cities),
        days,
        threshold,
        min_gap_hours: minGapHours,
        min_duration_hours: minDurationHours,
        limit,
      },
      ttl: TTL.short,
    }),
  insightsRelationshipPairs: () => request(`/insights/relationship/pairs`, { ttl: TTL.long }),
  insightsRelationship: ({ x = 'wind_speed_10m', y = 'pm2_5', cities = null, days = 30, bins = 8, byCity = false } = {}) =>
    request(`/insights/relationship`, {
      params: { x, y, cities: j(cities), days, bins, by_city: byCity || null },
      ttl: TTL.short,
    }),
  insightsCompliance: ({ cities = null, days = 90, goodCeiling = 40, targetRatio = 80 } = {}) =>
    request(`/insights/compliance`, {
      params: { cities: j(cities), days, good_ceiling: goodCeiling, target_ratio: targetRatio },
      ttl: TTL.short,
    }),
  insightsComparison: ({ cities = null, days = 30, metric = 'aqi_avg' } = {}) =>
    request(`/insights/comparison`, { params: { cities: j(cities), days, metric }, ttl: TTL.short }),
  insightsClustering: ({ cities = null, days = 30, features = null, k = null } = {}) =>
    request(`/insights/clustering`, { params: { cities: j(cities), days, features: j(features), k }, ttl: TTL.short }),

  // ---------- 运维 ----------
  // 作业清单两个门户都有（同一份采集配置），因此跟随当前门户。
  opsJobs: () => request(`/ops/jobs`, { portal: CURRENT, ttl: TTL.long }),
  opsCollect: ({ scope = 'all', cities = null, includeArchive = true, qualityAfter = true, wait = false } = {}) =>
    request(`/ops/collect`, {
      method: 'POST',
      body: {
        scope,
        cities: cities && cities.length ? cities : null,
        include_archive: includeArchive,
        quality_after: qualityAfter,
        wait,
      },
    }),
  opsBootstrap: () => request(`/ops/bootstrap`, { portal: 'governance', method: 'POST' }),
  opsRefresh: () => request(`/ops/refresh`, { portal: 'governance', method: 'POST' }),
  opsRuns: ({ limit = 100, asset = null } = {}) =>
    request(`/ops/runs`, { portal: 'governance', params: { limit, asset }, ttl: TTL.short }),
  opsSummary: (hours = 24) => request(`/ops/summary`, { portal: 'governance', params: { hours }, ttl: TTL.short }),
  opsTasks: (limit = 20) => request(`/ops/tasks`, { params: { limit }, ttl: 0, silent: true }),
  opsSpecs: () => request(`/ops/specs`, { portal: 'governance', ttl: TTL.long }),
};

/**
 * 资产数据导出下载链接（仅治理门户提供 CSV / JSON 两个下载端点）。
 * 下载由浏览器直接发起，因此这里必须给出**治理服务**上的绝对地址。
 */
export function exportUrl(assetKey, { city = null, format = 'csv', limit = null } = {}) {
  // 注意：portalUrl() 自己会拼上 `/api`，这里的 path 不含该前缀
  return portalUrl('governance', `/export/${assetKey}.${format}`, { city, limit });
}

/**
 * 分析结果导出链接（仅分析门户提供达标考核 / 污染过程 / 分布三类导出）。
 * @param {'compliance'|'episodes'|'distribution'} analysis
 */
export function analysisExportUrl(analysis, { cities = null, days = null } = {}) {
  return portalUrl('analysis', `/insights/${analysis}/export.csv`, { cities: j(cities), days });
}

export { TTL };
