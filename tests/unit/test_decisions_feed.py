"""Unit tests for the ``DecisionsFeed`` component (task 13.1).

Covers the pure Python view-model builder and the HTML renderer:
- open Decisions ordered newest-first (Req 14.1),
- approve/dismiss controls on every card (Req 14.2),
- optimistic-removal reconciliation key (stable card id) (Req 14.5),
- empty-state message when there are no open Decisions (Req 14.7),
- real-time-add hydration payload / endpoint embedding (Req 14.8),
- HTML-escaping of all interpolated text.
"""

from __future__ import annotations

import json

import pytest

from clinic_front_desk.dashboard.components.decisions_feed import (
    DEFAULT_DECISIONS_ENDPOINT,
    EMPTY_STATE_MESSAGE,
    LOAD_ERROR_MESSAGE,
    DecisionsFeedView,
    build_decisions_feed,
    build_decisions_feed_from_result,
    render_decisions_feed,
)
from clinic_front_desk.models import (
    Decision,
    DecisionKind,
    Err,
    Ok,
    StoreError,
    StoreErrorKind,
)


def _decision(
    id: str,
    *,
    generated_at: str,
    kind: DecisionKind = DecisionKind.NO_SHOW_TREND,
    summary: str = "summary",
    recommended_action: str = "do the thing",
    supporting_record_count: int = 5,
) -> Decision:
    return Decision(
        id=id,
        kind=kind,
        finding_key=f"fk-{id}",
        summary=summary,
        recommended_action=recommended_action,
        supporting_record_count=supporting_record_count,
        generated_at=generated_at,
    )


# ---------------------------------------------------------------------------
# build_decisions_feed
# ---------------------------------------------------------------------------


def test_orders_open_decisions_newest_first() -> None:
    """Cards are ordered most-recently-generated first regardless of input order
    (Req 14.1)."""
    decisions = [
        _decision("a", generated_at="2024-01-01T09:00:00+00:00"),
        _decision("c", generated_at="2024-03-01T09:00:00+00:00"),
        _decision("b", generated_at="2024-02-01T09:00:00+00:00"),
    ]

    view = build_decisions_feed(decisions)

    assert [card.id for card in view.cards] == ["c", "b", "a"]
    assert view.is_empty is False
    assert view.has_error is False


def test_each_card_has_approve_and_dismiss_controls() -> None:
    """Every card carries an approve and a dismiss control targeting its id
    (Req 14.2)."""
    view = build_decisions_feed(
        [_decision("d1", generated_at="2024-01-01T09:00:00+00:00")]
    )

    card = view.cards[0]
    assert card.approve.action == "approve"
    assert card.approve.decision_id == "d1"
    assert card.dismiss.action == "dismiss"
    assert card.dismiss.decision_id == "d1"
    # Stable reconciliation key for optimistic removal / real-time add.
    assert card.id == "d1"


def test_card_carries_kind_label() -> None:
    """A card exposes a human-readable label for its Decision kind."""
    view = build_decisions_feed(
        [
            _decision(
                "g", generated_at="2024-01-01T09:00:00+00:00",
                kind=DecisionKind.GAP_FILL,
            )
        ]
    )

    assert view.cards[0].kind == DecisionKind.GAP_FILL.value
    assert view.cards[0].kind_label == "Fill schedule gap"


def test_empty_feed_reports_empty_state() -> None:
    """With no open Decisions the view is empty and carries the empty-state
    message (Req 14.7)."""
    view = build_decisions_feed([])

    assert view.cards == []
    assert view.is_empty is True
    assert view.empty_state_message == EMPTY_STATE_MESSAGE
    assert view.has_error is False


# ---------------------------------------------------------------------------
# build_decisions_feed_from_result
# ---------------------------------------------------------------------------


def test_from_result_ok_builds_feed() -> None:
    result: Ok[list[Decision]] = Ok(
        [_decision("x", generated_at="2024-01-01T09:00:00+00:00")]
    )

    view = build_decisions_feed_from_result(result)

    assert [card.id for card in view.cards] == ["x"]
    assert view.has_error is False


def test_from_result_err_yields_error_view() -> None:
    """A read failure maps to a recoverable error view, not a misleading empty
    state."""
    result: Err[StoreError] = Err(
        StoreError(kind=StoreErrorKind.STORE_FAILURE, detail="boom")
    )

    view = build_decisions_feed_from_result(result)

    assert view.has_error is True
    assert view.is_empty is True
    assert view.cards == []
    assert view.empty_state_message == LOAD_ERROR_MESSAGE


# ---------------------------------------------------------------------------
# render_decisions_feed
# ---------------------------------------------------------------------------


def test_render_shows_empty_state_and_hides_list() -> None:
    html_out = render_decisions_feed([])

    assert EMPTY_STATE_MESSAGE in html_out
    # Empty-state message visible, list hidden.
    assert 'id="decisions-feed-empty" class="decisions-feed__empty" role="status">' in html_out
    assert 'id="decisions-feed-list" class="decisions-feed__list" hidden>' in html_out
    assert 'data-has-error="false"' in html_out


def test_render_emits_cards_with_controls() -> None:
    html_out = render_decisions_feed(
        [
            _decision("c2", generated_at="2024-02-01T09:00:00+00:00"),
            _decision("c1", generated_at="2024-01-01T09:00:00+00:00"),
        ]
    )

    # Newest-first order in the rendered markup (Req 14.1).
    assert html_out.index('data-decision-id="c2"') < html_out.index(
        'data-decision-id="c1"'
    )
    # Approve / dismiss controls present on each server-rendered card (Req 14.2).
    # (Match the per-card control, which carries data-decision-id, so the static
    # <template> button is not counted.)
    assert html_out.count('data-action="approve" data-decision-id=') == 2
    assert html_out.count('data-action="dismiss" data-decision-id=') == 2
    # List visible, empty-state hidden.
    assert 'id="decisions-feed-empty" class="decisions-feed__empty" role="status" hidden>' in html_out
    assert 'id="decisions-feed-list" class="decisions-feed__list">' in html_out


def test_render_escapes_interpolated_text() -> None:
    """Finding-derived text is HTML-escaped so it cannot inject markup."""
    html_out = render_decisions_feed(
        [
            _decision(
                "d",
                generated_at="2024-01-01T09:00:00+00:00",
                summary="<script>alert('x')</script>",
                recommended_action="a & b <b>",
            )
        ]
    )

    # Inspect the server-rendered card markup (everything before the JSON
    # hydration blob), where interpolated text must be HTML-escaped.
    card_markup = html_out.split(
        '<script id="decisions-feed-data"', 1
    )[0]
    assert "<script>alert" not in card_markup
    assert "&lt;script&gt;alert" in card_markup
    assert "a &amp; b &lt;b&gt;" in card_markup
    # The JSON blob cannot be broken out of: any closing tag is neutralized.
    assert "</script>" not in html_out.split(
        '<script id="decisions-feed-data"', 1
    )[1].split("</script>", 1)[0]


def test_render_embeds_endpoint_and_hydration_json() -> None:
    """The endpoint and view-model JSON are embedded for real-time add (Req 14.8)."""
    html_out = render_decisions_feed(
        [_decision("d", generated_at="2024-01-01T09:00:00+00:00")],
        decisions_endpoint="/custom/decisions",
    )

    assert 'data-decisions-endpoint="/custom/decisions"' in html_out

    start = html_out.index('<script id="decisions-feed-data" type="application/json">')
    body = html_out[start:]
    json_text = body.split(">", 1)[1].split("</script>", 1)[0].strip()
    payload = json.loads(json_text)
    assert payload["cards"][0]["id"] == "d"
    assert payload["is_empty"] is False
    assert payload["has_error"] is False


def test_render_accepts_prebuilt_view() -> None:
    """render accepts a pre-built view (e.g. an error view) and reflects it."""
    view = DecisionsFeedView(
        cards=[],
        is_empty=True,
        empty_state_message=LOAD_ERROR_MESSAGE,
        has_error=True,
    )

    html_out = render_decisions_feed(view)

    assert 'data-has-error="true"' in html_out
    assert LOAD_ERROR_MESSAGE in html_out


def test_render_uses_default_endpoint() -> None:
    html_out = render_decisions_feed([])
    assert f'data-decisions-endpoint="{DEFAULT_DECISIONS_ENDPOINT}"' in html_out


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
