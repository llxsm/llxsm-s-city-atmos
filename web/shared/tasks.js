/**
 * tasks.js —— 后台长任务监视器（两个门户共享）。
 *
 * 采集（`POST /api/ops/collect`，分析门户）与质量评测
 * （`POST /api/quality/evaluate`，治理门户）默认立即返回
 * `{status:"accepted", task_id}`，真实进度需要轮询。
 *
 * 两个门户的任务登记簿挂在不同的端点上，因此本模块由 `portal.js` 通过
 * `configureTasks({ fetch, label, source })` 注入数据源 —— 门户传进来的
 * `fetch` 就是该门户自己的 API 方法（分析门户传 `api.opsTasks`，
 * 治理门户传 `api.qualityTasks`），本模块不硬编码任何端点路径。
 *
 * 未配置数据源时，本模块是**惰性**的：不发任何请求，快照为空。
 * 治理门户的 `/api/ops/summary` 不返回进程内任务，因此那边不会启动轮询，
 * 也就不会出现"接口 404 把页面顶栏刷红"的情况。
 */

const POLL_INTERVAL_MS = 4000;
const IDLE_INTERVAL_MS = 15000;
const LISTENERS = new Set();

let source = null; // { label, source, fetch(limit) } —— 由门户注入
let records = [];
let timer = null;
let inflight = false;
let lastError = null;
let started = false;
/** 连续失败次数：端点不可用时停止空转，避免无谓的重试风暴。 */
let failures = 0;

function notify() {
  const snapshot = { records: [...records], error: lastError, source: source?.source || null };
  for (const listener of LISTENERS) {
    try {
      listener(snapshot);
    } catch (error) {
      // 单个订阅者出错不应影响其他订阅者与轮询本身
      console.warn('[tasks] 订阅回调异常', error);
    }
  }
}

function activeRecords() {
  return records.filter((item) => !item.finished_at);
}

function schedule(delay = POLL_INTERVAL_MS) {
  if (timer) clearTimeout(timer);
  timer = setTimeout(poll, delay);
}

function stopTimer() {
  if (timer) clearTimeout(timer);
  timer = null;
}

/**
 * 注册任务登记簿数据源，并启动轮询（幂等）。
 *
 * @param {{fetch: (limit:number) => Promise<object>, label?: string, source?: string}} config
 *        `fetch` 由门户提供（分析门户传 api.opsTasks，治理门户传 api.qualityTasks），
 *        因此本模块不需要知道任何一个门户的端点路径。
 */
export function configureTasks(config) {
  const fetcher = typeof config?.fetch === 'function' ? config.fetch : null;
  if (!fetcher) {
    source = null;
    stopTimer();
    started = false;
    records = [];
    lastError = null;
    notify();
    return;
  }
  source = {
    label: config.label || '后台任务',
    source: config.source || 'ops',
    fetch: fetcher,
  };
  startTaskMonitor();
}

/** 当前是否已接入任务登记簿。 */
export function hasTaskSource() {
  return source !== null;
}

function fetchTasks(limit) {
  if (!source) return Promise.resolve({ items: [] });
  return source.fetch(limit);
}

async function poll() {
  if (!source || inflight) return;
  inflight = true;
  try {
    const payload = await fetchTasks(20);
    records = Array.isArray(payload?.items) ? payload.items : [];
    lastError = null;
    failures = 0;
  } catch (error) {
    lastError = error?.message || '任务状态查询失败';
    failures += 1;
  } finally {
    inflight = false;
  }
  notify();
  if (!source) return;
  if (activeRecords().length > 0) schedule(POLL_INTERVAL_MS);
  else if (failures >= 3) stopTimer(); // 端点不可用：停止空转，等待 requestTaskRefresh()
  else schedule(IDLE_INTERVAL_MS); // 低频守候，等待新的任务被受理
}

/** 启动后台任务监视（幂等）。未配置数据源时不发请求。 */
export function startTaskMonitor() {
  if (!source || started) return;
  started = true;
  poll();
}

/** 停止轮询（门户切换 / 视图清理时使用）。 */
export function stopTaskMonitor() {
  stopTimer();
  started = false;
}

/** 立即刷新一次任务列表（例如刚提交任务后）。 */
export function requestTaskRefresh() {
  if (!source) return;
  failures = 0;
  started = true;
  stopTimer();
  schedule(400);
}

/**
 * 订阅任务列表变化。
 * @param {(snapshot:{records:Array, error:string|null, source:string|null}) => void} listener
 * @returns {Function} 取消订阅
 */
export function onTasks(listener) {
  LISTENERS.add(listener);
  listener({ records: [...records], error: lastError, source: source?.source || null });
  return () => LISTENERS.delete(listener);
}

export function getTasks() {
  return [...records];
}

/**
 * 等待指定任务结束；同时把进度回调透出给视图。
 *
 * 任务不在当前登记簿中（例如治理门户尚未接入任务接口）时立即返回 null，
 * 而不是把视图挂在"永远等不到"的状态里。
 *
 * @param {string} taskId 任务 ID；为空时退化为"等待全部活动任务结束"
 * @param {{onProgress?:Function, timeoutMs?:number}} [options]
 * @returns {Promise<object|null>} 结束时的任务记录
 */
export function waitForTask(taskId, options = {}) {
  const { onProgress = null, timeoutMs = 30 * 60 * 1000 } = options;
  const startedAt = Date.now();

  if (!source) {
    if (onProgress) onProgress(null);
    return Promise.resolve(null);
  }

  return new Promise((resolve) => {
    let unsubscribe = () => {};
    let settled = false;
    const finish = (record) => {
      if (settled) return;
      settled = true;
      clearTimeout(guard);
      unsubscribe();
      resolve(record || null);
    };

    // 兜底：即使一次快照都没到达也不能把视图卡死
    const guard = setTimeout(() => finish(null), timeoutMs);

    unsubscribe = onTasks(({ records: list }) => {
      const target = taskId
        ? list.find((item) => item.task_id === taskId)
        : list.find((item) => !item.finished_at) || list[0];
      if (onProgress) onProgress(target || null);
      if (target && target.finished_at) {
        finish(target);
        return;
      }
      if (!target && taskId === null && list.length === 0) {
        // 无 ID 且列表已无该任务：可能已被登记簿裁剪，直接结束等待
        finish(null);
        return;
      }
      if (Date.now() - startedAt > timeoutMs) finish(target || null);
    });
  });
}

/** 任务类型中文名。 */
export function taskKindLabel(kind) {
  return { collect: '采集管道', quality: '质量评测' }[kind] || kind || '后台任务';
}

/** 任务参数摘要（中文）。 */
export function taskParamsText(params) {
  if (!params || typeof params !== 'object') return '';
  const parts = [];
  if (params.scope) {
    const scopeLabel = { all: '全量', collect: '采集', derive: '派生', reference: '参考层' }[params.scope] || params.scope;
    parts.push(`范围：${scopeLabel}`);
  }
  if (Array.isArray(params.cities) && params.cities.length) parts.push(`城市：${params.cities.length} 个`);
  if (Array.isArray(params.assets) && params.assets.length) parts.push(`资产：${params.assets.length} 个`);
  if (params.window_hours) parts.push(`窗口：${params.window_hours} 小时`);
  if (params.include_archive === false) parts.push('跳过归档回补');
  if (params.quality_after) parts.push('采后评测');
  return parts.join(' · ');
}
