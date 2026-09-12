"""Store-contract tests for publishing and reading a day's slots.

Every test runs against the in-memory fake *and* the DynamoDB store on a
moto-mocked table, because these two operations are exactly where the backends
drifted: ``list_slots_for_day`` worked in memory and silently returned nothing on
DynamoDB, since it was written against the wrong partition key. Nothing caught it
until a live publish, so the equivalence is pinned here (Req 16.5).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

from clinic_front_desk.data_layer.dynamodb import DynamoAppointmentStore, create_table
from clinic_front_desk.data_layer.memory import MemoryAppointmentStore
from clinic_front_desk.models import Slot, SlotStatus, is_err, is_ok
from clinic_front_desk.scheduling import generate_day_slots

DAY = "2026-09-10"
OTHER_DAY = "2026-09-11"
PROVIDER = "prov-raana"
OTHER_PROVIDER = "prov-other"
SERVICE = "ENT Consultation"


@pytest.fixture
def memory_store() -> MemoryAppointmentStore:
    return MemoryAppointmentStore()


@pytest.fixture
def dynamo_store() -> Iterator[Any]:
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        table = create_table(resource, "clinic-front-desk-test")
        yield DynamoAppointmentStore(table)


@pytest.fixture(params=["memory", "dynamo"])
def store(request: pytest.FixtureRequest) -> Any:
    return request.getfixturevalue(f"{request.param}_store")


def _day(day: str = DAY, provider: str = PROVIDER, **kwargs: Any) -> list[Slot]:
    return generate_day_slots(day, provider, SERVICE, **kwargs)


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_a_published_day_is_readable_back(store: Any) -> None:
    written = store.add_slots(_day())

    assert is_ok(written)
    assert len(written.value) == 48

    read = store.list_slots_for_day(PROVIDER, DAY)
    assert is_ok(read)
    assert len(read.value) == 48


def test_an_unpublished_day_reads_as_empty(store: Any) -> None:
    result = store.list_slots_for_day(PROVIDER, DAY)

    assert is_ok(result)
    assert result.value == []


def test_slots_come_back_earliest_first(store: Any) -> None:
    store.add_slots(list(reversed(_day())))

    read = store.list_slots_for_day(PROVIDER, DAY)

    starts = [slot.start for slot in read.value]
    assert starts == sorted(starts)


def test_published_slots_are_open_and_keep_their_details(store: Any) -> None:
    store.add_slots(_day(start="09:00", end="10:00"))

    read = store.list_slots_for_day(PROVIDER, DAY)

    assert [s.start for s in read.value] == [f"{DAY}T09:00", f"{DAY}T09:30"]
    assert all(s.status is SlotStatus.OPEN for s in read.value)
    assert all(s.service == SERVICE for s in read.value)
    assert all(s.provider_id == PROVIDER for s in read.value)


def test_publishing_is_idempotent(store: Any) -> None:
    store.add_slots(_day())
    store.add_slots(_day())

    read = store.list_slots_for_day(PROVIDER, DAY)

    assert len(read.value) == 48


def test_republishing_does_not_reopen_a_booked_slot(store: Any) -> None:
    slots = _day(start="09:00", end="10:00")
    store.add_slots(slots)
    store.set_slot_status(slots[0].id, SlotStatus.BOOKED)

    written = store.add_slots(slots)

    # The booked slot is skipped, not reset — resetting it would strand a
    # patient's appointment on a slot that now says it is free.
    assert is_ok(written)
    assert [s.id for s in written.value] == [slots[1].id]
    after = store.get_slot(slots[0].id)
    assert after.value is not None
    assert after.value.status is SlotStatus.BOOKED


def test_a_slot_with_no_provider_is_rejected_and_nothing_is_written(store: Any) -> None:
    good = _day(start="09:00", end="10:00")
    # Slot.__post_init__ rejects a blank provider at construction, so the only way
    # to present one to the store is to blank it afterwards — which is also how it
    # could happen for real, via a mutated object.
    bad = Slot(
        id="slot-bad",
        provider_id=PROVIDER,
        service=SERVICE,
        start=f"{DAY}T11:00",
        end=f"{DAY}T11:30",
    )
    bad.provider_id = ""

    result = store.add_slots([*good, bad])

    assert is_err(result)
    # Validated before any write, so the day is never left half-published.
    assert store.list_slots_for_day(PROVIDER, DAY).value == []


# ---------------------------------------------------------------------------
# Isolation between days and providers
# ---------------------------------------------------------------------------


def test_a_day_read_excludes_other_days(store: Any) -> None:
    store.add_slots(_day(DAY))
    store.add_slots(_day(OTHER_DAY))

    read = store.list_slots_for_day(PROVIDER, DAY)

    assert len(read.value) == 48
    assert all(slot.start.startswith(DAY) for slot in read.value)


def test_a_day_read_excludes_other_providers(store: Any) -> None:
    store.add_slots(_day(provider=PROVIDER))
    store.add_slots(_day(provider=OTHER_PROVIDER))

    read = store.list_slots_for_day(PROVIDER, DAY)

    assert len(read.value) == 48
    assert {slot.provider_id for slot in read.value} == {PROVIDER}


def test_the_last_slot_of_the_day_belongs_to_that_day(store: Any) -> None:
    # It ends at midnight, which is tomorrow's date — it must not leak into the
    # next day's calendar or vanish from this one.
    store.add_slots(_day(DAY))

    today = store.list_slots_for_day(PROVIDER, DAY)
    tomorrow = store.list_slots_for_day(PROVIDER, OTHER_DAY)

    assert today.value[-1].start == f"{DAY}T23:30"
    assert today.value[-1].end == f"{OTHER_DAY}T00:00"
    assert tomorrow.value == []


# ---------------------------------------------------------------------------
# Interaction with what the agent can be offered
# ---------------------------------------------------------------------------


def test_a_published_day_becomes_offerable_availability(store: Any) -> None:
    store.add_slots(_day(start="09:00", end="11:00"))

    open_slots = store.list_open_slots(PROVIDER, SERVICE, DAY)

    assert is_ok(open_slots)
    assert len(open_slots.value) == 4


def test_a_booked_slot_leaves_the_offerable_set_but_stays_on_the_calendar(
    store: Any,
) -> None:
    slots = _day(start="09:00", end="11:00")
    store.add_slots(slots)
    store.set_slot_status(slots[0].id, SlotStatus.BOOKED)

    offerable = store.list_open_slots(PROVIDER, SERVICE, DAY)
    calendar = store.list_slots_for_day(PROVIDER, DAY)

    # The two views differ on purpose: one answers "what can I offer a caller",
    # the other "what does the doctor's day look like".
    assert len(offerable.value) == 3
    assert len(calendar.value) == 4


# ---------------------------------------------------------------------------
# Removing slots the clinic never opens
# ---------------------------------------------------------------------------
#
# Blocking the out-of-hours slots was the first attempt and it left the doctor's
# day listing 26 struck-through overnight rows above the 22 that matter. Blocking
# is right for a lunch hour someone wants to see; hours the clinic never opens
# should not be on the calendar at all.


def test_removing_slots_takes_them_off_the_calendar_entirely(store: Any) -> None:
    store.add_slots(_day(start="00:00", end="03:00"))
    slots = store.list_slots_for_day(PROVIDER, DAY).value
    overnight = [s for s in slots if s.start.partition("T")[2][:5] < "02:00"]

    removed = store.remove_slots(overnight)

    assert is_ok(removed)
    assert set(removed.value) == {s.id for s in overnight}
    left = store.list_slots_for_day(PROVIDER, DAY).value
    assert [s.start.partition("T")[2][:5] for s in left] == ["02:00", "02:30"]


def test_a_booked_slot_is_never_removed_and_fails_the_batch(store: Any) -> None:
    """Removing it would strand a patient on time that no longer exists."""
    store.add_slots(_day(start="09:00", end="11:00"))
    slots = store.list_slots_for_day(PROVIDER, DAY).value
    store.set_slot_status(slots[0].id, SlotStatus.BOOKED)
    with_booked = store.list_slots_for_day(PROVIDER, DAY).value

    removed = store.remove_slots(with_booked)

    assert is_err(removed)
    # Nothing at all was deleted, including the slots that were safe to remove.
    assert len(store.list_slots_for_day(PROVIDER, DAY).value) == len(with_booked)


def test_removing_a_blocked_slot_is_allowed(store: Any) -> None:
    store.add_slots(_day(start="09:00", end="10:00"))
    slots = store.list_slots_for_day(PROVIDER, DAY).value
    store.set_slot_statuses(slots, SlotStatus.BLOCKED)
    blocked = store.list_slots_for_day(PROVIDER, DAY).value

    removed = store.remove_slots(blocked)

    assert is_ok(removed)
    assert store.list_slots_for_day(PROVIDER, DAY).value == []


def test_removing_is_safe_to_run_twice(store: Any) -> None:
    """An absent id is not an error, so a half-finished run can be repeated."""
    store.add_slots(_day(start="09:00", end="10:00"))
    slots = store.list_slots_for_day(PROVIDER, DAY).value
    store.remove_slots(slots)

    again = store.remove_slots(slots)

    assert is_ok(again)
    assert store.list_slots_for_day(PROVIDER, DAY).value == []


def test_removing_nothing_is_not_an_error(store: Any) -> None:
    assert is_ok(store.remove_slots([]))


def test_a_removed_day_can_be_republished(store: Any) -> None:
    """Reversible: slot ids come from the day and time, not from randomness."""
    store.add_slots(_day(start="09:00", end="10:00"))
    original = store.list_slots_for_day(PROVIDER, DAY).value
    store.remove_slots(original)

    store.add_slots(_day(start="09:00", end="10:00"))

    restored = store.list_slots_for_day(PROVIDER, DAY).value
    assert [s.id for s in restored] == [s.id for s in original]
