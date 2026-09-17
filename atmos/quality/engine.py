"""数据质量评测引擎。

评测流程
--------
1. 从数据湖按城市取数，并按 **各城市本地时间** 裁出评测窗口
   （数据时间戳为城市本地时间，用 UTC 直接比较会产生时区偏移误差）；
2. 逐项执行已启用的检查，得到原始观测值与单项得分；
3. 按严重级别加权合成 **维度得分**，再按维度权重合成 **总评分**；
4. 映射等级并持久化：逐项明细写 ``quality_results``，
   汇总写 ``asset_quality_scores``，同时回写资产行的最新等级。

缺失维度不做"0 分惩罚"而是从分母中剔除（权重再归一化），
避免"资产只声明了三个维度就必然低分"的错误结论；
对应的检查会被标记为 skipped 并在报告中显式列出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from atmos.analytics.query import LakeQuery
from atmos.catalog.definitions import AssetRegistry, load_registry
from atmos.catalog.stats import refresh_asset_scores
from atmos.errors import QualityError
from atmos.lake import layout
from atmos.logging_setup import get_logger
from atmos.models import (
    AssetQualityScore,
    City,
    CollectionRun,
    DataAsset,
    QualityCheck,
    QualityResult,
    utcnow,
)
from atmos.models.enums import CheckStatus, RunStatus
from atmos.quality.checks import CheckContext, CheckOutcome, evaluator_for
from atmos.settings import Settings, get_settings
from atmos.sources.openmeteo import local_now
from atmos.utils import clamp, safe_ratio

logger = get_logger("quality.engine")

DEFAULT_WINDOW_HOURS = 168
GRADE_LABEL_FALLBACK = {"grade": "E", "label": "待治理", "color": "#dc2626"}


@dataclass
class DimensionResult:
    """单个维度的合成结果。"""

    key: str
    name: str
    weight: float
    score: float | None = None
    check_keys: list[str] = field(default_factory=list)
    weighted_sum: float = field(default=0.0, repr=False)
    weight_sum: float = field(default=0.0, repr=False)

    def add(self, outcome_score: float, severity_weight: float) -> None:
        """按严重级别权重累积。"""
        self.weighted_sum += outcome_score * severity_weight
        self.weight_sum += severity_weight

    def to_payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.name,
            "weight": self.weight,
            "score": None if self.score is None else round(self.score, 2),
            "checks": self.check_keys,
        }


@dataclass
class EvaluationReport:
    """一次资产质量评测的完整结果。"""

    asset_key: str
    asset_name: str
    city_slug: str | None
    window_hours: int
    scored_at: datetime
    dimensions: dict[str, DimensionResult] = field(default_factory=dict)
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    overall: float = 0.0
    grade: str = "E"
    grade_label: str = "待治理"
    grade_color: str = "#dc2626"
    checks_total: int = 0
    checks_failed: int = 0
    checks_warned: int = 0
    checks_skipped: int = 0
    cities_evaluated: int = 0
    rows_evaluated: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "asset_key": self.asset_key,
            "asset_name": self.asset_name,
            "city_slug": self.city_slug,
            "window_hours": self.window_hours,
            "scored_at": self.scored_at.isoformat(),
            "overall": round(self.overall, 2),
            "grade": self.grade,
            "grade_label": self.grade_label,
            "grade_color": self.grade_color,
            "dimensions": [item.to_payload() for item in self.dimensions.values()],
            "checks": self.outcomes,
            "summary": {
                "checks_total": self.checks_total,
                "checks_failed": self.checks_failed,
                "checks_warned": self.checks_warned,
                "checks_skipped": self.checks_skipped,
                "cities_evaluated": self.cities_evaluated,
                "rows_evaluated": self.rows_evaluated,
            },
        }


class QualityEngine:
    """五维数据质量评测引擎。"""

    def __init__(
        self,
        registry: AssetRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry or load_registry()
        self.settings = settings or get_settings()
        self.query = LakeQuery(self.settings)

    # ==============================================================
    # 对外入口
    # ==============================================================
    def evaluate_asset(
        self,
        session: Session,
        asset_key: str,
        *,
        city_slug: str | None = None,
        window_hours: int | None = None,
        run_id: str | None = None,
        persist: bool = True,
    ) -> EvaluationReport:
        """评测单个数据资产。"""
        if not self.registry.has_asset(asset_key):
            raise QualityError(f"未注册的数据资产: {asset_key}")

        definition = self.registry.asset(asset_key)
        model = self.registry.quality
        window = window_hours or int(
            model.defaults.get("evaluation_window_hours", DEFAULT_WINDOW_HOURS)
        )
        scored_at = utcnow()

        report = EvaluationReport(
            asset_key=asset_key,
            asset_name=definition.name,
            city_slug=city_slug,
            window_hours=window,
            scored_at=scored_at,
        )

        asset_row = session.scalar(select(DataAsset).where(DataAsset.asset_key == asset_key))
        if asset_row is None:
            raise QualityError(f"资产 {asset_key} 尚未同步到目录，请先执行 bootstrap")

        frames, local_nows = self._load_frames(asset_key, city_slug)
        report.cities_evaluated = len(frames)
        report.rows_evaluated = int(sum(len(frame) for frame in frames.values()))

        lake_schema = self.query.schema(asset_key)
        run_stats = self._run_stats(session, asset_row.id, window)

        checks = list(
            session.scalars(
                select(QualityCheck).where(
                    QualityCheck.asset_id == asset_row.id,
                    QualityCheck.is_enabled.is_(True),
                )
            ).all()
        )
        if not checks:
            logger.warning("资产 %s 未配置任何质量检查项", asset_key)

        dimension_state: dict[str, DimensionResult] = {
            item.key: DimensionResult(key=item.key, name=item.name, weight=item.weight)
            for item in model.dimensions
        }

        for check in sorted(checks, key=lambda item: item.check_key):
            definition_check = next(
                (item for item in model.checks if item.key == check.check_key), None
            )
            if definition_check is None:
                logger.warning("检查项 %s 缺少规则定义，已跳过", check.check_key)
                continue

            outcome = self._run_check(
                definition, definition_check, frames, local_nows, window, lake_schema, run_stats
            )
            report.checks_total += 1
            if outcome.status == CheckStatus.FAIL.value:
                report.checks_failed += 1
            elif outcome.status == CheckStatus.WARN.value:
                report.checks_warned += 1
            elif outcome.status == CheckStatus.SKIPPED.value:
                report.checks_skipped += 1

            report.outcomes.append(
                {
                    "check_key": check.check_key,
                    "name": check.name,
                    "dimension": check.dimension,
                    "dimension_label": dimension_state[check.dimension].name
                    if check.dimension in dimension_state
                    else check.dimension,
                    "severity": check.severity,
                    "category": definition_check.name,
                    **outcome.to_payload(),
                }
            )

            bucket = dimension_state.get(check.dimension)
            if bucket is None:
                continue
            if outcome.status in (CheckStatus.PASS.value, CheckStatus.WARN.value, CheckStatus.FAIL.value):
                bucket.check_keys.append(check.check_key)
                bucket.add(outcome.score, _severity_weight(check.severity))

        report.dimensions = dimension_state
        self._finalize(report, model)

        if persist:
            self._persist(session, asset_row, report, checks, run_id)

        logger.info(
            "质量评测 %s%s：总分 %.2f（%s），检查 %d 项，失败 %d、预警 %d、跳过 %d",
            asset_key,
            f"[{city_slug}]" if city_slug else "",
            report.overall,
            report.grade,
            report.checks_total,
            report.checks_failed,
            report.checks_warned,
            report.checks_skipped,
        )
        return report

    def evaluate_all(
        self,
        session: Session,
        *,
        window_hours: int | None = None,
        run_id: str | None = None,
        asset_keys: list[str] | None = None,
    ) -> list[EvaluationReport]:
        """评测全部（或指定）资产。单资产失败不中断整体流程。"""
        keys = asset_keys or [asset.asset_key for asset in self.registry.assets]
        reports: list[EvaluationReport] = []
        for asset_key in keys:
            try:
                reports.append(
                    self.evaluate_asset(
                        session, asset_key, window_hours=window_hours, run_id=run_id
                    )
                )
            except Exception as exc:  # pragma: no cover - 单资产失败隔离
                logger.error("资产 %s 质量评测失败: %s", asset_key, exc)
        return reports

    # ==============================================================
    # 内部步骤
    # ==============================================================
    def _load_frames(
        self, asset_key: str, city_slug: str | None
    ) -> tuple[dict[str, pd.DataFrame], dict[str, datetime]]:
        """按城市加载数据湖数据，并计算每个城市的本地当前时间。"""
        definition = self.registry.asset(asset_key)
        partitions = layout.list_partitions(asset_key, self.settings)
        if not partitions:
            return {}, {}

        slug_to_timezone = {city.slug: city.timezone for city in self.registry.cities}
        city_slugs = sorted({str(item["city"]) for item in partitions})
        if city_slug:
            city_slugs = [slug for slug in city_slugs if slug == city_slug]

        frames: dict[str, pd.DataFrame] = {}
        local_nows: dict[str, datetime] = {}
        for slug in city_slugs:
            frame = self.query.fetch(
                asset_key,
                city_slug=slug,
                time_column=definition.time_column or None,
            )
            if frame.empty:
                continue
            frames[slug] = frame
            local_nows[slug] = local_now(slug_to_timezone.get(slug, "UTC"))
        return frames, local_nows

    def _run_stats(self, session: Session, asset_id: int, window_hours: int) -> dict[str, int]:
        since = utcnow() - timedelta(hours=window_hours)
        runs = list(
            session.scalars(
                select(CollectionRun).where(
                    CollectionRun.asset_id == asset_id,
                    CollectionRun.started_at >= since,
                )
            ).all()
        )
        return {
            "total": len(runs),
            "success": sum(1 for run in runs if run.status == RunStatus.SUCCESS.value),
            "partial": sum(1 for run in runs if run.status == RunStatus.PARTIAL.value),
            "failed": sum(1 for run in runs if run.status == RunStatus.FAILED.value),
        }

    def _run_check(
        self,
        asset: Any,
        check: Any,
        frames: dict[str, pd.DataFrame],
        local_nows: dict[str, datetime],
        window_hours: int,
        lake_schema: dict[str, str],
        run_stats: dict[str, int],
    ) -> CheckOutcome:
        evaluator = evaluator_for(check.key)
        if evaluator is None:
            return CheckOutcome(
                status=CheckStatus.SKIPPED.value,
                score=0.0,
                message=f"检查项 {check.key} 尚无实现，已跳过",
            )
        context = CheckContext(
            asset=asset,
            check=check,
            frames=frames,
            city_local_now=local_nows,
            window_hours=window_hours,
            defaults=self.registry.quality.defaults,
            lake_schema=lake_schema,
            run_stats=run_stats,
        )
        try:
            return evaluator(context)
        except Exception as exc:
            logger.exception("检查项 %s 执行异常", check.key)
            return CheckOutcome(
                status=CheckStatus.ERROR.value,
                score=0.0,
                message=f"检查执行异常: {exc}",
                detail={"error": repr(exc)},
            )

    @staticmethod
    def _finalize(report: EvaluationReport, model: Any) -> None:
        """从累积量算出维度得分、总评分与等级。"""
        weighted_total = 0.0
        weight_total = 0.0

        for dimension in report.dimensions.values():
            if dimension.weight_sum > 0:
                dimension.score = dimension.weighted_sum / dimension.weight_sum
                weighted_total += dimension.score * dimension.weight
                weight_total += dimension.weight

        overall = safe_ratio(weighted_total, weight_total, default=0.0)
        report.overall = clamp(overall)

        band = model.grade_for(report.overall) or GRADE_LABEL_FALLBACK
        report.grade = str(band.get("grade", "E"))
        report.grade_label = str(band.get("label", "待治理"))
        report.grade_color = str(band.get("color", "#dc2626"))

    def _persist(
        self,
        session: Session,
        asset_row: DataAsset,
        report: EvaluationReport,
        checks: list[QualityCheck],
        run_id: str | None,
    ) -> None:
        """写入逐项明细与汇总评分。"""
        check_by_key = {check.check_key: check for check in checks}
        for outcome in report.outcomes:
            check_row = check_by_key.get(outcome["check_key"])
            session.add(
                QualityResult(
                    check_id=check_row.id if check_row else None,
                    asset_id=asset_row.id,
                    run_id=run_id,
                    check_key=outcome["check_key"],
                    dimension=outcome["dimension"],
                    status=outcome["status"],
                    score=float(outcome["score"]),
                    observed_value=_as_float(outcome.get("observed")),
                    expected_value=_as_float(outcome.get("expected")),
                    records_checked=int(outcome.get("records_checked", 0)),
                    records_failed=int(outcome.get("records_failed", 0)),
                    message=str(outcome.get("message") or ""),
                    detail={**outcome.get("detail", {}), "severity": outcome.get("severity")},
                    evaluated_at=report.scored_at,
                )
            )

        def dimension_value(key: str) -> float:
            dimension = report.dimensions.get(key)
            return round(dimension.score, 2) if dimension and dimension.score is not None else 0.0

        session.add(
            AssetQualityScore(
                asset_id=asset_row.id,
                city_id=None,
                completeness=dimension_value("completeness"),
                timeliness=dimension_value("timeliness"),
                validity=dimension_value("validity"),
                consistency=dimension_value("consistency"),
                uniqueness=dimension_value("uniqueness"),
                overall=round(report.overall, 2),
                grade=report.grade,
                rules_version=self.registry.quality.version,
                checks_total=report.checks_total,
                checks_failed=report.checks_failed,
                checks_warned=report.checks_warned,
                window_hours=report.window_hours,
                scored_at=report.scored_at,
            )
        )
        refresh_asset_scores(asset_row.asset_key, report.overall, report.grade, session)
        session.flush()


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return None if numeric != numeric else numeric


def _severity_weight(severity: str) -> float:
    """严重级别在维度内的权重系数。"""
    from atmos.models.enums import CheckSeverity

    try:
        return CheckSeverity(severity).weight
    except ValueError:
        return 1.0


__all__ = ["QualityEngine", "EvaluationReport", "DimensionResult", "DEFAULT_WINDOW_HOURS"]
