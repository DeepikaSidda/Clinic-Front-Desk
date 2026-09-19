"""The closed-day fact must reach the model, not just exist in a helper.

``check_availability`` searches on or after the requested date, so a caller asking
for Sunday 13 September gets Monday's slots back. Correct times, no reason — and the
agent told the caller it could not find anything for the thirteenth, which sounds
like a fully booked day rather than a closed one.

These tests assert on the payload the Strands tool actually returns, because that
dict is the entire channel between the calendar and what the agent says. A helper
that computes the right answer while the payload omits it fixes nothing.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryCallSessionStore,
    MemoryClinicKnowledgeBaseStore,
    MemoryEscalationStore,
    MemoryPatientStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
)
from clinic_front_desk.voice.agent import (
    VoiceFrontDeskStores,
    build_patient_facing_tools,
)

PROVIDER = "prov-raana"
SERVICE = "ENT Consultation"

#: Sunday = 0. The live clinic: open Monday to Saturday, shut Sunday.
MON_TO_SAT = (1, 2, 3, 4, 5, 6)


def _next_sunday() -> date:
    """The next Sunday strictly in the future.

    Dates here are computed rather than written down. The first version of this file
    hardcoded 13 September as the Sunday and seeded slots on the 14th and 15th; it
    passed the day it was written and failed a week later, because
    ``check_availability`` correctly refuses to offer slots that have already
    started. A test that only passes during one week is not a test.
    """
    today = datetime.now(UTC).date()
    # Python's weekday(): Monday = 0 ... Sunday = 6.
    ahead = (6 - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


#: A Sunday the clinic is shut, and the two working days straight after it.
CLOSED_SUNDAY = _next_sunday().isoformat()
OPEN_MONDAY = (_next_sunday() + timedelta(days=1)).isoformat()
OPEN_TUESDAY = (_next_sunday() + timedelta(days=2)).isoformat()


def _knowledge_base(open_days: tuple[int, ...]) -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location="Tirupati",
        hours={
            day: (DayHours(open="09:00", close="20:00") if day in open_days else None)
            for day in range(7)
        },
        services=[ServiceConfig(name=SERVICE)],
        providers=[Provider(id=PROVIDER, name="Dr Raana", specialty="ENT")],
    )


def _slot(slot_id: str, start: str) -> Slot:
    return Slot(
        id=slot_id,
        provider_id=PROVIDER,
        service=SERVICE,
        start=start,
        end=start[:11] + "09:30",
        status=SlotStatus.OPEN,
    )


def _tools(open_days: tuple[int, ...] = MON_TO_SAT) -> dict[str, Any]:
    appointments = MemoryAppointmentStore()
    # The Monday after is published; the Sunday is not, and never will be.
    appointments.seed_slots(
        [
            _slot("mon-0900", f"{OPEN_MONDAY}T09:00"),
            _slot("mon-0930", f"{OPEN_MONDAY}T09:30"),
            _slot("tue-0900", f"{OPEN_TUESDAY}T09:00"),
        ]
    )
    knowledge_base = MemoryClinicKnowledgeBaseStore()
    knowledge_base.save(_knowledge_base(open_days))
    stores = VoiceFrontDeskStores(
        appointments=appointments,
        patients=MemoryPatientStore(),
        waitlist=MemoryWaitlistStore(),
        escalations=MemoryEscalationStore(),
        knowledge_base=knowledge_base,
        call_sessions=MemoryCallSessionStore(),
    )
    return build_patient_facing_tools(stores)


def _availability(tools: dict[str, Any], from_date: str) -> dict[str, Any]:
    """Invoke the tool the way the model does, and return its payload."""
    tool = tools["check_availability"]
    func = getattr(tool, "__wrapped__", None) or getattr(tool, "func", None) or tool
    result = func(service=SERVICE, from_date=from_date, provider_id=PROVIDER)
    assert isinstance(result, dict)
    return result


# -- the live failure ------------------------------------------------------


def test_asking_for_a_closed_sunday_flags_it_in_the_payload() -> None:
    payload = _availability(_tools(), CLOSED_SUNDAY)
    assert payload["clinic_closed_on_requested_date"] is True
    assert payload["requested_weekday"] == "Sunday"
    assert payload["requested_date"] == CLOSED_SUNDAY


def test_the_notice_names_the_day_so_the_agent_can_say_it() -> None:
    payload = _availability(_tools(), CLOSED_SUNDAY)
    notice = payload["closed_notice"]
    assert "Sunday" in notice
    assert CLOSED_SUNDAY in notice
    assert "closed" in notice.lower()


def test_the_notice_calls_it_the_clinic_holiday() -> None:
    """The doctor's own words: "that day is Sunday, our clinic holiday"."""
    notice = _availability(_tools(), CLOSED_SUNDAY)["closed_notice"]
    assert "holiday" in notice.lower()


def test_the_notice_tells_the_agent_to_lead_with_it() -> None:
    """Order matters. Offering Monday first buries the reason they cannot have Sunday."""
    notice = _availability(_tools(), CLOSED_SUNDAY)["closed_notice"]
    assert "before offering" in notice.lower()


def test_slots_are_still_offered_so_the_caller_gets_an_alternative() -> None:
    """Telling them it is shut is only half an answer; offer the next working day."""
    payload = _availability(_tools(), CLOSED_SUNDAY)
    assert payload["ok"] is True
    assert payload["value"], "the caller should still be offered later dates"
    assert all(slot.start[:10] != CLOSED_SUNDAY for slot in payload["value"])


# -- it must stay quiet when the day is open -------------------------------


def test_an_open_day_carries_no_closure_flag() -> None:
    payload = _availability(_tools(), OPEN_TUESDAY)
    assert "clinic_closed_on_requested_date" not in payload
    assert "closed_notice" not in payload


def test_a_clinic_open_on_sunday_carries_no_closure_flag() -> None:
    """The flag follows configured hours, not the calendar's idea of a weekend."""
    payload = _availability(_tools(open_days=(0, 1, 2, 3, 4, 5, 6)), CLOSED_SUNDAY)
    assert "clinic_closed_on_requested_date" not in payload


def test_no_requested_date_carries_no_closure_flag() -> None:
    """"Any time soon" is not a request for a closed day."""
    tools = _tools()
    tool = tools["check_availability"]
    func = getattr(tool, "__wrapped__", None) or getattr(tool, "func", None) or tool
    payload = func(service=SERVICE, provider_id=PROVIDER)
    assert "clinic_closed_on_requested_date" not in payload
