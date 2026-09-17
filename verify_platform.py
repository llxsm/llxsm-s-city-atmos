"""两个平台的真实数据端到端验证。

启动两个服务后，逐一验证：接口可用、职责边界、以及六大分析能力
在真实 Open-Meteo 数据上给出的结论是否合理。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")

ANALYSIS = "http://127.0.0.1:8000"
GOVERNANCE = "http://127.0.0.1:8001"


def get(base: str, path: str):
    with urllib.request.urlopen(f"{base}{path}", timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def status(base: str, path: str, method: str = "GET") -> int:
    request = urllib.request.Request(f"{base}{path}", method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code


print("=" * 78)
print("一、两个平台各自健康，且共享同一份数据")
print("=" * 78)
a_health = get(ANALYSIS, "/api/health")
g_health = get(GOVERNANCE, "/api/health")
print(f"  分析平台 {a_health['status']:<6} portal={a_health['portal']:<10} 名称={a_health['name']}")
print(
    f"  治理平台 {g_health['status']:<6} portal={g_health['portal']:<10} "
    f"名称={g_health['name']}  资产={g_health['registry']['assets']} 城市={g_health['registry']['cities']}"
)
a_over = get(ANALYSIS, "/api/overview")
g_over = get(GOVERNANCE, "/api/overview")
print(
    f"  共享数据：分析平台看到 {a_over['total_rows']:,} 行 / {a_over['city_count']} 城市；"
    f"治理平台看到 {g_over['total_rows']:,} 行 / {g_over['city_count']} 城市"
)
print(f"  数据窗口：逐小时 {a_over['analysis_windows']['hourly_days']} 天、"
      f"逐日 {a_over['analysis_windows']['daily_days']} 天、归档 {a_over['analysis_windows']['archive_days']} 天")

print()
print("=" * 78)
print("二、职责边界（接口集合互不重叠）")
print("=" * 78)
a_paths = set(get(ANALYSIS, "/api/openapi.json")["paths"])
g_paths = set(get(GOVERNANCE, "/api/openapi.json")["paths"])
print(f"  分析平台 {len(a_paths)} 个路径，治理平台 {len(g_paths)} 个路径")
print(f"  交集（仅框架接口）：{sorted(a_paths & g_paths)}")

checks = [
    (ANALYSIS, "/api/insights/compliance", 200, "分析平台有达标考核"),
    (GOVERNANCE, "/api/insights/compliance", 404, "治理平台无达标考核"),
    (GOVERNANCE, "/api/assets", 200, "治理平台有资产目录"),
    (ANALYSIS, "/api/assets", 404, "分析平台无资产目录"),
    (GOVERNANCE, "/api/quality/summary", 200, "治理平台有质量评分"),
    (ANALYSIS, "/api/quality/summary", 404, "分析平台无质量评分"),
    (ANALYSIS, "/api/env/current", 200, "分析平台有环境实况"),
    (GOVERNANCE, "/api/env/current", 404, "治理平台无环境数据"),
]
for base, path, expected, note in checks:
    actual = status(base, path)
    flag = "OK " if actual == expected else "!! "
    print(f"  {flag}{note:<22} {path:<34} -> {actual}")

print(f"  治理平台触发采集: POST /api/ops/collect -> {status(GOVERNANCE, '/api/ops/collect', 'POST')}（应为 404/405，刷新数据归分析平台）")

print()
print("=" * 78)
print("三、六大分析能力（真实数据结论）")
print("=" * 78)

episodes = get(ANALYSIS, "/api/insights/episodes?days=92&threshold=40&limit=5")
print(f"\n【污染过程识别】窗口 {episodes['window_days']} 天 / {episodes['city_count']} 城市")
print(f"  共识别 {episodes['episode_count']} 次过程，累计超标 {episodes['total_hours']} 小时")
worst = episodes.get("worst_city") or {}
if worst:
    print(f"  最严重城市：{worst['city_name']}（{worst['count']} 次过程 / {worst['hours']} 小时 / 峰值 {worst['peak']}）")
for item in episodes["episodes"][:3]:
    weather = item["weather"]

    def fmt(value, digits=1):
        return "—" if value is None else f"{value:.{digits}f}"

    print(
        f"  #{item['rank']} {item['city_name']:<5} {item['start'][:16]} → {item['end'][:16]} "
        f"{item['duration_hours']:>3}h 峰值 {item['peak_value']:>5}({item['peak_level']}) "
        f"首要 {item['primary_pollutant']:<6} 风速 {fmt(weather['wind_speed_avg'])} "
        f"湿度 {fmt(weather['humidity_avg'], 0)} 静稳 {fmt(weather['stagnant_hours'], 0)}h"
        + ("  [持续性污染]" if item.get("chronic") else "")
    )
print(f"  其中持续性污染 {episodes.get('chronic_episode_count', 0)} 次"
      f"（{'、'.join(episodes.get('chronic_cities') or []) or '无'}）")

rel = get(ANALYSIS, "/api/insights/relationship?x=wind_speed_10m&y=pm2_5&days=92")
print(f"\n【成因分析】风速 → PM2.5（{rel['samples']:,} 个配对样本）")
print(f"  Pearson {rel['pearson']}（{rel['pearson_label']}）  Spearman {rel['spearman']}（{rel['spearman_label']}）")
print(f"  回归：斜率 {rel['regression']['slope']}，R² {rel['regression']['r2']}")
print(f"  结论：{rel['interpretation']}")

rel2 = get(ANALYSIS, "/api/insights/relationship?x=temperature_2m&y=ozone&days=92")
print(f"  对照 —— 气温 → 臭氧：Pearson {rel2['pearson']}（{rel2['pearson_label']}）")

comp = get(ANALYSIS, "/api/insights/compliance?days=92&target_ratio=80")
totals = comp["totals"]
print(f"\n【达标考核】{comp['period']['start']} → {comp['period']['end']}，目标优良率 {comp['target_ratio']}%")
print(f"  平台整体：优良天 {totals['good_days']}/{totals['station_days']} = {totals['good_day_ratio']}%，日均 AQI {totals['aqi_avg']}")
print(f"  最好 {totals['best_city']} / 最差 {totals['worst_city']}")
print(f"  {'城市':<7}{'优良率':>8}{'达标':>6}{'差距':>8}{'日均AQI':>9}{'峰值':>7}{'超标h':>7}")
for row in comp["cities"][:5]:
    print(
        f"  {row['city_name']:<7}{row['good_day_ratio']:>7}%"
        f"{('是' if row['target_met'] else '否'):>6}{row['gap_to_target']:>7}"
        f"{row['aqi_avg']:>9}{row['aqi_peak']:>7}{row['exceed_hours']:>7}"
    )
print(f"  ...（共 {len(comp['cities'])} 个城市）")

compare = get(ANALYSIS, "/api/insights/comparison?days=30&metric=china_aqi")
print(f"\n【环比】{compare['current_period']['start']} → {compare['current_period']['end']} "
      f"对比 {compare['previous_period']['start']} → {compare['previous_period']['end']}")
improved = [row for row in compare["cities"] if row["trend"] == "改善"]
worsened = [row for row in compare["cities"] if row["trend"] == "转差"]
print(f"  改善 {len(improved)} 个城市，转差 {len(worsened)} 个城市")
for row in compare["cities"][:3]:
    print(f"  {row['city_name']:<7} {row['previous']} → {row['current']}  {row['trend']} {row['change_ratio']}%")
yoy = compare["year_over_year"]
print(f"  同比：{'可用' if yoy.get('available') else '不可用 —— ' + (yoy.get('message') or '')[:60]}")

cluster = get(ANALYSIS, "/api/insights/clustering?days=92")
print(f"\n【城市聚类】{cluster['city_count']} 个城市 → {cluster['k']} 组，轮廓系数 {cluster['silhouette']}")
for item in cluster["clusters"]:
    members = "、".join(member["city_name"] for member in item["members"])
    print(f"  组{item['cluster_id']}「{item['label']}」({item['size']} 个)：{members}")
pairs = cluster["most_similar_pairs"][:2]
for pair in pairs:
    print(f"  最相似：{pair['left_name']} ↔ {pair['right_name']} = {pair['similarity']}")

profile = get(ANALYSIS, "/api/insights/hourly-profile?days=92&metric=european_aqi")
print(f"\n【时段规律】日内 AQI 画像（{profile['window_days']} 天）")
print(f"  日内波动最大：{profile['most_volatile']}")
for row in profile["rows"][:3]:
    print(
        f"  {row['city_name']:<7} 峰值 {row['peak_value']:>5} @ {row['peak_hour']:>2}时"
        f"   谷值 {row['trough_value']:>5} @ {row['trough_hour']:>2}时   振幅 {row['amplitude']}"
    )

weekly = get(ANALYSIS, "/api/insights/weekly-profile?days=92")
print("\n【周内规律】")
for metric in weekly["metrics"][:3]:
    print(
        f"  {metric['label']:<12} 工作日 {metric['weekday_mean']:>7} / 周末 {metric['weekend_mean']:>7}"
        f"  → {metric['higher_on']}更高，差异 {metric['delta_ratio']}%"
    )

dist = get(ANALYSIS, "/api/insights/distribution?days=92&metrics=pm2_5,european_aqi")
print("\n【统计与分布】")
for metric in dist["metrics"]:
    overall = metric["overall"]
    box = metric["boxplot"]
    print(
        f"  {metric['label']:<12} 均值 {overall['mean']:>7} 中位 {overall['p50']:>7} "
        f"P95 {overall['p95']:>7} 最大 {overall['max']:>7} 标准差 {overall['std']:>7} "
        f"离群点 {box['outlier_count']}"
    )

corr = get(ANALYSIS, "/api/insights/correlation?days=92")
print("\n【相关性矩阵】最强的前 4 对：")
for item in corr["highlights"][:4]:
    print(
        f"  {item['left_label']:<12} ↔ {item['right_label']:<12} "
        f"Pearson {item['pearson']:>7}  Spearman {item['spearman']:>7}  {item['strength']}"
    )

outliers = get(ANALYSIS, "/api/insights/outliers?days=92&metric=european_aqi&method=zscore&threshold=3")
print(f"\n【异常检测】Z 分数 > 3：{outliers['flagged']} / {outliers['samples']:,} "
      f"（{outliers['flagged_ratio']}%），最严重 {outliers['points'][0]['city_name']} "
      f"{outliers['points'][0]['time'][:16]} AQI {outliers['points'][0]['value']}")

export = urllib.request.urlopen(f"{ANALYSIS}/api/insights/compliance/export.csv?days=92", timeout=120)
body = export.read().decode("utf-8-sig")
print(f"\n【报表导出】达标考核 CSV：{export.headers['X-Row-Count']} 行，"
      f"{len(body.splitlines()[0].split(','))} 列")

print()
print("=" * 78)
print("四、治理平台（精简后的核心能力）")
print("=" * 78)
assets = get(GOVERNANCE, "/api/assets")
print(f"  资产目录：{assets['total']} 项，分层分布 {assets['facets']['layer']}")
detail = get(GOVERNANCE, "/api/assets/atmos.city_environment.hourly")
print(f"  字段字典：{detail['name']} 共 {len(detail['columns'])} 个字段，"
      f"责任人 {detail['owner']}，SLA {detail['sla_freshness_minutes']} 分钟")
graph = get(GOVERNANCE, "/api/lineage/graph")
print(f"  数据血缘：{len(graph['nodes'])} 节点 / {len(graph['edges'])} 条边，最大深度 {graph['stats']['max_depth']}，"
      f"孤立资产 {graph['stats']['isolated_count']}")
impact = get(GOVERNANCE, "/api/lineage/openmeteo.weather.forecast.hourly/impact?direction=downstream")
print(f"  影响分析：天气预报变更会波及 {impact['summary']['related_count']} 个下游资产")
quality = get(GOVERNANCE, "/api/quality/summary")
print(f"  质量评分：{quality['evaluated_assets']} 项资产，平均 {quality['average_overall']}，"
      f"等级分布 {quality['grade_distribution']}")
print(f"  五维均值：{quality['dimension_average']}")
