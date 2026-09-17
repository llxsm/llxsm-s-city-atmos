"""分析引擎门面。

API 层只与 ``InsightService`` 交互，不直接调用各个分析器 ——
这样"哪些分析能力存在、各自需要什么参数"只有一个清单，
新增分析能力时改动范围可控。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.insights import clustering, compliance, descriptive, episodes, relationship
from atmos.insights.loader import FrameLoader
from atmos.logging_setup import get_logger
from atmos.settings import Settings, get_settings

logger = get_logger("insights.service")


@dataclass(frozen=True)
class InsightSpec:
    """一项分析能力的元信息（供 API 暴露目录、供前端生成菜单）。"""

    key: str
    name: str
    question: str
    category: str
    endpoint: str
    requires: tuple[str, ...] = ()


INSIGHT_CATALOG: tuple[InsightSpec, ...] = (
    InsightSpec(
        key="distribution",
        name="统计与分布",
        question="数据长什么样？集中趋势、离散程度、长尾与离群点在哪？",
        category="描述性分析",
        endpoint="/api/insights/distribution",
        requires=("environment.hourly",),
    ),
    InsightSpec(
        key="correlation",
        name="指标相关性",
        question="哪些指标会同向变化？是线性关系还是单调非线性关系？",
        category="描述性分析",
        endpoint="/api/insights/correlation",
        requires=("environment.hourly",),
    ),
    InsightSpec(
        key="hourly-profile",
        name="日内规律画像",
        question="一天之内什么时候最脏？各城市的日变化幅度差多少？",
        category="时段规律",
        endpoint="/api/insights/hourly-profile",
        requires=("environment.hourly",),
    ),
    InsightSpec(
        key="weekly-profile",
        name="周内规律画像",
        question="工作日和周末有差别吗？差别有多大？",
        category="时段规律",
        endpoint="/api/insights/weekly-profile",
        requires=("environment.daily",),
    ),
    InsightSpec(
        key="monthly-trend",
        name="月度趋势",
        question="月度水平是在改善还是转差？",
        category="时段规律",
        endpoint="/api/insights/monthly-trend",
        requires=("environment.daily",),
    ),
    InsightSpec(
        key="outliers",
        name="异常点检测",
        question="哪些时刻的数值显著偏离常态？",
        category="异常与过程",
        endpoint="/api/insights/outliers",
        requires=("environment.hourly",),
    ),
    InsightSpec(
        key="episodes",
        name="污染过程识别",
        question="共有几次污染过程？各从何时开始、持续多久、峰值多少、成因是什么？",
        category="异常与过程",
        endpoint="/api/insights/episodes",
        requires=("environment.hourly",),
    ),
    InsightSpec(
        key="relationship",
        name="气象—空气质量关系",
        question="为什么这次污染重？风速、湿度、降水的影响有多大？",
        category="归因分析",
        endpoint="/api/insights/relationship",
        requires=("environment.hourly",),
    ),
    InsightSpec(
        key="compliance",
        name="达标考核",
        question="优良天比例达标了吗？哪些城市拖了后腿？",
        category="考核与报表",
        endpoint="/api/insights/compliance",
        requires=("environment.daily",),
    ),
    InsightSpec(
        key="comparison",
        name="环比与同比",
        question="和上一个周期、和去年同期比，是改善还是转差？",
        category="考核与报表",
        endpoint="/api/insights/comparison",
        requires=("environment.daily",),
    ),
    InsightSpec(
        key="clustering",
        name="城市聚类与相似度",
        question="哪些城市环境特征接近？可以归为同一类管理？",
        category="横向对比",
        endpoint="/api/insights/clustering",
        requires=("environment.hourly",),
    ),
)


class InsightService:
    """六大类分析能力的统一入口。"""

    def __init__(
        self,
        registry: AssetRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry or load_registry()
        self.settings = settings or get_settings()
        self.loader = FrameLoader(self.registry, self.settings)

    # ---------- 目录与可用性 ----------
    def catalog(self) -> dict[str, Any]:
        """分析能力清单 + 数据可用性，供前端生成菜单并提示"数据不足"。"""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for spec in INSIGHT_CATALOG:
            grouped.setdefault(spec.category, []).append(
                {
                    "key": spec.key,
                    "name": spec.name,
                    "question": spec.question,
                    "endpoint": spec.endpoint,
                    "requires": list(spec.requires),
                }
            )
        return {
            "categories": [
                {"category": category, "items": items} for category, items in grouped.items()
            ],
            "total": len(INSIGHT_CATALOG),
            "availability": self.loader.availability(),
            "metrics": [
                {
                    "name": item["name"],
                    "label": item["label"],
                    "unit": item["unit"],
                    "numeric": item["numeric"],
                    "comparable": item["comparable"],
                }
                for item in self.loader.environment.metric_catalog()
            ],
        }

    def availability(self) -> dict[str, Any]:
        return self.loader.availability()

    # ---------- 分析能力 ----------
    def distribution(self, **kwargs: Any) -> dict[str, Any]:
        return descriptive.distribution(self.loader, **kwargs)

    def correlation(self, **kwargs: Any) -> dict[str, Any]:
        return descriptive.correlation(self.loader, **kwargs)

    def hourly_profile(self, **kwargs: Any) -> dict[str, Any]:
        return descriptive.hourly_profile(self.loader, **kwargs)

    def weekly_profile(self, **kwargs: Any) -> dict[str, Any]:
        return descriptive.weekly_profile(self.loader, **kwargs)

    def monthly_trend(self, **kwargs: Any) -> dict[str, Any]:
        return descriptive.monthly_trend(self.loader, **kwargs)

    def outliers(self, **kwargs: Any) -> dict[str, Any]:
        return episodes.detect_outliers(self.loader, **kwargs)

    def episodes(self, **kwargs: Any) -> dict[str, Any]:
        return episodes.detect_episodes(self.loader, **kwargs)

    def relationship(self, **kwargs: Any) -> dict[str, Any]:
        return relationship.analyse_pair(self.loader, **kwargs)

    def relationship_pairs(self) -> list[dict[str, Any]]:
        return relationship.list_pairs()

    def compliance(self, **kwargs: Any) -> dict[str, Any]:
        return compliance.compliance_report(self.loader, **kwargs)

    def comparison(self, **kwargs: Any) -> dict[str, Any]:
        return compliance.period_comparison(self.loader, **kwargs)

    def clustering(self, **kwargs: Any) -> dict[str, Any]:
        return clustering.cluster_cities(self.loader, **kwargs)

    # ---------- 一站式分析摘要 ----------
    def digest(
        self, *, cities: list[str] | None = None, days: int = 30
    ) -> dict[str, Any]:
        """给"分析首页"用的精简摘要：几个关键结论 + 一跳入口。

        刻意只做必要的几次查询，避免首页加载时把全部分析都跑一遍。
        """
        report = self.compliance(cities=cities, days=max(days, 30))
        episode_data = self.episodes(cities=cities, days=max(days, 30), limit=5)
        profile = self.hourly_profile(cities=cities, days=max(days, 30))

        return {
            "available": report.get("available", False) or episode_data.get("available", False),
            "window_days": days,
            "compliance": {
                "good_day_ratio": report.get("totals", {}).get("good_day_ratio"),
                "aqi_avg": report.get("totals", {}).get("aqi_avg"),
                "best_city": report.get("totals", {}).get("best_city"),
                "worst_city": report.get("totals", {}).get("worst_city"),
                "city_count": report.get("totals", {}).get("city_count"),
                "target_ratio": report.get("target_ratio"),
            },
            "episodes": {
                "count": episode_data.get("episode_count"),
                "total_hours": episode_data.get("total_hours"),
                "worst_city": episode_data.get("worst_city"),
                "chronic_episode_count": episode_data.get("chronic_episode_count"),
                "chronic_cities": episode_data.get("chronic_cities"),
                "top": episode_data.get("episodes", [])[:5],
            },
            "profile": {
                "most_volatile": profile.get("most_volatile"),
                "rows": profile.get("rows", [])[:5],
            },
            "messages": [
                message
                for message in (
                    report.get("message"),
                    episode_data.get("message"),
                )
                if message
            ],
        }

    # ---------- 导出 ----------
    def export_rows(
        self, analysis: str, **kwargs: Any
    ) -> tuple[list[str], list[list[Any]], str]:
        """导出分析结果的行列与建议文件名。

        只实现真正需要"拿走"的分析（考核报表、污染过程、统计分布），
        其余分析以 JSON 输出为主。
        """
        if analysis == "compliance":
            report = self.compliance(**kwargs)
            if not report.get("available"):
                raise ValueError(str(report.get("message") or "无可用数据"))
            header, rows = compliance.report_rows(report)
            return header, rows, "达标考核报表"

        if analysis == "episodes":
            data = self.episodes(**kwargs)
            header = [
                "序号",
                "城市",
                "开始时间",
                "结束时间",
                "持续小时",
                "超标小时",
                "峰值AQI",
                "峰值等级",
                "均值AQI",
                "首要污染物",
                "平均风速(km/h)",
                "平均湿度(%)",
                "累计降水(mm)",
                "静稳小时",
                "严重度",
                "持续性污染",
            ]
            rows = [
                [
                    item.get("rank"),
                    item.get("city_name"),
                    item.get("start"),
                    item.get("end"),
                    item.get("duration_hours"),
                    item.get("exceed_hours"),
                    item.get("peak_value"),
                    item.get("peak_level"),
                    item.get("mean_value"),
                    item.get("primary_pollutant"),
                    (item.get("weather") or {}).get("wind_speed_avg"),
                    (item.get("weather") or {}).get("humidity_avg"),
                    (item.get("weather") or {}).get("precipitation_sum"),
                    item.get("weather") and (item.get("weather") or {}).get("stagnant_hours"),
                    item.get("severity"),
                    "是" if item.get("chronic") else "否",
                ]
                for item in data.get("episodes", [])
            ]
            return header, rows, "污染过程清单"

        if analysis == "distribution":
            data = self.distribution(**kwargs)
            header = [
                "指标",
                "城市",
                "样本数",
                "均值",
                "标准差",
                "最小",
                "P25",
                "中位数",
                "P75",
                "最大",
                "四分位距",
            ]
            rows = []
            for metric in data.get("metrics", []):
                for city in metric.get("by_city", []):
                    rows.append(
                        [
                            metric.get("label"),
                            city.get("city_name"),
                            city.get("count"),
                            city.get("mean"),
                            city.get("std"),
                            city.get("min"),
                            city.get("p25"),
                            city.get("p50"),
                            city.get("p75"),
                            city.get("max"),
                            city.get("iqr"),
                        ]
                    )
            return header, rows, "统计分布表"

        raise ValueError(f"不支持导出的分析类型: {analysis}")


__all__ = ["InsightService", "InsightSpec", "INSIGHT_CATALOG"]
