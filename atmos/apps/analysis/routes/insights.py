"""深度分析路由。

每一项分析都对应 :class:`~atmos.insights.service.InsightSpec` 中声明的业务问题，
返回结构里带 ``available`` 字段 —— 数据不足时返回 ``available=false`` 与
中文说明，而不是 4xx/5xx，前端据此展示"先去采集"的引导。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from atmos.apps.common.deps import get_insight_service
from atmos.insights.service import InsightService

router = APIRouter(prefix="/insights", tags=["深度分析"])


def _parse(value: str | None) -> list[str] | None:
    """解析逗号分隔参数。"""
    if not value:
        return None
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or None


@router.get("/catalog", summary="分析能力清单")
def catalog(service: InsightService = Depends(get_insight_service)) -> dict:
    """按类别列出全部分析能力及其回答的业务问题，附数据可用性与指标字典。"""
    return service.catalog()


@router.get("/digest", summary="分析摘要（首页）")
def digest(
    cities: str | None = Query(None, description="逗号分隔的城市 slug；为空表示全部"),
    days: int = Query(30, ge=1, le=365),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """考核概况 + 污染过程摘要 + 日内规律，供分析首页一次性取用。"""
    return service.digest(cities=_parse(cities), days=days)


# ============================================================
# 描述性分析
# ============================================================
@router.get("/distribution", summary="统计与分布")
def distribution(
    cities: str | None = Query(None),
    metrics: str | None = Query(None, description="逗号分隔的指标名"),
    days: int = Query(30, ge=1, le=365),
    bins: int = Query(20, ge=5, le=60),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """均值/标准差/分位数、箱线图五数概括、直方图分箱，并给出逐城市对比。"""
    return service.distribution(
        cities=_parse(cities), metrics=_parse(metrics), days=days, bins=bins
    )


@router.get("/correlation", summary="指标相关性矩阵")
def correlation(
    cities: str | None = Query(None),
    metrics: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """同时返回 Pearson 与 Spearman 矩阵，并挑出最强的前若干对供直接展示。"""
    return service.correlation(cities=_parse(cities), metrics=_parse(metrics), days=days)


# ============================================================
# 时段规律
# ============================================================
@router.get("/hourly-profile", summary="日内小时画像")
def hourly_profile(
    cities: str | None = Query(None),
    metric: str = Query("european_aqi"),
    days: int = Query(30, ge=1, le=365),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """城市 × 小时的热力图数据，并标出各城市的峰值/谷值时段与日内振幅。"""
    return service.hourly_profile(cities=_parse(cities), metric=metric, days=days)


@router.get("/weekly-profile", summary="周内规律画像")
def weekly_profile(
    cities: str | None = Query(None),
    metrics: str | None = Query(None),
    days: int = Query(90, ge=7, le=365),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """工作日与周末对比，以及周一至周日的逐日水平。"""
    return service.weekly_profile(cities=_parse(cities), metrics=_parse(metrics), days=days)


@router.get("/monthly-trend", summary="月度趋势")
def monthly_trend(
    cities: str | None = Query(None),
    metric: str = Query("european_aqi"),
    days: int = Query(92, ge=28, le=400),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """按月聚合的均值与极值，用于观察季节性变化与治理成效。"""
    return service.monthly_trend(cities=_parse(cities), metric=metric, days=days)


# ============================================================
# 异常与过程
# ============================================================
@router.get("/outliers", summary="异常点检测")
def outliers(
    cities: str | None = Query(None),
    metric: str = Query("european_aqi"),
    days: int = Query(30, ge=1, le=365),
    method: str = Query("zscore", pattern="^(zscore|iqr)$"),
    threshold: float = Query(3.0, ge=1.0, le=10.0),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """按 Z 分数或 Tukey 围栏标记异常时刻，并给出各城市的异常占比。"""
    return service.outliers(
        cities=_parse(cities), metric=metric, days=days, method=method, threshold=threshold
    )


@router.get("/episodes", summary="污染过程识别")
def episodes(
    cities: str | None = Query(None),
    days: int = Query(92, ge=1, le=400),
    threshold: float = Query(100.0, description="超标判定阈值（国标 AQI，默认 100 = 优良上界）"),
    min_gap_hours: int = Query(3, ge=0, le=24, description="允许合并的最大达标间隙"),
    min_duration_hours: int = Query(2, ge=1, le=48, description="成案的最短持续时长"),
    limit: int = Query(30, ge=1, le=200),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """把连续超标时段合并为污染过程，给出起止、时长、峰值、首要污染物与期间气象。"""
    return service.episodes(
        cities=_parse(cities),
        days=days,
        threshold=threshold,
        min_gap_hours=min_gap_hours,
        min_duration_hours=min_duration_hours,
        limit=limit,
    )


# ============================================================
# 归因分析
# ============================================================
@router.get("/relationship/pairs", summary="可分析的指标配对")
def relationship_pairs(service: InsightService = Depends(get_insight_service)) -> dict:
    """预设的"气象要素 → 污染物"配对及其业务含义。"""
    items = service.relationship_pairs()
    return {"total": len(items), "items": items}


@router.get("/relationship", summary="气象—空气质量关系")
def relationship(
    x: str = Query("wind_speed_10m", description="自变量（气象要素）"),
    y: str = Query("pm2_5", description="因变量（污染物）"),
    cities: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    bins: int = Query(8, ge=3, le=20),
    by_city: bool = Query(False, description="是否逐城市给出相关系数"),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """散点数据 + Pearson/Spearman 相关系数 + 分箱均值曲线 + 一元回归。"""
    return service.relationship(
        x=x, y=y, cities=_parse(cities), days=days, bins=bins, by_city=by_city
    )


# ============================================================
# 考核与报表
# ============================================================
@router.get("/compliance", summary="达标考核报表")
def compliance(
    cities: str | None = Query(None),
    days: int = Query(90, ge=7, le=400),
    good_ceiling: float = Query(100.0, description="优良天上限（国标 AQI，默认 100）"),
    target_ratio: float = Query(80.0, ge=0, le=100, description="优良天比例目标（%）"),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """逐城市优良天比例、等级天数分布、超标小时与目标完成度，附全平台汇总。"""
    return service.compliance(
        cities=_parse(cities), days=days, good_ceiling=good_ceiling, target_ratio=target_ratio
    )


@router.get("/comparison", summary="环比与同比")
def comparison(
    cities: str | None = Query(None),
    days: int = Query(30, ge=7, le=180),
    metric: str = Query("china_aqi"),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """与上一个等长周期比（环比）；同比依赖历史归档，数据不足时明确说明。"""
    return service.comparison(cities=_parse(cities), days=days, metric=metric)


# ============================================================
# 横向对比
# ============================================================
@router.get("/clustering", summary="城市聚类与相似度")
def clustering(
    cities: str | None = Query(None),
    days: int = Query(30, ge=1, le=365),
    features: str | None = Query(None, description="逗号分隔的聚类特征"),
    k: int | None = Query(None, ge=2, le=8, description="簇数；留空则按轮廓系数自动选择"),
    service: InsightService = Depends(get_insight_service),
) -> dict:
    """按环境特征对城市聚类，给出分组、命名、相似度矩阵与最相似城市对。"""
    return service.clustering(
        cities=_parse(cities), days=days, features=_parse(features), k=k
    )


@router.get("/{analysis}/export.csv", summary="导出分析结果")
def export_analysis(
    analysis: str,
    cities: str | None = Query(None),
    days: int = Query(90, ge=1, le=400),
    service: InsightService = Depends(get_insight_service),
):
    """把分析结果导出为 UTF-8 BOM 的 CSV（Excel 可直接打开）。

    支持 ``compliance``（达标考核报表）、``episodes``（污染过程清单）、
    ``distribution``（统计分布表）。
    """
    import csv
    import io
    from urllib.parse import quote

    from fastapi.responses import Response

    try:
        header, rows, title = service.export_rows(analysis, cities=_parse(cities), days=days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    payload = "\ufeff" + buffer.getvalue()

    return Response(
        content=payload.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="atmos_{analysis}.csv"',
            "X-Row-Count": str(len(rows)),
            # HTTP 头必须是 latin-1，中文报表名需百分号编码（RFC 3986）；
            # 直接放中文会让 Starlette 在编码响应头时抛 UnicodeEncodeError。
            "X-Report-Title": quote(title),
            "X-Report-Title-Encoding": "percent",
        },
    )
