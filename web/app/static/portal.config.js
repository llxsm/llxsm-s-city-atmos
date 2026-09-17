/**
 * portal.config.js —— 两个门户的导航与元信息（统一前端唯一的一份配置）。
 *
 * 两个后端服务（分析 8000 / 治理 8001）托管同一份外壳，因此这里同时声明两个
 * 门户；顶栏的分段切换器据此生成，点击即整体切换侧边导航与内容区，无需刷新。
 *
 * 门户差异集中在本文件的 `portals` 里：
 *   * nav          —— 侧边导航（section 项只作为分组标题）；
 *   * defaultRoute —— 没有记忆路由时进入的页面；
 *   * tasks        —— 该门户的任务登记簿（分析 /api/ops/tasks，治理 /api/quality/tasks）；
 *   * capabilities —— 该门户的 API 能力边界（视图用 ctx.can(kind) 查询）。
 *
 * 路由一律命名空间化：`#/analysis/...`、`#/governance/...`。两个门户都有
 * `ops` 路由（分析「数据刷新」、治理「运行审计」），文件与路由靠前缀区分：
 * 视图文件按用途命名（refresh.js / audit.js），路由名则保持各自门户内的 `ops`，
 * 因为 `#/<portal>/ops` 已经不会冲突。
 *
 * module 用统一前端静态目录的绝对路径（`/static` 挂载到 web/app/static），
 * 共享模块一律走 `/shared/*`。模块缺失时 portal.js 会在页面内给出
 * 「该页面尚未安装」的提示，而不是抛错或白屏。
 */

import { startPortal } from '/shared/portal.js';
import { api } from '/shared/api.js';

export const PORTAL_CONFIG = {
  appName: '城市环境数据平台',
  defaultPortal: 'analysis',

  portals: {
    // ================================================================
    // 分析门户（端口 8000）—— 面向业务分析人员，导航按"看什么"分组：
    // 概览 → 实时监测 → 深度分析 → 数据管理
    // ================================================================
    analysis: {
      name: '城市环境数据分析平台',
      shortName: '分析平台',
      brandSub: '分析门户',
      brandMark: 'AN',
      subtitle: '从实时实况到归因与考核',
      defaultRoute: 'digest',

      nav: [
        { section: '概览' },
        {
          route: 'digest',
          title: '分析总览',
          subtitle: '关键结论、重点污染过程与快捷入口',
          module: '/static/views/digest.js',
        },

        { section: '实时监测' },
        {
          route: 'monitor',
          title: '环境监测',
          subtitle: '城市实况、逐小时曲线、预警与排名',
          module: '/static/views/monitor.js',
        },
        {
          route: 'compare',
          title: '多城市对比',
          subtitle: '同一指标跨城市横向比较',
          module: '/static/views/compare.js',
        },
        {
          route: 'trend',
          title: '历史趋势',
          subtitle: '基于 ERA5 归档资产的长期序列',
          module: '/static/views/trend.js',
        },

        { section: '深度分析' },
        {
          route: 'distribution',
          title: '统计与分布',
          subtitle: '分位数、直方图与指标相关性',
          module: '/static/views/distribution.js',
        },
        {
          route: 'profile',
          title: '时段规律',
          subtitle: '日内小时画像、工作日与周末对比、月度趋势',
          module: '/static/views/profile.js',
        },
        {
          route: 'episodes',
          title: '污染过程',
          subtitle: '连续超标过程识别与异常点检测',
          module: '/static/views/episodes.js',
        },
        {
          route: 'relationship',
          title: '成因分析',
          subtitle: '气象要素与污染物的关系与回归',
          module: '/static/views/relationship.js',
        },
        {
          route: 'compliance',
          title: '达标考核',
          subtitle: '优良天比例、等级分布与目标完成度',
          module: '/static/views/compliance.js',
        },
        {
          route: 'clustering',
          title: '城市聚类',
          subtitle: '环境特征相似度与城市分组',
          module: '/static/views/clustering.js',
        },

        { section: '数据管理' },
        {
          route: 'ops',
          title: '数据刷新',
          subtitle: '触发采集、后台任务进度与作业清单',
          module: '/static/views/refresh.js',
        },
      ],

      // 分析门户的任务登记簿：采集任务（GET /api/ops/tasks）。
      // fetch 由门户注入，tasks.js 不硬编码端点，也不感知另一个门户的存在。
      tasks: { fetch: (limit) => api.opsTasks(limit), label: '采集任务', source: 'ops' },

      // 侧边栏脚注：本门户的能力边界
      sidebarMeta: [
        ['数据源', 'Open-Meteo'],
        ['存储', 'Parquet 数据湖'],
        ['查询引擎', 'DuckDB'],
      ],
    },

    // ================================================================
    // 治理门户（端口 8001）—— 面向数据治理部门，导航只分四组：
    // 概览 → 资产 → 质量 → 运维
    // 「数据治理（治理体检）」页已从产品下线；「运行审计」也不再包含触发采集的表单。
    // ================================================================
    governance: {
      name: '城市环境数据资产治理平台',
      shortName: '数据治理',
      brandSub: '治理门户',
      brandMark: 'GV',
      subtitle: '资产目录、血缘、质量与运行审计',
      defaultRoute: 'overview',

      nav: [
        { section: '概览' },
        {
          route: 'overview',
          title: '资产总览',
          subtitle: '资产规模、质量分布、SLA 超期与最近运行',
          module: '/static/views/overview.js',
        },

        { section: '资产' },
        {
          route: 'assets',
          title: '资产目录',
          subtitle: '资产卡片、字段字典、版本快照与样例数据',
          module: '/static/views/assets.js',
        },
        {
          route: 'lineage',
          title: '数据血缘',
          subtitle: '从数据源到服务层的加工链路与影响分析',
          module: '/static/views/lineage.js',
        },

        { section: '质量' },
        {
          route: 'quality',
          title: '数据质量',
          subtitle: '五维评分模型、检查明细与评测触发',
          module: '/static/views/quality.js',
        },

        { section: '运维' },
        {
          route: 'ops',
          title: '运行审计',
          subtitle: '作业清单、元数据同步、统计刷新与运行记录',
          module: '/static/views/audit.js',
        },
      ],

      // 治理门户的任务登记簿：质量评测任务（GET /api/quality/tasks）。
      // 治理门户没有 /api/ops/tasks，因此这里注入 qualityTasks；
      // 顶栏的任务面板与「数据质量」页共用这一个数据源。
      tasks: { fetch: (limit) => api.qualityTasks(limit), label: '质量评测任务', source: 'quality' },

      sidebarMeta: [
        ['数据源', 'Open-Meteo'],
        ['存储', 'Parquet 数据湖'],
        ['元数据', 'SQLite 资产目录'],
      ],
    },
  },
};

/** 启动统一外壳（模块内自执行，index.html 只需引入本文件）。 */
export const portal = startPortal(PORTAL_CONFIG);

export default portal;

