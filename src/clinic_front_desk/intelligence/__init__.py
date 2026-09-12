"""Practice_Intelligence: autonomous pattern analysis and decision synthesis.

Populated across tasks 10.x: ``PatternDetectors`` (no-show trend, schedule gap,
unmet demand, unoffered-service demand, gap-fill matching) and the
``analyze_patterns`` tool (task 10.1); the ``DecisionSynthesizer`` (generation
gates + persistence, task 10.2); and the ``AnalysisScheduler`` (recurring
interval <= 24 h, task 10.3).
"""

from __future__ import annotations

from .analyze import analyze_patterns
from .detectors import (
    LOW_UTILIZATION_THRESHOLD,
    NO_SHOW_BASELINE_RATE,
    RECURRING_GAP_THRESHOLD,
    UNMET_DEMAND_THRESHOLD,
    PatternInput,
    detect_all,
    detect_gap_fill,
    detect_no_show_trend,
    detect_schedule_gaps,
    detect_unmet_demand,
    detect_unoffered_service_demand,
)

__all__ = [
    "analyze_patterns",
    "PatternInput",
    "detect_all",
    "detect_no_show_trend",
    "detect_schedule_gaps",
    "detect_unmet_demand",
    "detect_unoffered_service_demand",
    "detect_gap_fill",
    "NO_SHOW_BASELINE_RATE",
    "LOW_UTILIZATION_THRESHOLD",
    "RECURRING_GAP_THRESHOLD",
    "UNMET_DEMAND_THRESHOLD",
]
