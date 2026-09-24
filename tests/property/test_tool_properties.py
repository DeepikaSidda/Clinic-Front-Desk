"""Property-based tests for the patient-facing Strands tool suite.

These validate design correctness Properties 3, 4, 5, 6, 9, and 11 against the
in-memory fake stores (fast, deterministic). Each property is implemented as a
single Hypothesis test running >=100 iterations and is tagged with a comment in
the design's required format.

Covered:
    - Property 3  (task 6.2)  — Offered-service matching (Req 2.1, 2.9)
    - Property 4  (task 6.3)  — Availability offers at most three dated slots
                                (Req 2.3, 2.7)
    - Property 5  (task 6.5)  — Booking round-trip and slot lifecycle
                                (Req 2.5, 2.6, 4.7, 4.8, 5.5, 5.7)
    - Property 6  (task 6.7)  — Patient lookup round-trip and disambiguation
                                convergence (Req 3.1, 3.2, 3.3, 3.4, 3.6)
    - Property 9  (task 6.9)  — FAQ pricing and information availability;
                                never fabricated (Req 6.3, 6.4, 6.5)
    - Property 11 (task 6.11) — Waitlist add round-trip and no active duplicates
                                (Req 7.1, 7.2, 7.5)
"""

from __future__ import annotations

import re
import string
from datetime import datetime, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryClinicKnowledgeBaseStore,
    MemoryPatientStore,
    MemoryWaitlistStore,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Duplicate,
    format_money,
    NotFound,
    NotOffered,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
    is_err,
    is_ok,
)
from clinic_front_desk.tools.appointments import book_appointment, cancel, reschedule
from clinic_front_desk.tools.availability import (
    DEFAULT_AVAILABILITY_LIMIT,
    check_availability,
)
from clinic_front_desk.tools.faq import answer_faq
from clinic_front_desk.tools.patients import create_patient, lookup_patient
from clinic_front_desk.tools.service_matcher import (
    match_offered_service,
    normalize_service_name,
)
from clinic_front_desk.tools.waitlist import add_to_waitlist

pytestmark = pytest.mark.property

# ---------------------------------------------------------------------------
# Shared strategies
# ---------------------------------------------------------------------------

_ident = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=8)
_service = st.text(alphabet=string.ascii_letters + string.digits, min_size=1, max_size=8)
_datetimes = st.datetimes(
    min_value=datetime(2024, 1, 1, 0, 0, 0),
    max_value=datetime(2027, 12, 31, 23, 59, 59),
)
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


def _iso(dt: datetime) -> str:
    """Format a naive datetime as an ISO-8601 ``...Z`` string."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ===========================================================================
# Property 3: Offered-service matching (Req 2.1, 2.9)
# ===========================================================================


@settings(max_examples=200)
@given(data=st.data())
def test_property_3_offered_service_matching(data: st.DataObject) -> None:
    # Feature: clinic-front-desk-agent, Property 3: For any set of offered
    # services and any spoken service name, the matcher resolves to an offered
    # service iff the name equals an offered service, and resolves to that exact
    # service; a name matching no offered service yields a not-offered result and
    # no service selection.
    # Validates: Requirements 2.1, 2.9
    #
    # "Equals" is judged on the name, not its transcription: speech recognition
    # lower-cases and re-spaces what the caller said, so comparison normalizes
    # case and whitespace (see normalize_service_name). Matching remains exact —
    # no substring, fuzzy, or synonym matching — and the value returned is always
    # the exact configured string.
    offered = data.draw(
        st.lists(_service, unique=True, max_size=6), label="offered_services"
    )
    # Draw a spoken name three ways: an offered name verbatim, an offered name
    # re-cased/re-spaced the way ASR would render it, or an arbitrary string.
    branch = data.draw(st.sampled_from(["exact", "transcribed", "arbitrary"]))
    if offered and branch == "exact":
        spoken = data.draw(st.sampled_from(offered), label="spoken")
    elif offered and branch == "transcribed":
        picked = data.draw(st.sampled_from(offered), label="picked")
        spoken = data.draw(
            st.sampled_from([picked.upper(), picked.lower(), f"  {picked}  "]),
            label="spoken",
        )
    else:
        spoken = data.draw(st.text(max_size=8), label="spoken")

    result = match_offered_service(spoken, offered)

    # First match wins, matching the matcher: it returns the first offered service
    # whose normalized name matches. A dict comprehension would keep the *last*,
    # which differs for a list like ["K", "k"] that Hypothesis happily generates
    # (two spellings of one service name — something config validation should
    # reject, but the matcher still has to be deterministic about).
    target = normalize_service_name(spoken)
    expected = next(
        (name for name in offered if normalize_service_name(name) == target), None
    )

    if expected is not None:
        # Resolves iff the named service is offered, to that exact configured
        # service string (not to whatever casing the caller's audio produced).
        assert is_ok(result)
        assert result.value == expected
        assert result.value in offered
    else:
        # No match -> not-offered, no service selected, name preserved verbatim.
        assert is_err(result)
        assert isinstance(result.error, NotOffered)
        assert result.error.named_service == spoken


# ===========================================================================
# Property 4: Availability offers at most three dated slots (Req 2.3, 2.7)
# ===========================================================================


@settings(max_examples=150)
@given(
    provider_id=_ident,
    service=_service,
    open_starts=st.lists(_datetimes, unique=True, max_size=7),
    other_starts=st.lists(_datetimes, unique=True, max_size=3),
    booked_starts=st.lists(_datetimes, unique=True, max_size=3),
)
def test_property_4_availability_offers_at_most_three(
    provider_id: str,
    service: str,
    open_starts: list[datetime],
    other_starts: list[datetime],
    booked_starts: list[datetime],
) -> None:
    # Feature: clinic-front-desk-agent, Property 4: For any list of open slots
    # for a requested service, the number of slots offered equals
    # min(3, number of open slots), and every offered slot carries a concrete
    # date and time; when the list is empty, no slot is offered and a waitlist
    # offer is produced.
    # Validates: Requirements 2.3, 2.7
    store = MemoryAppointmentStore()
    other_service = service + "_other"

    # Open, matching-service slots — the only ones that should be offered.
    for i, dt in enumerate(open_starts):
        store.seed_slot(
            Slot(
                id=f"open-{i}",
                provider_id=provider_id,
                service=service,
                start=_iso(dt),
                end=_iso(dt),
                status=SlotStatus.OPEN,
            )
        )
    # Open, published under a different service label. NOT distractors: one ENT
    # doctor takes whichever ENT service the caller needs in whatever half hour is
    # free, so this time is offerable too. The label is only whatever the last
    # publish of that day used — a slot's identity carries no service, so the data
    # cannot express a service-specific slot at all.
    for i, dt in enumerate(other_starts):
        store.seed_slot(
            Slot(
                id=f"other-{i}",
                provider_id=provider_id,
                service=other_service,
                start=_iso(dt),
                end=_iso(dt),
                status=SlotStatus.OPEN,
            )
        )
    # Distractors: matching service but already booked.
    for i, dt in enumerate(booked_starts):
        store.seed_slot(
            Slot(
                id=f"booked-{i}",
                provider_id=provider_id,
                service=service,
                start=_iso(dt),
                end=_iso(dt),
                status=SlotStatus.BOOKED,
            )
        )

    # This property is about *capping and dating* the offer, so both time filters
    # are pinned wide open: `from_date` in the far past and `now` before every
    # generated slot. Not-yet-started filtering is a separate rule with its own
    # test (see test_availability.py), and leaving `now` at the wall clock here
    # would make the expected count depend on today's date.
    result = check_availability(
        store,
        service=service,
        provider_ids=[provider_id],
        from_date="2000-01-01",
        now=datetime(2000, 1, 1, tzinfo=timezone.utc),
    )
    assert is_ok(result)
    offered = result.value

    # Offered count == min(3, distinct open minutes on this provider's calendar),
    # whatever label those slots carry. Booked ones never count.
    open_minutes = {_iso(dt) for dt in open_starts} | {_iso(dt) for dt in other_starts}
    assert len(offered) == min(DEFAULT_AVAILABILITY_LIMIT, len(open_minutes))

    if not open_minutes:
        # Empty -> no slot offered; the orchestrator turns [] into a waitlist
        # offer (Req 2.7).
        assert offered == []

    for slot in offered:
        # Every offer is open, belongs to this provider, and is presented as the
        # service the caller asked for — the internal label never reaches them.
        assert slot.service == service
        assert slot.status == SlotStatus.OPEN
        assert slot.provider_id == provider_id
        # Every offered slot carries a concrete date and time.
        assert _ISO_DATE_RE.match(slot.start[:10])
        assert _ISO_TIME_RE.match(slot.start[11:16])

    # One minute is never offered twice, however many labels it was published
    # under: two callers must not be handed the same time.
    assert len({slot.start for slot in offered}) == len(offered)


# ===========================================================================
# Property 5: Booking round-trip and slot lifecycle
# (Req 2.5, 2.6, 4.7, 4.8, 5.5, 5.7)
# ===========================================================================


@settings(max_examples=150)
@given(
    provider_id=_ident,
    patient_id=_ident,
    service=_service,
    start1=_datetimes,
    start2=_datetimes,
)
def test_property_5_booking_roundtrip_and_slot_lifecycle(
    provider_id: str,
    patient_id: str,
    service: str,
    start1: datetime,
    start2: datetime,
) -> None:
    # Feature: clinic-front-desk-agent, Property 5: For any patient, open slot,
    # and matched service, a confirmed booking creates an appointment retrievable
    # with the same patient, service, and slot, and the slot becomes booked.
    # Rescheduling to another open slot leaves the appointment on the new slot
    # (booked) with the previously held slot open. Cancelling removes it and
    # returns its slot to open.
    # Validates: Requirements 2.5, 2.6, 4.7, 4.8, 5.5, 5.7
    store = MemoryAppointmentStore()
    store.seed_slot(
        Slot(id="slot1", provider_id=provider_id, service=service,
             start=_iso(start1), end=_iso(start1), status=SlotStatus.OPEN)
    )
    store.seed_slot(
        Slot(id="slot2", provider_id=provider_id, service=service,
             start=_iso(start2), end=_iso(start2), status=SlotStatus.OPEN)
    )

    # --- book (Req 2.5, 2.6) ---
    booked = book_appointment(
        store,
        provider_id=provider_id,
        patient_id=patient_id,
        slot_id="slot1",
        service=service,
        appointment_id="appt1",
    )
    assert is_ok(booked)
    appt = booked.value.appointment
    assert appt.patient_id == patient_id
    assert appt.service == service
    assert appt.slot_id == "slot1"
    # Retrievable round-trip.
    stored = store.get("appt1")
    assert is_ok(stored) and stored.value is not None
    assert stored.value.patient_id == patient_id
    assert stored.value.slot_id == "slot1"
    # Slot booked.
    assert store.get_slot("slot1").unwrap().status == SlotStatus.BOOKED

    # --- reschedule (Req 4.7, 4.8) ---
    moved = reschedule(store, appointment_id="appt1", new_slot_id="slot2")
    assert is_ok(moved)
    assert moved.value.appointment.slot_id == "slot2"
    assert moved.value.released_slot_id == "slot1"
    assert store.get_slot("slot2").unwrap().status == SlotStatus.BOOKED
    assert store.get_slot("slot1").unwrap().status == SlotStatus.OPEN

    # --- cancel (Req 5.5, 5.7) ---
    cancelled = cancel(store, appointment_id="appt1")
    assert is_ok(cancelled)
    assert cancelled.value.released_slot_id == "slot2"
    assert store.get("appt1").unwrap() is None
    assert store.get_slot("slot2").unwrap().status == SlotStatus.OPEN


# ===========================================================================
# Property 6: Patient lookup round-trip and disambiguation convergence
# (Req 3.1, 3.2, 3.3, 3.4, 3.6)
# ===========================================================================

_NAME_POOL = ["Alice", "Bob", "Carol"]
_PHONE_POOL = ["555-0100", "555-0200"]


@settings(max_examples=150)
@given(
    specs=st.lists(
        st.tuples(st.sampled_from(_NAME_POOL), st.sampled_from(_PHONE_POOL)),
        max_size=8,
    ),
    data=st.data(),
)
def test_property_6_patient_lookup_and_disambiguation(
    specs: list[tuple[str, str]], data: st.DataObject
) -> None:
    # Feature: clinic-front-desk-agent, Property 6: For any patient store state,
    # lookup_patient(name, phone) returns exactly the records whose name and
    # callback phone match; a created patient is subsequently returned; a
    # name/phone with no record returns an empty set. For any candidate set with
    # more than one match, supplying an additional identifier returns a subset of
    # the prior candidates, so repeated disambiguation converges to at most one.
    # Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6
    store = MemoryPatientStore()
    # Create patients with unique ids and a unique "member" extra identifier.
    for i, (name, phone) in enumerate(specs):
        pid = f"p{i}"
        created = create_patient(
            store,
            name,
            phone,
            {"member": f"m{i}"},
            id_factory=(lambda pid=pid: pid),
            clock=(lambda: "2025-01-01T00:00:00Z"),
        )
        # A created patient is persisted and thus subsequently returned (Req 3.4).
        assert is_ok(created)

    # Exact-match property (Req 3.1): every present (name, phone) returns exactly
    # the ids that match, and a created record is among them (Req 3.4).
    present_pairs = sorted({pair for pair in specs})
    for name, phone in present_pairs:
        expected = {f"p{i}" for i, (n, p) in enumerate(specs) if n == name and p == phone}
        got = lookup_patient(store, name, phone)
        assert is_ok(got)
        assert {pt.id for pt in got.value} == expected

    # No matching record -> empty set (Req 3.3).
    absent = lookup_patient(store, "Nobody", "000-0000")
    assert is_ok(absent)
    assert absent.value == []

    # Disambiguation convergence (Req 3.6): narrowing by an extra identifier
    # returns a subset of the prior candidates and, since "member" is unique,
    # converges to exactly one.
    if present_pairs:
        name, phone = data.draw(st.sampled_from(present_pairs), label="query_pair")
        candidates = lookup_patient(store, name, phone).value
        candidate_ids = {pt.id for pt in candidates}
        target = data.draw(st.sampled_from(candidates), label="target")
        narrowed = lookup_patient(
            store, name, phone, {"member": target.extra_identifiers["member"]}
        )
        assert is_ok(narrowed)
        narrowed_ids = {pt.id for pt in narrowed.value}
        assert narrowed_ids <= candidate_ids  # subset of prior candidates
        assert target.id in narrowed_ids  # target retained
        assert len(narrowed_ids) == 1  # converges to at most one


# ===========================================================================
# Property 9: FAQ pricing and information availability (Req 6.3, 6.4, 6.5)
# ===========================================================================

_svc_name = st.text(alphabet=string.ascii_letters, min_size=1, max_size=8)
_price = st.one_of(
    st.none(),
    st.floats(min_value=0.01, max_value=999_999.99, allow_nan=False, allow_infinity=False),
)


@settings(max_examples=150)
@given(
    services=st.lists(
        st.tuples(_svc_name, _price),
        unique_by=lambda t: t[0].casefold(),
        max_size=6,
    ),
)
def test_property_9_faq_pricing_and_availability(
    services: list[tuple[str, float | None]],
) -> None:
    # Feature: clinic-front-desk-agent, Property 9: For any offered service with
    # a configured price, answer_faq("pricing", service) returns exactly that
    # configured price. For any topic or service absent from the
    # Clinic_Knowledge_Base, answer_faq returns an unavailable result rather than
    # a fabricated answer.
    # Validates: Requirements 6.3, 6.4, 6.5
    store = MemoryClinicKnowledgeBaseStore()
    kb = ClinicKnowledgeBase(
        location="123 Main St",
        hours={1: DayHours(open="09:00", close="17:00")},
        services=[ServiceConfig(name=name, price=price) for name, price in services],
        accepted_insurance=["Aetna"],
        providers=[Provider(id="p1", name="Dr. ENT", specialty="ENT")],
        configured=True,
    )
    assert is_ok(store.save(kb))

    for name, price in services:
        result = answer_faq(store, "pricing", service=name)
        if price is None:
            # Offered but no configured price -> unavailable, never fabricated.
            assert is_err(result)
            assert isinstance(result.error, NotFound)
        else:
            # Configured price -> returns exactly that price.
            assert is_ok(result)
            assert format_money(price) in result.value

    # A service absent from the knowledge base -> unavailable (Req 6.5), not
    # fabricated. The sentinel is longer than any generated name so it can never
    # collide.
    absent_result = answer_faq(store, "pricing", service="absent_service_sentinel_x")
    assert is_err(absent_result)
    assert isinstance(absent_result.error, NotOffered)

    # A topic absent from an unconfigured knowledge base -> unavailable (Req 6.3).
    empty = MemoryClinicKnowledgeBaseStore()
    empty_result = answer_faq(empty, "location")
    assert is_err(empty_result)


# ===========================================================================
# Property 11: Waitlist add round-trip and no active duplicates
# (Req 7.1, 7.2, 7.5)
# ===========================================================================

_PATIENT_POOL = ["pa", "pb"]
_SERVICE_POOL = ["s1", "s2"]
_SLOTTYPE_POOL = ["morning", "afternoon"]


@settings(max_examples=150)
@given(
    specs=st.lists(
        st.tuples(
            st.sampled_from(_PATIENT_POOL),
            st.sampled_from(_SERVICE_POOL),
            st.sampled_from(_SLOTTYPE_POOL),
        ),
        max_size=12,
    ),
)
def test_property_11_waitlist_add_roundtrip_no_duplicates(
    specs: list[tuple[str, str, str]],
) -> None:
    # Feature: clinic-front-desk-agent, Property 11: For any patient, service,
    # and preferred slot type, a successful add produces an entry retrievable
    # with those exact fields; a second add for the same (patient, service, slot
    # type) while an entry is active does not create a second entry, and the
    # active-entry count for that key remains one.
    # Validates: Requirements 7.1, 7.2, 7.5
    store = MemoryWaitlistStore()
    seen: set[tuple[str, str, str]] = set()

    for patient_id, service, slot_type in specs:
        key = (patient_id, service, slot_type)
        result = add_to_waitlist(
            store,
            patient_id=patient_id,
            service=service,
            preferred_slot_type=slot_type,
        )
        if key in seen:
            # Active duplicate is suppressed (Req 7.5).
            assert is_err(result)
            assert isinstance(result.error, Duplicate)
        else:
            # Round-trip with exact confirmation fields (Req 7.1, 7.2).
            assert is_ok(result)
            entry = result.value
            assert entry.patient_id == patient_id
            assert entry.service == service
            assert entry.preferred_slot_type == slot_type
            assert entry.active is True
            seen.add(key)

    # Exactly one active entry per distinct key.
    for patient_id, service, slot_type in seen:
        listed = store.list_by_service_ordered(service).unwrap()
        count = sum(
            1
            for e in listed
            if e.patient_id == patient_id
            and e.preferred_slot_type == slot_type
            and e.active
        )
        assert count == 1
