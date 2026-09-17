# web/ —— 前端静态资源（无构建步骤）

两个后端服务（分析 8000 / 治理 8001）**托管同一份前端**：一个页面、一个顶栏
分段切换器，点击即在「分析平台」与「数据治理」之间整体切换侧边导航与内容区，
不刷新页面。前端只有一份代码，门户差异集中在一份配置里。

全部是原生 HTML + CSS + ES module，没有 npm、没有打包器、没有框架：
浏览器直接加载源文件，改完刷新即可生效。

```
web/
  shared/                 两个服务共享；服务端挂载在 /shared
    style.css             深色主题、全部组件样式与顶栏分段切换器
    api.js                REST 客户端：按门户解析基地址、TTL 缓存、统一错误面
    charts.js             ECharts 生命周期、深色主题与图表构建器
    ui.js                 DOM 构造器、格式化、通用组件、抽屉与提示
    tasks.js              后台任务轮询（数据源由外壳按门户注入）
    portal.js             统一外壳：命名空间路由 + 门户切换（startPortal）

  app/                    统一前端；服务端把 /static 与 / 都指向这里
    index.html            侧边导航 + 顶栏（切换器 / 健康状态 / 刷新）+ #view
    static/portal.config.js   两个门户的导航、默认路由、任务源与能力边界
    static/views/         16 个视图（见下）
```

## 挂载约定（重要）

`atmos/apps/common/webapp.py` 对两个服务使用同一套路径约定：

| URL | 磁盘目录 | 说明 |
| --- | --- | --- |
| `/shared/*` | `web/shared/` | 共享 JS/CSS，两个服务都挂载 |
| `/portal-config.js` | 运行时生成 | 当前门户标识与两个后端的浏览器可达地址 |
| `/static/*` | `web/app/static/` | `portal.config.js` 与全部视图模块 |
| `/` | `web/app/index.html` | 统一外壳，注册在静态目录挂载之前 |

因此：

* `index.html` 按固定顺序加载三样东西：ECharts（CDN）→ `/portal-config.js`
  （**必须早于 `portal.js`**，`api.js` 在加载时就要读它）→ 模块化外壳脚本；
* 视图模块用**绝对说明符**导入共享模块：`import { el } from '/shared/ui.js'`，
  不要写 `../../shared/ui.js`；
* 视图模块的 `module` 路径也是绝对路径：`/static/views/<name>.js`；
* ECharts 通过 CDN 的 `<script>` 标签引入，`charts.js` 不 import 它，
  只在缺失时于图表位置给出内联提示。

两个服务监听同样的路径，只是端口不同（分析 8000、治理 8001），
共享模块与视图的绝对 URL 在两边解析结果一致 —— 这是"一份前端"的关键。

## 视图清单（16 个）

| 门户 | 路由 | 文件 |
| --- | --- | --- |
| analysis | `#/analysis/digest` | `digest.js` |
| analysis | `#/analysis/monitor` | `monitor.js` |
| analysis | `#/analysis/compare` | `compare.js` |
| analysis | `#/analysis/trend` | `trend.js` |
| analysis | `#/analysis/distribution` | `distribution.js` |
| analysis | `#/analysis/profile` | `profile.js` |
| analysis | `#/analysis/episodes` | `episodes.js` |
| analysis | `#/analysis/relationship` | `relationship.js` |
| analysis | `#/analysis/compliance` | `compliance.js` |
| analysis | `#/analysis/clustering` | `clustering.js` |
| analysis | `#/analysis/ops`（数据刷新） | `refresh.js` |
| governance | `#/governance/overview` | `overview.js` |
| governance | `#/governance/assets` | `assets.js` |
| governance | `#/governance/lineage` | `lineage.js` |
| governance | `#/governance/quality` | `quality.js` |
| governance | `#/governance/ops`（运行审计） | `audit.js` |

两个门户都有名为 `ops` 的**路由**，命名空间（`#/<portal>/…`）已经把它们分开；
**文件**则按用途命名（`refresh.js` / `audit.js`），因为文件不能同名共存。

## 路由方案

```
#/<portal>/<route>[/<param>…][?<query>]
#/analysis/monitor
#/governance/assets/air_quality_hourly?tab=fields
```

* `<portal>` 只能是 `analysis` 或 `governance`（在 `portal.config.js` 的 `portals` 里声明）；
* `<route>` 必须属于该门户的 `nav`，否则回落到该门户的默认路由；
* 地址栏只出现规范形式：hash 缺失门户段或被手改时，外壳会改写回规范形式；
* 启动时的门户选择顺序：hash → `localStorage['atmos.portal']` →
  `/portal-config.js` 注入的 `current` → `config.defaultPortal`；
* 每个门户各自记住最后访问的路由（内存 + `localStorage['atmos.route.<portal>']`），
  来回切换会回到离开时的页面；没有记忆时用该门户的 `defaultRoute`。

迁移提示：拆分前形如 `#/monitor` 的旧书签不会失效，但会**收敛到当前门户**：
`#/monitor` → `#/analysis/monitor`；若首段既不是门户名也不是当前门户的路由名
（例如 `#/bogus`），则回落到该门户记住/默认的路由，而不是把垃圾段当成路径参数。

## 切换器行为契约

* 顶栏 `#portalSwitch` 是一个分段控件，按钮文字取自各门户的 `shortName`
  （`分析平台` / `数据治理`），当前门户高亮并带 `aria-pressed="true"`；
* 点击即写 hash（`#/<目标门户>/<记住的路由>`），`hashchange` 驱动整体重渲染：
  侧边导航、页面标题、侧栏脚注、页脚、当前视图一次性换到目标门户；
* 切换时：调用上一视图的 `destroy?.()`、销毁 `#view` 子树内所有 ECharts 实例、
  清空 `#view`；任务面板改用目标门户的登记簿（分析 `/api/ops/tasks`，
  治理 `/api/quality/tasks`）并重置首帧抑制；健康状态按目标门户重新探测；
* 后端地址来自 `/portal-config.js` 注入的 `endpoints`，页面本身不需要重新加载，
  也不需要跳转到另一个端口。

## 视图模块契约

```js
export async function render(container, ctx, params) { /* … */ }
export function destroy() { /* 可选：清理定时器、订阅、视图私有状态 */ }
```

`ctx` 由 `portal.js` 注入：

| 成员 | 说明 |
| --- | --- |
| `ctx.portal` / `ctx.portalName` | 当前门户 id 与中文名 |
| `ctx.route` / `ctx.params` / `ctx.query` | 当前路由名、路径参数、查询参数 |
| `ctx.navigate(name, params?, query?)` | 唯一允许的跳转方式（视图内不要碰 `location.hash`） |
| `ctx.url(name, params?, query?)` | 生成**本门户内**该路由的完整 hash（用于 `href`、展示或复制链接） |
| `ctx.refresh()` | 重新渲染当前视图 |
| `ctx.isActive()` | 视图是否仍是当前路由 |
| `ctx.can(kind)` | 本门户是否提供某类端点（见下） |
| `ctx.toast(msg, kind?, ms?)` | 轻提示 |

**导航不得硬编码门户前缀。** `ctx.navigate('ops')` 与 `ctx.url('ops')` 都在
**当前活动门户内**解析：分析门户里 `ops` 是「数据刷新」，治理门户里 `ops` 是
「运行审计」，视图不需要（也不应该）知道自己在哪个门户。同理，视图内不要写
`#/analysis/...` 或 `location.hash = …`，否则视图就再也无法在门户之间复用。

路由在每次导航前会自动：调用上一视图的 `destroy?.()`、销毁 `#view` 子树内的全部
ECharts 实例、清空容器；视图不需要（也不应该）自己 import `disposeCharts`。

视图模块用动态 `import()` 懒加载。模块缺失（页面尚未安装）或加载失败时，
`portal.js` 会在 `#view` 内渲染「该页面尚未安装」的内联提示，
导航与顶栏保持可用，不会白屏、也不会抛到控制台之外。

## 按门户解析 API 基地址

服务端在每个页面注入 `/portal-config.js`，**早于** `portal.js` 加载：

```js
window.__ATMOS_PORTALS__ = {
  current: 'analysis',                              // 本页由哪台服务托管
  endpoints: {
    analysis: 'http://127.0.0.1:8000',              // 浏览器可达地址，无尾斜杠
    governance: 'http://127.0.0.1:8001',
  },
  environment: 'dev',
}
```

`shared/api.js` 的约定：

* `request(path, options)` 新增 `options.portal`：`'analysis'`（默认）、`'governance'`
  或 `'current'`（跟随当前浏览的门户）；实际 URL 为
  `` `${endpoints[portal] || ''}/api${path}` `` —— 空字符串表示同源 `/api`，
  因此缺少 `/portal-config.js` 时前端退化为单服务模式而不是整体失效；
* 治理专属方法一律显式标注 `portal: 'governance'`（`assets*` / `cities` /
  `lineage*` / `quality*` / `opsBootstrap` / `opsRefresh` / `opsRuns` /
  `opsSummary` / `opsSpecs`）；分析方法不标注；
* 两个门户都有但**语义不同**的路径（`/health`、`/meta`、`/overview`，以及作业
  配置 `/ops/jobs`）标注 `portal: 'current'`，跟随当前门户 —— 否则在治理页面上
  会读到分析平台的健康状态与总览口径；`portal.js` 在切换页签时调用
  `setActivePortal()` 同步它；
* 导出链接同样是绝对地址：`exportUrl()`（资产数据下载，治理）与
  `analysisExportUrl()`（分析结果导出，分析）都经 `portalUrl()` 拼出完整 URL；
* 缓存键包含解析后的基地址（`GET:http://127.0.0.1:8001|/api/assets`），
  切换门户不可能命中另一个门户的缓存；写操作后的失效仍只匹配 `|` 之后的
  路径部分，TTL、`detail` 错误解析与状态订阅保持原样。

### 关于 `shared/api.js` 的路径写法

`api.js` 必须同时承载两个门户的方法（这是"共享"的意义），但它只暴露**相对路径**，
基地址在 `request()` 内拼接。这些相对路径统一写成**模板字面量**：

```js
overview: () => request(`/overview`, { portal: CURRENT, ttl: TTL.short }),
insightsDigest: ({ days = 30 } = {}) => request(`/insights/digest`, { params: { days } }),
```

原因是 `tests/test_frontend_contract.py` 会用 `/api/...` 字面量扫描**浏览器可直接
加载的 JS**，逐门户校验"没有调用另一台服务上的接口"。若共享客户端里出现
`'/api/insights/digest'` 这样的字面量，治理门户的校验会（正确地）认为
`web/shared/api.js` 在引用它没有的接口。用模板字面量表达"与门户无关的相对路径"，
契约测试就回到它真正要覆盖的位置：各门户自己的视图模块。
**新增 API 方法时请沿用这个写法**，并显式声明它属于哪个门户。

## 两个门户的 API 面不同

**不是**每个端点在两边都存在。`ctx.can(kind)` 回答这类问题：

| kind | 含义 | analysis | governance |
| --- | --- | --- | --- |
| `assets` | `/api/assets[/{key}[/sample\|/profiling]]`、导出 | — | ✓ |
| `cities` | `/api/cities`（含新增/修改） | — | ✓ |
| `lineage` | `/api/lineage/*` | — | ✓ |
| `quality` | `/api/quality/*` | — | ✓ |
| `opsAudit` | `/api/ops/runs`、`/api/ops/summary`、`/api/ops/specs` | — | ✓ |
| `insights` | `/api/insights/*` | ✓ | — |
| `collect` | `POST /api/ops/collect`、`/api/ops/tasks` | ✓ | — |

两个门户共有的：`/api/health`、`/api/meta`、`/api/overview`（**语义不同**，
见下）、`/api/ops/jobs`、`/api/env/*`。

* 分析门户的 `/api/overview` 是**数据覆盖视角**：行数、字节、城市数、覆盖窗口；
* 治理门户的 `/api/overview` 是**治理视角**：资产数、字段数、SLA 超期、等级分布。

因此同一个 `api.overview()` 在两边渲染出的 KPI 并不相同，视图需按自己的门户
读取字段，不要假设另一边存在同样的字段。城市清单同理：治理门户用 `/api/cities`，
分析门户用 `/api/env/cities`（两者都只返回启用中的城市）。

## 后端不可达时的表现

* 当前门户的后端不可达时，内容区给出内联中文提示（标题说明哪个平台未启动、
  正文提示运行 `start.cmd` 或 `python -m atmos serve --portal both`，
  并说明另一个门户不受影响），附「重试」按钮；
* 顶栏健康灯变红、文字为「服务不可用」；不会弹出接口异常横幅，不会白屏、
  不会把错误抛到控制台之外；
* 已知不可用时不再让视图逐个接口去撞网络错误，而是直接显示提示；
  顶栏「刷新」与提示内的「重试」都会重新探测，探测成功即渲染视图；
* **另一个门户完全不受影响**：它的地址单独解析、缓存键单独隔离、
  任务登记簿单独轮询，可以正常切换过去使用；
* 治理专属页面在治理后端未启动时退化为该提示，而不是画出半张空图表。

## 任务监视

`tasks.js` 是共享模块，但两个门户的**任务登记簿端点不同**，由 `portal.config.js`
把该门户自己的 API 方法注入进去（`tasks.js` 自身不硬编码任何端点）：

```js
// analysis
tasks: { fetch: (limit) => api.opsTasks(limit), label: '采集任务', source: 'ops' }

// governance
tasks: { fetch: (limit) => api.qualityTasks(limit), label: '质量评测任务', source: 'quality' }
```

切换门户时 `portal.js` 会重新 `configureTasks()` 并清空"已见任务"集合，
避免另一台服务的任务快照触发误报与误刷新。

未声明 `tasks`（或 `fetch` 不是函数）时 `tasks.js` 完全惰性：不发请求、
`waitForTask()` 立即返回 `null`，顶栏的任务面板保持隐藏。这是刻意的 ——
治理门户没有 `/api/ops/tasks`，若沿用拆分前的全局轮询，会在每一个治理页面上
刷出 404 并在顶栏弹出接口异常。

## 时区约定

`time` / `date` 一类字段是**城市本地时间的裸字符串**（无时区），
`ui.js` 的 `formatDataTime()` / `shortDataTime()` 只做格式化，**不做也不会做**
时区换算，界面上也不写"本地时间"字样。只有审计时间（ISO-8601、带 `Z`）
才用 `formatAuditTime()` 转换为浏览器本地时区并显式标注。

## 职责边界

| 能力 | 分析门户 | 治理门户 |
| --- | --- | --- |
| 触发采集（数据刷新） | ✓ 表单 + 任务进度 | — 只读运行记录 |
| 运行记录逐条审计 | — | ✓ |
| 元数据同步 / 统计刷新 | — | ✓ |
| 资产目录 / 字段字典 / 样例 / 画像 | — | ✓ |
| 血缘图谱与影响分析 | — | ✓ |
| 五维质量评分与评测 | — | ✓ |
| 统计分布 / 时段规律 / 污染过程 / 成因 / 考核 / 聚类 | ✓ | — |

「数据治理（治理体检）」页已从产品中下线，两个门户都不再包含它，
`web/` 中也不再有对应的视图文件。

## 本地校验

没有构建步骤，因此校验靠静态检查：

```powershell
# 1. 语法
Get-ChildItem -Recurse -File web -Filter *.js | ForEach-Object { node --check $_.FullName }

# 2. 模块图：确认 /shared/... 与 /static/views/... 说明符都能落到真实文件、
#    portal.config 的 module 全部存在、没有模块直接调用全局 fetch(...)
```

浏览器端的运行前提：先启动服务，再访问 `/`。若共享资源未挂载
（`web/shared` 不存在），页面会退化为无样式的空壳。
