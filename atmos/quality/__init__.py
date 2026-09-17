"""数据质量层：五维模型、检查项实现与评测引擎。"""

from atmos.quality.checks import (
    CheckContext,
    CheckOutcome,
    evaluator_for,
    register,
    registered_keys,
)
from atmos.quality.engine import DEFAULT_WINDOW_HOURS, DimensionResult, EvaluationReport, QualityEngine
from atmos.quality.service import (
    latest_scores,
    list_checks,
    open_issues_count,
    quality_summary,
    recent_results,
    score_history,
)

__all__ = [
    "QualityEngine",
    "EvaluationReport",
    "DimensionResult",
    "DEFAULT_WINDOW_HOURS",
    "CheckContext",
    "CheckOutcome",
    "register",
    "evaluator_for",
    "registered_keys",
    "latest_scores",
    "score_history",
    "recent_results",
    "list_checks",
    "quality_summary",
    "open_issues_count",
]
