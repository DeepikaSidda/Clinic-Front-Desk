"""Unit tests for the dashboard OnboardingWizard component (task 13.4, Req 1.1-1.6).

Covers :mod:`clinic_front_desk.dashboard.components.onboarding_wizard`:

- The wizard is presented on first access when no config exists (Req 1.1).
- Form parsing collects hours, location, services (with prep + price), accepted
  insurance, and providers (Req 1.2, 1.3).
- A valid submit is saved and reported successful (Req 1.4).
- A submit missing required fields is rejected, names each missing field, and
  retains the entered values (Req 1.5).
- A persistence failure is rejected with an error indication and retained values
  (Req 1.6).
- Rendering reflects retained values and per-field errors.
"""

from __future__ import annotations

from clinic_front_desk.config.validation import PREP_INSTRUCTIONS_MAX
from clinic_front_desk.dashboard.components.onboarding_wizard import (
    build_view_model,
    handle_submit,
    parse_form,
    render_html,
    rows_from_form,
    should_present_onboarding,
)
from clinic_front_desk.data_layer.faults import (
    FaultInjectingClinicKnowledgeBaseStore,
    FaultController,
    fail_on,
)
from clinic_front_desk.data_layer.memory.clinic_knowledge_base_store import (
    MemoryClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import ClinicKnowledgeBase, DayHours, is_ok


def _fixed_clock() -> str:
    return "2025-06-01T00:00:00+00:00"


def _valid_form() -> dict[str, str]:
    """A complete, valid onboarding submission."""
    return {
        "location": "123 Main St, Springfield",
        "hours[1].open": "09:00",
        "hours[1].close": "17:00",
        "hours[3].open": "09:00",
        "hours[3].close": "17:00",
        "services[0].name": "Hearing Test",
        "services[0].prep_instructions": "Arrive 10 minutes early.",
        "services[0].price": "120.00",
        "accepted_insurance": "Aetna, BlueCross",
        "providers[0].name": "Dr. Alice Smith",
        "providers[0].specialty": "ENT",
        "providers[0].days": "1,3",
        "providers[0].start": "09:00",
        "providers[0].end": "17:00",
    }


# ---------------------------------------------------------------------------
# Req 1.1 — present onboarding on first access when no config exists
# ---------------------------------------------------------------------------


def test_presents_onboarding_when_no_config() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    assert should_present_onboarding(store) is True

    view = build_view_model(store)
    assert view.present is True
    # Blank defaults: seven day rows, one empty service, one empty provider.
    assert len(view.hours) == 7
    assert len(view.services) == 1
    assert len(view.providers) == 1


def test_does_not_present_onboarding_once_configured() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    result = handle_submit(store, _valid_form(), clock=_fixed_clock)
    assert result.saved is True

    assert should_present_onboarding(store) is False
    assert build_view_model(store).present is False


def test_presents_onboarding_when_store_read_fails() -> None:
    controller = FaultController([fail_on("get")])
    store = FaultInjectingClinicKnowledgeBaseStore(
        MemoryClinicKnowledgeBaseStore(), controller
    )
    assert should_present_onboarding(store) is True


# ---------------------------------------------------------------------------
# Req 1.2, 1.3 — form parsing collects all fields
# ---------------------------------------------------------------------------


def test_parse_form_collects_all_fields() -> None:
    kb, parse_errors = parse_form(_valid_form())
    assert parse_errors == {}

    assert kb.location == "123 Main St, Springfield"
    assert kb.hours[1] == DayHours(open="09:00", close="17:00")
    assert kb.hours[0] is None  # Sunday not submitted -> closed
    assert kb.accepted_insurance == ["Aetna", "BlueCross"]

    assert len(kb.services) == 1
    svc = kb.services[0]
    assert svc.name == "Hearing Test"
    assert svc.prep_instructions == "Arrive 10 minutes early."
    assert svc.price == 120.00

    assert len(kb.providers) == 1
    prov = kb.providers[0]
    assert prov.name == "Dr. Alice Smith"
    assert prov.specialty == "ENT"
    assert prov.id  # a non-empty id was generated (Req 16.7)
    assert [r.day_of_week for r in prov.schedule] == [1, 3]


def test_parse_form_drops_empty_rows_and_flags_bad_price() -> None:
    form = {
        "location": "x",
        "services[0].name": "Cleaning",
        "services[0].price": "not-a-number",
        "services[1].name": "",
        "services[1].price": "",
        "providers[0].name": "",
    }
    kb, parse_errors = parse_form(form)
    # Empty service row and empty provider row are dropped.
    assert len(kb.services) == 1
    assert kb.providers == []
    # Unparseable price is flagged, value coerced to None.
    assert parse_errors == {"services[0].price": "price must be a number"}
    assert kb.services[0].price is None


# ---------------------------------------------------------------------------
# Req 1.4 — valid save succeeds and is persisted
# ---------------------------------------------------------------------------


def test_valid_submit_saves_and_reports_success() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    view = handle_submit(store, _valid_form(), clock=_fixed_clock)

    assert view.saved is True
    assert view.present is False
    assert view.errors == {}
    assert view.save_failed is False

    stored = store.get()
    assert is_ok(stored)
    assert stored.value is not None
    assert stored.value.configured is True
    assert stored.value.location == "123 Main St, Springfield"


# ---------------------------------------------------------------------------
# Req 1.5 — missing required fields rejected, named, values retained
# ---------------------------------------------------------------------------


def test_missing_required_fields_rejected_and_named() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    form = {
        "services[0].name": "Hearing Test",
        "services[0].price": "50",
        # no location, no hours, no providers
    }
    view = handle_submit(store, form)

    assert view.saved is False
    assert view.present is True
    # Each missing required field is identified (Req 1.5).
    assert set(view.missing_required_fields) == {"location", "hours", "providers"}
    assert "location" in view.errors
    assert "hours" in view.errors
    assert "providers" in view.errors
    # Nothing was persisted.
    assert store.get().value is None


def test_rejected_submit_retains_entered_values() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    form = {
        "services[0].name": "Hearing Test",
        "services[0].prep_instructions": "Bring your ID.",
        "services[0].price": "75.50",
        "accepted_insurance": "Aetna",
        # missing location / hours / providers
    }
    view = handle_submit(store, form)

    assert view.saved is False
    # Entered values are retained verbatim for re-render (Req 1.5).
    assert view.services[0].name == "Hearing Test"
    assert view.services[0].prep_instructions == "Bring your ID."
    assert view.services[0].price == "75.50"
    assert view.accepted_insurance == "Aetna"


def test_out_of_bounds_price_and_prep_produce_per_field_errors() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    form = _valid_form()
    form["services[0].price"] = "9999999.00"  # above MONEY_MAX
    form["services[0].prep_instructions"] = "x" * (PREP_INSTRUCTIONS_MAX + 1)

    view = handle_submit(store, form)

    assert view.saved is False
    assert "services[0].price" in view.errors
    assert "services[0].prep_instructions" in view.errors
    # The offending (out-of-bounds) value is retained.
    assert view.services[0].price == "9999999.00"


# ---------------------------------------------------------------------------
# Req 1.6 — persistence failure rejected with error + retained values
# ---------------------------------------------------------------------------


def test_persistence_failure_rejected_and_retains_values() -> None:
    controller = FaultController([fail_on("save", detail="disk offline")])
    store = FaultInjectingClinicKnowledgeBaseStore(
        MemoryClinicKnowledgeBaseStore(), controller
    )
    view = handle_submit(store, _valid_form(), clock=_fixed_clock)

    assert view.saved is False
    assert view.present is True
    assert view.save_failed is True
    assert view.message is not None and "disk offline" in view.message
    # Values retained so the doctor does not lose their work (Req 1.6).
    assert view.location == "123 Main St, Springfield"
    assert view.services[0].name == "Hearing Test"
    # No partial update applied.
    assert store.get().value is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_html_reflects_values_and_errors() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    form = {
        "services[0].name": "Hearing Test",
        "services[0].price": "75.50",
        # missing required fields -> errors
    }
    view = handle_submit(store, form)
    html = render_html(view)

    # Retained value present in the rendered form.
    assert 'value="75.50"' in html
    assert 'value="Hearing Test"' in html
    # Required-field summary present.
    assert "required" in html.lower()
    # Form shell + JS wired in.
    assert 'id="onboarding-wizard"' in html
    assert "onboarding_wizard.js" in html


def test_render_html_escapes_values() -> None:
    view = rows_from_form({"location": '<script>alert(1)</script>'})
    html = render_html(view)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_done_page_when_not_present() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    view = handle_submit(store, _valid_form(), clock=_fixed_clock)
    html = render_html(view)
    # Confirmation page rather than the form fields.
    assert "saved" in html.lower()


def test_prefills_from_existing_partial_config() -> None:
    store = MemoryClinicKnowledgeBaseStore()
    # A partially-entered, not-yet-configured config.
    store.save(
        ClinicKnowledgeBase(
            location="456 Oak Ave",
            hours={2: DayHours(open="08:00", close="12:00")},
            configured=False,
        )
    )
    view = build_view_model(store)
    assert view.present is True
    assert view.location == "456 Oak Ave"
    assert view.hours[2].open == "08:00"
