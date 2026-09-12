"""The ``analyze_patterns`` Strands tool (task 10.1, Req 13.2).

``analyze_patterns`` reads over the accumulated Appointment / Waitlist /
Call_Session snapshot (a :class:`~clinic_front_desk.intelligence.detectors.PatternInput`)
and returns the aggregated :class:`~clinic_front_desk.models.Finding` list
produced by the :mod:`~clinic_front_desk.intelligence.detectors` suite.

Like the other tools it returns a discriminated
:data:`~clinic_front_desk.models.ToolResult` so the caller can branch on
failure: any unexpected error while analysing surfaces as an
:class:`~clinic_front_desk.models.Err` carrying a
:class:`~clinic_front_desk.models.StoreFailure`. The
``DecisionSynthesizer`` (task 10.2) treats that ``Err`` as the Req 13.7
analysis-failure path (generate no Decision, record the failure); on ``Ok`` it
applies the ≥ 5-record / actionability / dedupe generation gates to the returned
findings.

The tool itself applies **no** generation gates — it aggregates raw findings so
the synthesizer remains the single place those gates live (design "Decision
generation flow").
"""

from __future__ import annotations

from clinic_front_desk.models import Err, Finding, Ok, StoreFailure, ToolResult

from .detectors import PatternInput, detect_all


def analyze_patterns(snapshot: PatternInput) -> ToolResult[list[Finding]]:
    """Analyse the accumulated data snapshot and return aggregated findings (Req 13.2).

    Args:
        snapshot: The Appointment / Slot / Waitlist / Call_Session data assembled
            from the Data_Layer, with the analysis window and offered-service
            context.

    Returns:
        ``Ok(list[Finding])`` — the aggregated findings across all detectors,
        each carrying a stable ``findingKey`` and an accurate
        ``supporting_record_count`` (empty when nothing qualifies). On an
        unexpected analysis error, ``Err(StoreFailure)`` so the caller can take
        the Req 13.7 no-Decision-and-record-failure path.
    """
    try:
        findings = detect_all(snapshot)
    except Exception as exc:  # pragma: no cover - defensive: Req 13.7 failure path
        return Err(
            StoreFailure(store="analyze_patterns", detail=f"analysis failed: {exc}")
        )
    return Ok(findings)


__all__ = ["analyze_patterns", "PatternInput"]
