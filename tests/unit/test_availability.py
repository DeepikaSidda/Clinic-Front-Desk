"""Unit tests for the offered-service matcher and ``check_availability`` tool.

Covers task 6.1: exact offered-service matching (Req 2.1, 2.9) and
``check_availability`` open-slot retrieval with the default limit of 3, dated
slots, empty availability, and the retrieval-failure path (Req 2.2, 2.3, 2.10,
4.4). These are example/edge unit tests; the universal properties are covered
separately by the Hypothesis property tests (tasks 6.2, 6.3).
"""

from __future__ import annotations

from datetime import datetime, timezone

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import MemoryAppointmentStore
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    Err,
    NotOffered,
    Ok,
    ServiceConfig,
    Slot,
    SlotStatus,
    StoreFailure,
)
from clinic_front_desk.tools.availability import (
    DEFAULT_AVAILABILITY_LIMIT,
    check_availability,
)
from clinic_front_desk.tools.service_matcher import (
    match_offered_service,
    offered_service_names,
)

PROVIDER = "prov-ent-1"
SERVICE = "hearing test"


def _slot(slot_id: str, start: str, service: str = SERVICE, status: SlotStatus = SlotStatus.OPEN) -> Slot:
    return Slot(
        id=slot_id,
        provider_id=PROVIDER,
        service=service,
        start=start,
        end=start,
        status=status,
    )


# --- offered-service matcher (Req 2.1, 2.9) --------------------------------


def test_match_returns_exact_offered_service() -> None:
    result = match_offered_service("hearing test", ["hearing test", "sinus consult"])
    assert isinstance(result, Ok)
    assert result.value == "hearing test"


def test_match_unknown_service_is_not_offered_with_no_selection() -> None:
    result = match_offered_service("dental cleaning", ["hearing test", "sinus consult"])
    assert isinstance(result, Err)
    assert isinstance(result.error, NotOffered)
    assert result.error.named_service == "dental cleaning"


def test_match_ignores_transcription_casing_and_spacing() -> None:
    # "name equals an offered service" (Property 3) is judged on the name, not its
    # transcription: speech recognition lower-cases and re-spaces what the caller
    # said, so comparing raw bytes made every real spoken booking fail as
    # not-offered. The value returned is the exact *configured* string.
    for spoken in ("Hearing Test", "HEARING TEST", "  hearing   test  "):
        result = match_offered_service(spoken, ["Hearing Test"])
        assert isinstance(result, Ok), spoken
        assert result.value == "Hearing Test"


def test_match_stays_exact_and_does_not_fuzzy_match() -> None:
    # Normalizing case/whitespace must not become substring or fuzzy matching:
    # that is what keeps a symptom from ever resolving to a service (Req 10.2).
    for spoken in ("hearing", "test", "ear test", "hearing tests", "hearingtest"):
        result = match_offered_service(spoken, ["Hearing Test"])
        assert isinstance(result, Err), spoken
        assert isinstance(result.error, NotOffered)
        assert result.error.named_service == spoken


def test_offered_service_names_preserves_config_order() -> None:
    kb = ClinicKnowledgeBase(
        location="123 Main St",
        services=[ServiceConfig(name="hearing test"), ServiceConfig(name="sinus consult")],
    )
    assert offered_service_names(kb) == ["hearing test", "sinus consult"]


# --- check_availability (Req 2.2, 2.3, 2.10, 4.4) --------------------------

#: Reference instant for the not-yet-started filter, pinned before every seeded
#: slot below. Passing it explicitly keeps these tests deterministic: they seed
#: 2025 dates, so leaving `now` at the wall clock made them quietly
#: time-dependent and they would have started failing once that date passed.
NOW = datetime(2025, 1, 1, tzinfo=timezone.utc)


def test_check_availability_caps_at_default_limit_of_three() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots(
        [_slot(f"s{i}", f"2025-06-0{i}T09:00:00Z") for i in range(1, 6)]  # 5 open slots
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-01-01",
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert len(result.value) == DEFAULT_AVAILABILITY_LIMIT
    # Ordered by start; every offered slot carries a concrete date and time.
    starts = [s.start for s in result.value]
    assert starts == sorted(starts)
    assert all(s.start and s.end for s in result.value)


def test_check_availability_returns_fewer_when_few_open() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots([_slot("s1", "2025-06-01T09:00:00Z"), _slot("s2", "2025-06-02T09:00:00Z")])

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-01-01",
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert len(result.value) == 2


def test_check_availability_empty_when_no_open_slots() -> None:
    store = MemoryAppointmentStore()
    # A booked slot is not open, so nothing is offered (orchestrator -> waitlist).
    store.seed_slot(_slot("s1", "2025-06-01T09:00:00Z", status=SlotStatus.BOOKED))

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-01-01",
        now=NOW,
    )

    assert isinstance(result, Ok)
    assert result.value == []


def test_check_availability_only_matches_requested_service() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("s1", "2025-06-01T09:00:00Z", service=SERVICE),
            _slot("s2", "2025-06-01T10:00:00Z", service="sinus consult"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-01-01",
        now=NOW,
    )

    # Both are offered. One ENT doctor takes whichever ENT service the caller
    # needs in whatever half hour is free, so the label a slot was published under
    # does not restrict who may book it — the caller's service is recorded on the
    # appointment instead. Scoping by label reported a wide-open day as fully
    # booked on a live call.
    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["s1", "s2"]
    # And every offer carries the service the caller actually asked for.
    assert {s.service for s in result.value} == {SERVICE}


# --- not-yet-started filtering ---------------------------------------------
#
# Regression: the store's list_open_slots is date-scoped by contract, so at 17:00
# it still returns an untaken 11:30 slot from the same morning. Observed on a live
# call — the agent offered a slot that had already passed and the caller had to
# correct it.


def test_check_availability_does_not_offer_a_slot_that_already_started() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("past", "2025-06-01T09:00:00Z"),
            _slot("future", "2025-06-01T14:00:00Z"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 6, 1, 11, 30, tzinfo=timezone.utc),
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["future"]


def test_check_availability_offers_a_slot_starting_exactly_now() -> None:
    """The boundary is inclusive: a slot starting this instant is still bookable."""
    store = MemoryAppointmentStore()
    store.seed_slot(_slot("now", "2025-06-01T11:30:00Z"))

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 6, 1, 11, 30, tzinfo=timezone.utc),
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["now"]


def test_check_availability_handles_naive_and_offset_slot_starts() -> None:
    """Slot starts may carry Z, an explicit offset, or nothing; all are UTC."""
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("naive-past", "2025-06-01T09:00:00"),
            _slot("offset-future", "2025-06-01T14:00:00+00:00"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 6, 1, 11, 30, tzinfo=timezone.utc),
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["offset-future"]


def test_check_availability_keeps_a_slot_with_an_unparseable_start() -> None:
    """A malformed record should surface for correction, not silently vanish."""
    store = MemoryAppointmentStore()
    store.seed_slot(_slot("broken", "not-a-timestamp"))

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 6, 1, 11, 30, tzinfo=timezone.utc),
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["broken"]


def test_check_availability_retrieval_failure_maps_to_store_failure() -> None:
    inner = MemoryAppointmentStore()
    inner.seed_slot(_slot("s1", "2025-06-01T09:00:00Z"))
    faulty = wrap(inner, fail_on("list_open_slots_for_provider"))

    result = check_availability(faulty, service=SERVICE, provider_ids=[PROVIDER])

    assert isinstance(result, Err)
    assert isinstance(result.error, StoreFailure)
    assert result.error.store == "AppointmentStore"

# --- open time is offered whatever label it was published under -------------
#
# Regression: a slot's id is provider+day+start with no service in it, so
# publishing a day under a second service overwrites the same slots instead of
# adding parallel ones. A year published as "ENT Consultation" therefore made
# every "Hearing Test" request come back with nothing available, on a day with
# 48 open slots. Observed live.


def test_check_availability_offers_time_published_under_another_service() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("s1", "2025-06-01T09:00:00Z", service="ENT Consultation"),
            _slot("s2", "2025-06-01T10:00:00Z", service="ENT Consultation"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-01-01",
        now=NOW,
        also_published_as=["ENT Consultation", SERVICE, "Sinus Treatment"],
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["s1", "s2"]


def test_check_availability_needs_no_service_hint_to_find_the_doctors_time() -> None:
    """No label bookkeeping required: the provider's open calendar is the answer.

    ``also_published_as`` used to be needed to widen the search past the label a
    day happened to be published under. Availability no longer reads the label at
    all, so the parameter is inert and passing nothing still finds the time.
    """
    store = MemoryAppointmentStore()
    store.seed_slots([_slot("s1", "2025-06-01T09:00:00Z", service="ENT Consultation")])

    result = check_availability(
        store, service=SERVICE, provider_ids=[PROVIDER], from_date="2025-01-01", now=NOW
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["s1"]
    assert result.value[0].service == SERVICE


def test_check_availability_offers_one_minute_once_preferring_exact_service() -> None:
    """A minute must never be offered twice, or two callers get the same slot."""
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("other", "2025-06-01T09:00:00Z", service="ENT Consultation"),
            _slot("exact", "2025-06-01T09:00:00Z", service=SERVICE),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-01-01",
        now=NOW,
        also_published_as=["ENT Consultation", SERVICE],
    )

    assert isinstance(result, Ok)
    # One offer for 09:00, and it is the slot whose own label the caller asked for.
    assert [s.id for s in result.value] == ["exact"]


def test_check_availability_widened_search_still_respects_the_limit() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("s1", "2025-06-01T09:00:00Z", service="ENT Consultation"),
            _slot("s2", "2025-06-01T09:30:00Z", service="ENT Consultation"),
            _slot("s3", "2025-06-01T10:00:00Z", service="Sinus Treatment"),
            _slot("s4", "2025-06-01T10:30:00Z", service="Sinus Treatment"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-01-01",
        now=NOW,
        also_published_as=["ENT Consultation", "Sinus Treatment"],
    )

    assert isinstance(result, Ok)
    assert len(result.value) == DEFAULT_AVAILABILITY_LIMIT
    assert [s.id for s in result.value] == ["s1", "s2", "s3"]


def test_check_availability_widened_search_still_hides_booked_time() -> None:
    """Widening the label search must not resurrect a slot someone already has."""
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot(
                "taken",
                "2025-06-01T09:00:00Z",
                service="ENT Consultation",
                status=SlotStatus.BOOKED,
            ),
            _slot("free", "2025-06-01T09:30:00Z", service="ENT Consultation"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-01-01",
        now=NOW,
        also_published_as=["ENT Consultation"],
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["free"]


# --- the hour the caller asked for -----------------------------------------
#
# This clinic publishes midnight to midnight, so the earliest three slots of any
# day are 00:00, 00:30 and 01:00. Without a time floor every caller is offered
# the middle of the night no matter what hour they said.


def test_check_availability_starts_at_the_time_the_caller_asked_for() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("midnight", "2025-06-01T00:00:00Z"),
            _slot("early", "2025-06-01T01:00:00Z"),
            _slot("afternoon", "2025-06-01T15:00:00Z"),
            _slot("later", "2025-06-01T15:30:00Z"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 5, 1, tzinfo=timezone.utc),
        from_time="15:00",
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["afternoon", "later"]


def test_check_availability_includes_a_slot_starting_exactly_at_the_asked_time() -> None:
    store = MemoryAppointmentStore()
    store.seed_slots([_slot("on_the_hour", "2025-06-01T15:00:00Z")])

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 5, 1, tzinfo=timezone.utc),
        from_time="15:00",
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["on_the_hour"]


def test_check_availability_rolls_past_the_asked_day_when_the_rest_is_full() -> None:
    """The time is a floor, not a filter: a full afternoon rolls into the next day."""
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("too_early", "2025-06-01T09:00:00Z"),
            _slot("next_day", "2025-06-02T09:00:00Z"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 5, 1, tzinfo=timezone.utc),
        from_time="15:00",
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["next_day"]


def test_check_availability_ignores_an_unreadable_asked_time() -> None:
    """A time that arrives as speech-to-text noise must not refuse the booking."""
    store = MemoryAppointmentStore()
    store.seed_slots([_slot("s1", "2025-06-01T09:00:00Z")])

    for spoken in ("half three", "3pm", "", "25:00", "15:00:00 sharp"):
        result = check_availability(
            store,
            service=SERVICE,
            provider_ids=[PROVIDER],
            from_date="2025-06-01",
            now=datetime(2025, 5, 1, tzinfo=timezone.utc),
            from_time=spoken,
        )
        assert isinstance(result, Ok), spoken
        assert [s.id for s in result.value] == ["s1"], spoken


def test_check_availability_asked_time_never_offers_a_slot_that_already_started() -> None:
    """The asked-for time raises the floor; it can never lower it below now."""
    store = MemoryAppointmentStore()
    store.seed_slots(
        [
            _slot("this_morning", "2025-06-01T09:00:00Z"),
            _slot("this_evening", "2025-06-01T18:00:00Z"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        # It is already 17:00 and the caller asks for 09:00 — that hour is gone.
        now=datetime(2025, 6, 1, 17, 0, tzinfo=timezone.utc),
        from_time="09:00",
    )

    assert isinstance(result, Ok)
    assert [s.id for s in result.value] == ["this_evening"]


# --- the bound has to reach the query --------------------------------------
#
# Regression, measured against DynamoDB with a year of 30-minute slots published:
# reading the whole open-slot partition to offer three took 6.2 s for one service
# and 8.1 s for the tool. Nova Sonic runs tool calls concurrently with generation,
# so it had finished speaking long before the result arrived and answered "no
# availability" from its own guess. Trimming the list after the fact cannot fix
# that — the bound must reach the query.


class _RecordingStore(MemoryAppointmentStore):
    """Memory store that records how the provider's calendar was read."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, int | None]] = []

    def list_open_slots_for_provider(  # type: ignore[override]
        self,
        provider_id: str,
        from_bound: str,
        *,
        limit: int | None = None,
    ) -> object:
        self.calls.append((provider_id, from_bound, limit))
        return super().list_open_slots_for_provider(
            provider_id, from_bound, limit=limit
        )


def test_check_availability_bounds_the_store_read_by_a_limit() -> None:
    store = _RecordingStore()
    store.seed_slots(
        [_slot(f"s{n}", f"2025-06-01T{n:02d}:00:00Z") for n in range(10, 20)]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 5, 1, tzinfo=timezone.utc),
    )

    assert isinstance(result, Ok)
    assert len(result.value) == DEFAULT_AVAILABILITY_LIMIT
    # Every read carried a limit; none asked the store for the whole calendar.
    assert store.calls, "the store was never asked"
    for _provider, _bound, limit in store.calls:
        assert limit is not None and limit > 0
    # One query per provider, not one per configured service.
    assert len(store.calls) == 1


def test_check_availability_pushes_the_asked_time_into_the_store_bound() -> None:
    """The floor goes to the query, so a day's earlier slots are never read."""
    store = _RecordingStore()
    store.seed_slots([_slot("s1", "2025-06-01T15:00:00Z")])

    check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 5, 1, tzinfo=timezone.utc),
        from_time="15:00",
    )

    assert store.calls
    bounds = {call[1] for call in store.calls}
    assert bounds == {"2025-06-01T15:00"}


def test_check_availability_bound_never_widens_before_the_requested_date() -> None:
    """`now` sitting weeks earlier must not turn into a bound before from_date."""
    store = _RecordingStore()
    store.seed_slots([_slot("s1", "2025-06-01T09:00:00Z")])

    check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    assert store.calls
    for _provider, bound, _limit in store.calls:
        assert bound >= "2025-06-01"


def test_check_availability_limit_leaves_room_for_de_duplication() -> None:
    """Asking each label for exactly `limit` could starve the offer to one time."""
    store = _RecordingStore()
    store.seed_slots(
        [
            _slot("a1", "2025-06-01T09:00:00Z", service="ENT Consultation"),
            _slot("a2", "2025-06-01T09:30:00Z", service="ENT Consultation"),
            _slot("a3", "2025-06-01T10:00:00Z", service="ENT Consultation"),
        ]
    )

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 5, 1, tzinfo=timezone.utc),
        also_published_as=["ENT Consultation", "Sinus Treatment"],
    )

    assert isinstance(result, Ok)
    assert len(result.value) == DEFAULT_AVAILABILITY_LIMIT
    for _provider, _bound, limit in store.calls:
        assert limit is not None
        assert limit >= DEFAULT_AVAILABILITY_LIMIT


def test_check_availability_zero_limit_reads_nothing_at_all() -> None:
    store = _RecordingStore()
    store.seed_slots([_slot("s1", "2025-06-01T09:00:00Z")])

    result = check_availability(
        store,
        service=SERVICE,
        provider_ids=[PROVIDER],
        from_date="2025-06-01",
        now=datetime(2025, 5, 1, tzinfo=timezone.utc),
        limit=0,
    )

    assert isinstance(result, Ok)
    assert result.value == []
