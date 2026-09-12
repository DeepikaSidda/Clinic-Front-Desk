"""Booking intake: the agent collects details, the doctor sees them.

Covers the whole path — register the patient with intake details, book a published
slot, see the patient's name on the calendar cell, open their record — plus the two
rules that keep it safe: a booking is never blocked on a health detail, and those
details are visible only to the doctor.
"""

from __future__ import annotations

import html
import re
from typing import Any

import pytest

from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.models import (
    CallOutcome,
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
    SlotStatus,
    is_ok,
)

pytestmark = pytest.mark.integration

starlette_testclient = pytest.importorskip("starlette.testclient")
TestClient = starlette_testclient.TestClient

DAY = "2026-09-10"
PROVIDER = "prov-raana"
SERVICE = "ENT Consultation"


@pytest.fixture
def app() -> Any:
    application = build_memory_application()
    application.stores.knowledge_base.save(
        ClinicKnowledgeBase(
            location="Renigunta Road, Tirupati",
            hours={d: DayHours(open="09:00", close="17:00") for d in range(1, 7)},
            services=[ServiceConfig(name=SERVICE, price=500.0)],
            providers=[
                Provider(id=PROVIDER, name="Dr. Kuppam Divya Raana", specialty="ENT")
            ],
            configured=True,
        )
    )
    return application


@pytest.fixture
def client(app: Any) -> Any:
    from clinic_front_desk.deployment.server import create_asgi_app

    return TestClient(create_asgi_app(app))


@pytest.fixture
def published(client: Any, app: Any) -> Any:
    client.post(
        "/slots?role=doctor",
        data={
            "day": DAY,
            "provider_id": PROVIDER,
            "service": SERVICE,
            "minutes": "30",
            "start": "09:00",
            "end": "11:00",
            "end_": "",
        },
    )
    return app


def _book(app: Any, **intake: Any) -> tuple[Any, Any]:
    """Register a patient with intake details and book the first open slot."""
    session = app.start_voice_session("intake-session")
    registered = session.toolset.register_patient(
        name=intake.pop("name", "Ravi Kumar"),
        callback_phone=intake.pop("callback_phone", "9876543210"),
        **intake,
    )
    assert registered.__class__.__name__ == "Ok", registered
    patient = registered.value

    slots = app.stores.appointments.list_open_slots(PROVIDER, SERVICE, DAY)
    slot = slots.value[0]
    booked = session.toolset.book_appointment(
        provider_id=PROVIDER,
        patient_id=patient.id,
        slot_id=slot.id,
        service=SERVICE,
    )
    assert booked.__class__.__name__ == "Ok", booked
    return patient, slot


# ---------------------------------------------------------------------------
# The agent collects intake
# ---------------------------------------------------------------------------


def test_register_patient_is_available_to_the_agent(app: Any) -> None:
    agent = app.new_voice_agent()

    assert "register_patient" in agent.tool_names


def test_intake_details_are_recorded_on_the_patient(published: Any) -> None:
    patient, _ = _book(
        published,
        age=34,
        blood_group="O positive",
        weight_kg=72.5,
        height_cm=175.0,
    )

    stored = published.stores.patients.get(patient.id)

    assert is_ok(stored)
    assert stored.value.age == 34
    assert stored.value.blood_group == "O+"
    assert stored.value.weight_kg == 72.5
    assert stored.value.height_cm == 175.0


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("O positive", "O+"),
        ("o pos", "O+"),
        ("A negative", "A-"),
        ("AB positive", "AB+"),
        ("b neg", "B-"),
        ("A +", "A+"),
        ("O+", "O+"),
    ],
)
def test_spoken_blood_groups_are_normalised(
    published: Any, spoken: str, expected: str
) -> None:
    # Speech-to-text renders these a dozen ways; a free-text blood group in a
    # record looks authoritative and cannot be relied on.
    patient, _ = _book(published, blood_group=spoken)

    assert published.stores.patients.get(patient.id).value.blood_group == expected


@pytest.mark.parametrize("spoken", ["eight positive", "purple", "", "AC+", "O"])
def test_an_unrecognised_blood_group_is_left_unset(published: Any, spoken: str) -> None:
    patient, _ = _book(published, blood_group=spoken)

    # Better absent than wrong: the doctor can ask.
    assert published.stores.patients.get(patient.id).value.blood_group is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("age", 320),
        ("age", -4),
        ("weight_kg", 720.0),
        ("weight_kg", 0.1),
        ("height_cm", 900.0),
        ("height_cm", 5.0),
    ],
)
def test_an_implausible_measurement_is_dropped(
    published: Any, field: str, value: Any
) -> None:
    # Far likelier a transcription slip than a real measurement, and a wrong
    # number in a patient record reads as fact.
    patient, _ = _book(published, **{field: value})

    assert getattr(published.stores.patients.get(patient.id).value, field) is None


def test_a_booking_succeeds_with_no_health_details_at_all(published: Any) -> None:
    # The rule that matters most: a caller who declines still gets an appointment.
    patient, slot = _book(published)

    stored = published.stores.patients.get(patient.id).value
    assert stored.name == "Ravi Kumar"
    assert stored.age is None
    assert stored.blood_group is None
    assert published.stores.appointments.get_slot(slot.id).value.status is SlotStatus.BOOKED


def test_a_returning_patient_is_not_duplicated(published: Any) -> None:
    first, _ = _book(published, age=34)
    session = published.start_voice_session("again")

    again = session.toolset.register_patient(
        name="Ravi Kumar", callback_phone="9876543210"
    )

    assert again.value.id == first.id
    # The second call gave no age; the record must keep the one it had.
    assert published.stores.patients.get(first.id).value.age == 34


# ---------------------------------------------------------------------------
# The booked slot shows who holds it
# ---------------------------------------------------------------------------


def test_a_booked_slot_shows_the_patient_name_on_the_calendar(
    client: Any, published: Any
) -> None:
    _book(published, age=34)

    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text

    assert "Ravi Kumar" in page
    assert 'data-status="booked"' in page


def test_a_booked_slot_links_to_the_patient_instead_of_a_block_button(
    client: Any, published: Any
) -> None:
    patient, _ = _book(published)

    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text

    assert f"/slots/patient/{patient.id}" in html.unescape(page)
    assert "View patient" in page


def test_the_patient_link_from_the_calendar_works(client: Any, published: Any) -> None:
    _book(published)
    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text
    link = html.unescape(
        next(u for u in re.findall(r'href="(/slots/patient/[^"]*)"', page))
    )

    response = client.get(link)

    assert response.status_code == 200
    assert "Ravi Kumar" in response.text


# ---------------------------------------------------------------------------
# What the doctor sees
# ---------------------------------------------------------------------------


def test_the_doctor_sees_the_full_intake_record(client: Any, published: Any) -> None:
    patient, _ = _book(
        published, age=34, blood_group="O positive", weight_kg=72.5, height_cm=175.0
    )

    page = client.get(
        f"/slots/patient/{patient.id}?role=doctor&day={DAY}&provider_id={PROVIDER}"
    ).text

    assert "Ravi Kumar" in page
    assert "9876543210" in page
    assert "34" in page
    assert "O+" in page
    assert "72.5 kg" in page
    assert "175 cm" in page


def test_bmi_is_derived_but_not_interpreted(client: Any, published: Any) -> None:
    patient, _ = _book(published, weight_kg=72.5, height_cm=175.0)

    page = client.get(f"/slots/patient/{patient.id}?role=doctor").text

    # 72.5 / 1.75^2 = 23.7 — the arithmetic is a convenience; what it *means* is
    # the doctor's judgment, so no label like "normal" appears.
    assert "23.7" in page
    for verdict in ("normal", "overweight", "obese", "underweight", "healthy"):
        assert verdict not in page.lower()


def test_the_record_says_the_details_were_self_reported(
    client: Any, published: Any
) -> None:
    patient, _ = _book(published, weight_kg=72.5)

    page = client.get(f"/slots/patient/{patient.id}?role=doctor").text

    # A value taken over the phone is what the caller said, not a measurement.
    assert "As given by the caller" in page
    assert "Not measured at the clinic" in page


def test_a_record_with_no_intake_details_says_so_plainly(
    client: Any, published: Any
) -> None:
    patient, _ = _book(published)

    page = client.get(f"/slots/patient/{patient.id}?role=doctor").text

    assert "did not give any of these" in page
    assert "never held up" in page


def test_the_appointment_appears_on_the_patient_record(
    client: Any, published: Any
) -> None:
    patient, _ = _book(published)

    page = client.get(
        f"/slots/patient/{patient.id}?role=doctor&day={DAY}&provider_id={PROVIDER}"
    ).text

    assert "09:00" in page
    assert SERVICE in page


def test_an_unknown_patient_renders_a_not_found_state(client: Any) -> None:
    response = client.get("/slots/patient/nobody?role=doctor")

    assert response.status_code == 200
    assert "No record found" in response.text


def test_the_page_asks_not_to_be_indexed(client: Any, published: Any) -> None:
    patient, _ = _book(published)

    page = client.get(f"/slots/patient/{patient.id}?role=doctor").text

    assert 'name="robots"' in page
    assert "noindex" in page


# ---------------------------------------------------------------------------
# Access: health details are doctor-only
# ---------------------------------------------------------------------------


def test_the_assistant_cannot_open_a_patient_record(client: Any, published: Any) -> None:
    patient, _ = _book(published, blood_group="O positive")

    response = client.get(f"/slots/patient/{patient.id}?role=assistant")

    # Knowing the 09:00 slot is taken is front-desk work; reading that patient's
    # blood group is not.
    assert response.status_code == 403


def test_a_viewer_with_no_role_cannot_open_a_patient_record(
    client: Any, published: Any
) -> None:
    patient, _ = _book(published)

    assert client.get(f"/slots/patient/{patient.id}").status_code == 403


def test_the_assistant_still_sees_the_day_and_who_holds_each_slot(
    client: Any, published: Any
) -> None:
    _book(published)

    page = client.get(f"/slots?role=assistant&day={DAY}&provider_id={PROVIDER}").text

    # They run the diary, so the name stays; only the record behind it is gated.
    assert "Ravi Kumar" in page


def test_health_details_never_appear_on_the_calendar(client: Any, published: Any) -> None:
    _book(published, blood_group="O positive", weight_kg=72.5, height_cm=175.0)

    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text

    assert "O+" not in page
    assert "72.5" not in page
    assert "175" not in page


def test_a_patient_name_with_markup_is_escaped(client: Any, published: Any) -> None:
    patient, _ = _book(published, name="<script>alert(1)</script>")

    calendar = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text
    detail = client.get(f"/slots/patient/{patient.id}?role=doctor").text

    assert "<script>alert(1)</script>" not in calendar
    assert "<script>alert(1)</script>" not in detail


# ---------------------------------------------------------------------------
# Details offered AFTER the booking must actually be stored
# ---------------------------------------------------------------------------
#
# Observed on a live call. The caller booked on name and mobile alone, then
# offered her blood group, height and weight. Registration found her record,
# wrote nothing, and returned ok — so the agent told her: "I've added your blood
# group (B positive), height (154 centimeters), and weight (58 kilograms) to your
# clinic records." The stored record held None for all three.
#
# Telling a patient her medical details are on file when they are not is worse
# than never asking: she has no reason to repeat herself, and the doctor reads the
# empty fields as a refusal.


def test_details_given_after_booking_are_actually_stored(published: Any) -> None:
    first, _ = _book(published)
    assert published.stores.patients.get(first.id).value.blood_group is None

    session = published.start_voice_session("after-booking")
    again = session.toolset.register_patient(
        name="Ravi Kumar",
        callback_phone="9876543210",
        blood_group="B positive",
        height_cm=154.0,
        weight_kg=58.0,
    )

    assert again.__class__.__name__ == "Ok"
    stored = published.stores.patients.get(first.id).value
    assert stored.blood_group == "B+"
    assert stored.height_cm == 154.0
    assert stored.weight_kg == 58.0
    # Same record, not a duplicate.
    assert again.value.id == first.id


def test_the_tool_reports_exactly_which_fields_it_wrote(published: Any) -> None:
    """The agent may only confirm what this list contains."""
    _book(published)
    agent = published.new_voice_agent()
    tool = agent.tools["register_patient"]

    payload = tool(
        name="Ravi Kumar",
        callback_phone="9876543210",
        blood_group="B positive",
        weight_kg=58.0,
    )

    assert payload["ok"] is True
    assert set(payload["recorded"]) == {"blood_group", "weight_kg"}
    assert payload["rejected"] == []


def test_an_implausible_value_is_reported_as_rejected_not_recorded(
    published: Any,
) -> None:
    """The caller said 188 kg when she meant 58. It must not be silently dropped."""
    _book(published)
    agent = published.new_voice_agent()
    tool = agent.tools["register_patient"]

    payload = tool(
        name="Ravi Kumar",
        callback_phone="9876543210",
        weight_kg=880.0,
        blood_group="B positive",
    )

    assert payload["ok"] is True
    assert "weight_kg" in payload["rejected"]
    assert "weight_kg" not in payload["recorded"]
    assert published.stores.patients.get(payload["value"].id).value.weight_kg is None


def test_a_value_already_on_file_is_not_overwritten_and_is_reported(
    published: Any,
) -> None:
    """The person on the phone may not be the person whose record it is."""
    first, _ = _book(published, age=34)
    agent = published.new_voice_agent()
    tool = agent.tools["register_patient"]

    payload = tool(name="Ravi Kumar", callback_phone="9876543210", age=71)

    assert payload["ok"] is True
    assert "age" in payload["already_on_file"]
    assert "age" not in payload["recorded"]
    assert published.stores.patients.get(first.id).value.age == 34


def test_nothing_offered_means_nothing_reported_as_recorded(published: Any) -> None:
    """A plain re-registration must not look like a successful save."""
    _book(published, age=34)
    agent = published.new_voice_agent()
    tool = agent.tools["register_patient"]

    payload = tool(name="Ravi Kumar", callback_phone="9876543210")

    assert payload["ok"] is True
    assert payload["recorded"] == []
    assert payload["already_on_file"] == []
    assert payload["rejected"] == []


def test_a_new_record_reports_what_it_kept(published: Any) -> None:
    agent = published.new_voice_agent()
    tool = agent.tools["register_patient"]

    payload = tool(
        name="Nita Shah",
        callback_phone="9000000001",
        blood_group="not a blood group",
        height_cm=154.0,
    )

    assert payload["ok"] is True
    assert payload["recorded"] == ["height_cm"]
    assert payload["rejected"] == ["blood_group"]


def test_a_failed_write_never_reports_a_save(published: Any) -> None:
    """If the store refuses, the caller must not be told the detail is on file."""
    from clinic_front_desk.data_layer.faults import fail_on, wrap
    from clinic_front_desk.tools.patients import record_intake

    first, _ = _book(published)
    existing = published.stores.patients.get(first.id).value
    faulty = wrap(published.stores.patients, fail_on("update"))

    outcome = record_intake(faulty, existing, blood_group="B positive")

    assert outcome.__class__.__name__ == "Err"
    assert published.stores.patients.get(first.id).value.blood_group is None


# ---------------------------------------------------------------------------
# A completed booking must not be filed as an abandoned call
# ---------------------------------------------------------------------------
#
# Checked against the live table after a real call: the agent booked
# 2026-09-11 13:00 and read the reference back to the caller, and the Call_Session
# was persisted with outcome `interrupted`. Only the orchestrated book/cancel/
# reschedule chains recorded an outcome, and on a real call the model calls the
# tools directly. So every appointment the agent actually took looked like a
# dropped call, and the doctor's booking count stayed at zero.


def test_a_model_driven_booking_records_the_booked_outcome(published: Any) -> None:
    session = published.start_voice_session("outcome-booked")
    patient = session.toolset.register_patient(
        name="Ravi Kumar", callback_phone="9876543210"
    ).value
    slot = published.stores.appointments.list_open_slots(
        PROVIDER, SERVICE, DAY
    ).value[0]

    booked = session.toolset.book_appointment(
        provider_id=PROVIDER,
        patient_id=patient.id,
        slot_id=slot.id,
        service=SERVICE,
    )

    assert booked.__class__.__name__ == "Ok"
    assert session.context.outcome is CallOutcome.BOOKED


def test_the_booked_outcome_is_what_gets_persisted(published: Any) -> None:
    """The doctor's activity log has to agree with what happened."""
    session = published.start_voice_session("outcome-persisted")
    patient = session.toolset.register_patient(
        name="Ravi Kumar", callback_phone="9876543210"
    ).value
    slot = published.stores.appointments.list_open_slots(
        PROVIDER, SERVICE, DAY
    ).value[0]
    session.toolset.book_appointment(
        provider_id=PROVIDER,
        patient_id=patient.id,
        slot_id=slot.id,
        service=SERVICE,
    )

    # Exactly what the server does when the socket closes: the recorded outcome
    # if there is one, INTERRUPTED only as the fallback. An explicit argument
    # deliberately outranks the context (that is how genuine stream loss is
    # reported), so what makes this correct is that the context now HAS an
    # outcome to pass — before the fix it was None and every booking fell through
    # to INTERRUPTED.
    recorded = session.context.outcome
    result = session.finalize(recorded or CallOutcome.INTERRUPTED)

    assert result.__class__.__name__ == "Ok"
    assert result.value.outcome is CallOutcome.BOOKED
    stored = next(
        s
        for s in published.stores.call_sessions.list_recent(20).value
        if s.id == "outcome-persisted"
    )
    assert stored.outcome is CallOutcome.BOOKED


def test_a_cancellation_records_the_cancelled_outcome(published: Any) -> None:
    session = published.start_voice_session("outcome-cancelled")
    patient = session.toolset.register_patient(
        name="Ravi Kumar", callback_phone="9876543210"
    ).value
    slot = published.stores.appointments.list_open_slots(
        PROVIDER, SERVICE, DAY
    ).value[0]
    booked = session.toolset.book_appointment(
        provider_id=PROVIDER,
        patient_id=patient.id,
        slot_id=slot.id,
        service=SERVICE,
    )

    session.toolset.cancel(appointment_id=booked.value.appointment.id)

    # The later task wins: the caller booked and then cancelled.
    assert session.context.outcome is CallOutcome.CANCELLED


def test_a_failed_booking_records_no_outcome(published: Any) -> None:
    """An outcome is a claim about the call; a failure must not make one."""
    session = published.start_voice_session("outcome-failed")

    failed = session.toolset.book_appointment(
        provider_id=PROVIDER,
        patient_id="no-such-patient",
        slot_id="no-such-slot",
        service=SERVICE,
    )

    assert failed.__class__.__name__ == "Err"
    assert session.context.outcome is None


# ---------------------------------------------------------------------------
# The doctor can correct a misheard record
# ---------------------------------------------------------------------------
#
# Observed live: a caller who said "Sidda Deepika" was recorded as "siddha
# devika". Speech recognition on proper nouns cannot be made reliable, so the
# answer is not to prevent the mistake but to make it correctable — and until this
# form existed the patient page was read-only, so a misheard name was permanent
# and would never match the ID the patient brings to reception.


def _edit(client: Any, patient_id: str, **fields: str) -> Any:
    form = {"name": "Sidda Deepika", "callback_phone": "9502285901"}
    form.update(fields)
    return client.post(f"/slots/patient/{patient_id}?role=doctor", data=form)


def test_the_doctor_can_correct_a_misheard_name(client: Any, published: Any) -> None:
    patient, _ = _book(published, name="siddha devika", callback_phone="9502285901")

    response = _edit(client, patient.id)

    assert response.status_code == 200
    assert published.stores.patients.get(patient.id).value.name == "Sidda Deepika"


def test_the_corrected_name_reaches_the_calendar(client: Any, published: Any) -> None:
    """Names are resolved live, so the fix propagates without touching the slot."""
    patient, _ = _book(published, name="siddha devika", callback_phone="9502285901")

    _edit(client, patient.id)

    page = client.get(f"/slots?role=doctor&day={DAY}&provider_id={PROVIDER}").text
    assert "Sidda Deepika" in page
    assert "siddha devika" not in page


def test_the_edit_form_is_prefilled_with_what_is_stored(
    client: Any, published: Any
) -> None:
    """It has to read as a correction, not a blank re-entry."""
    patient, _ = _book(published, name="siddha devika", blood_group="B positive")

    page = client.get(f"/slots/patient/{patient.id}?role=doctor").text

    assert 'value="siddha devika"' in page
    assert 'name="blood_group"' in page
    assert '<option value="B+" selected>' in page


def test_the_assistant_cannot_edit_a_patient(client: Any, published: Any) -> None:
    """The front desk cannot read a blood group, so it cannot rewrite one."""
    patient, _ = _book(published)

    response = client.post(
        f"/slots/patient/{patient.id}?role=assistant",
        data={"name": "Someone Else", "callback_phone": "9000000000"},
    )

    assert response.status_code == 403
    assert published.stores.patients.get(patient.id).value.name == "Ravi Kumar"


def test_an_implausible_edit_is_refused_with_a_reason(
    client: Any, published: Any
) -> None:
    """On a form the doctor can fix it, so silence would be the wrong answer.

    Deliberately unlike the phone path, where an implausible measurement is
    dropped without comment because the agent cannot hold a reliable clarifying
    dialogue.
    """
    patient, _ = _book(published, weight_kg=58.0)

    response = _edit(client, patient.id, weight_kg="880")

    assert response.status_code == 200
    assert "outside the plausible range" in response.text
    # Nothing was written, including the name that was valid.
    stored = published.stores.patients.get(patient.id).value
    assert stored.weight_kg == 58.0
    assert stored.name == "Ravi Kumar"


def test_a_blank_name_is_refused(client: Any, published: Any) -> None:
    patient, _ = _book(published)

    response = _edit(client, patient.id, name="   ")

    assert response.status_code == 200
    assert "name is required" in response.text
    assert published.stores.patients.get(patient.id).value.name == "Ravi Kumar"


def test_clearing_a_box_clears_the_record(client: Any, published: Any) -> None:
    """A form that shows its contents should mean what it shows."""
    patient, _ = _book(published, weight_kg=58.0, blood_group="B positive")

    _edit(client, patient.id, weight_kg="", blood_group="")

    stored = published.stores.patients.get(patient.id).value
    assert stored.weight_kg is None
    assert stored.blood_group is None


def test_an_unchanged_submission_says_so_rather_than_claiming_a_save(
    client: Any, published: Any
) -> None:
    patient, _ = _book(published)

    response = client.post(
        f"/slots/patient/{patient.id}?role=doctor",
        data={"name": "Ravi Kumar", "callback_phone": "9876543210"},
    )

    assert "Nothing to change" in response.text


def test_the_edit_reports_which_fields_changed(client: Any, published: Any) -> None:
    patient, _ = _book(published)

    response = _edit(client, patient.id, age="31")

    assert "Updated name, mobile, age." in response.text


def test_an_unrecognised_blood_group_is_refused(client: Any, published: Any) -> None:
    patient, _ = _book(published)

    response = _edit(client, patient.id, blood_group="Z minus")

    assert "not a blood group this clinic records" in response.text
    assert published.stores.patients.get(patient.id).value.blood_group is None


def test_a_patient_supplied_name_cannot_break_out_of_the_form(
    client: Any, published: Any
) -> None:
    """The name reaches an HTML attribute, so it must be escaped."""
    patient, _ = _book(published)
    _edit(client, patient.id, name='Ravi" autofocus onfocus="alert(1)')

    page = client.get(f"/slots/patient/{patient.id}?role=doctor").text

    assert 'onfocus="alert(1)"' not in page
    assert "&quot;" in page


# ---------------------------------------------------------------------------
# A caller who cannot name a service is offered the consultation
# ---------------------------------------------------------------------------
#
# End to end through a real VoiceSession, because the unit tests exercise a clinic
# whose services do not include "ENT Consultation" and therefore take the safe
# fallback. This clinic offers it, so this is the path a live caller hits.


def test_a_symptom_offers_the_consultation_and_records_no_escalation(
    published: Any,
) -> None:
    """The live failure: she described itching and was offered a human instead."""
    session = published.start_voice_session("unsure-caller")

    decision = session.apply_guardrail(
        "there is some kind of itching inside my nose, which service should i prefer"
    )

    assert decision is not None
    assert decision.offer_general_consultation == "ENT Consultation"
    assert decision.requires_escalation is False
    # No service chosen for her, and no handover filed.
    assert decision.selected_service is None
    assert session.context.requested_service is None
    assert published.stores.escalations.list_recent(10).value == []


def test_accepting_the_consultation_is_the_caller_naming_it(published: Any) -> None:
    """Req 10.2 holds: the service is selected only once she says it herself."""
    session = published.start_voice_session("unsure-then-accepts")
    session.apply_guardrail("my nose is itching, which service should i prefer")

    decision = session.apply_guardrail("okay book the ENT Consultation then")

    assert decision is not None
    assert decision.selected_service == "ENT Consultation"
    assert session.context.requested_service == "ENT Consultation"
    assert decision.requires_escalation is False


def test_a_clinical_question_still_escalates_on_this_clinic(published: Any) -> None:
    """The boundary that must not move: triage is still refused and handed over."""
    session = published.start_voice_session("still-clinical")

    decision = session.apply_guardrail("my nose is itching, is this serious?")

    assert decision is not None
    assert decision.requires_escalation is True
    assert decision.offer_general_consultation is None
    assert len(published.stores.escalations.list_recent(10).value) == 1


def test_an_emergency_still_escalates_immediately(published: Any) -> None:
    session = published.start_voice_session("emergency")

    decision = session.apply_guardrail("i cant breathe and my throat is swollen")

    assert decision is not None
    assert decision.requires_escalation is True
    assert decision.offer_general_consultation is None


# ---------------------------------------------------------------------------
# Finding the caller's booking so it can be moved or cancelled
# ---------------------------------------------------------------------------
#
# Observed live. A caller said "I booked a slot on 12 September, 9 AM, my name is
# Lakshmi Prasad, I'd like to move it to 11 AM", gave her mobile number, and spelled
# her name out twice. The agent found her patient record and still could not find
# the booking, because there was no tool that could: reschedule and cancel both take
# an appointment id, and nothing produced one from a name and a number.
#
# So it asked her for an "appointment reference number" — which no caller has — and
# offered to "pull up your full appointment history", which it had no way to do.


def test_a_caller_can_find_their_own_booking(published: Any) -> None:
    patient, slot = _book(published)
    session = published.start_voice_session("find-mine")

    found = session.toolset.list_appointments(
        name="Ravi Kumar", callback_phone="9876543210"
    )

    assert found.__class__.__name__ == "Ok"
    assert [a.slot_id for a in found.value] == [slot.id]
    # The id reschedule and cancel need is right there.
    assert found.value[0].id


def test_the_booking_is_found_from_a_spoken_name(published: Any) -> None:
    """Speech-to-text lower-cases everything, which is how this broke."""
    _book(published)
    session = published.start_voice_session("find-spoken")

    found = session.toolset.list_appointments(
        name="ravi kumar", callback_phone="+91 98765 43210"
    )

    assert found.__class__.__name__ == "Ok"
    assert len(found.value) == 1


def test_it_can_actually_be_rescheduled_end_to_end(published: Any) -> None:
    """The whole point: from a name and a number to a moved appointment."""
    _book(published)
    session = published.start_voice_session("move-mine")

    listed = session.toolset.list_appointments(
        name="Ravi Kumar", callback_phone="9876543210"
    )
    appointment = listed.value[0]
    open_slots = published.stores.appointments.list_open_slots(PROVIDER, SERVICE, DAY)
    target = open_slots.value[0]

    moved = session.toolset.reschedule(
        appointment_id=appointment.id, new_slot_id=target.id
    )

    assert moved.__class__.__name__ == "Ok"
    assert published.stores.appointments.get(appointment.id).value.slot_id == target.id
    assert published.stores.appointments.get_slot(target.id).value.status is (
        SlotStatus.BOOKED
    )


def test_a_caller_with_nothing_booked_gets_an_empty_list_not_an_error(
    published: Any,
) -> None:
    """So the agent says "nothing booked" rather than demanding proof."""
    published.stores.patients.create(
        __import__(
            "clinic_front_desk.models", fromlist=["Patient"]
        ).Patient(id="p-none", name="Nobody Here", callback_phone="9000000009")
    )
    session = published.start_voice_session("nothing-booked")

    found = session.toolset.list_appointments(
        name="Nobody Here", callback_phone="9000000009"
    )

    assert found.__class__.__name__ == "Ok"
    assert found.value == []


def test_an_unknown_caller_is_reported_as_not_found(published: Any) -> None:
    session = published.start_voice_session("unknown-caller")

    found = session.toolset.list_appointments(
        name="Someone Else", callback_phone="9000000001"
    )

    assert found.__class__.__name__ == "Err"


def test_a_cancelled_appointment_is_not_offered_as_movable(published: Any) -> None:
    """Offering it wastes the caller's time on a request that must fail."""
    _book(published)
    session = published.start_voice_session("after-cancel")
    listed = session.toolset.list_appointments(
        name="Ravi Kumar", callback_phone="9876543210"
    )
    session.toolset.cancel(appointment_id=listed.value[0].id)

    again = session.toolset.list_appointments(
        name="Ravi Kumar", callback_phone="9876543210"
    )

    assert again.value == []
