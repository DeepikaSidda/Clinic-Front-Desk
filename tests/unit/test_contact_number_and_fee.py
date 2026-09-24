"""The clinic's own number, and a fee quoted in the right currency.

Two things a caller asks that the agent could previously only fumble:

* **"What's your number?"** — there was no field for it anywhere, so "you can ring
  the clinic during opening hours" was an instruction nobody could act on.
* **"What does a visit cost?"** — the price existed but rendered as ``$500.00``. For
  a clinic in Tirupati charging rupees, that misstates the fee by roughly eighty
  times, in a confident voice, on a recorded line. Same class of harm as inventing
  availability: a commitment stated as the clinic's word.

Both are kept **structured** rather than answerable from uploaded documents, for the
same reason: a number lifted out of a PDF could be a fax line, a supplier's, or last
year's fee, and the caller will act on it either way.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from clinic_front_desk.data_layer.memory import MemoryClinicKnowledgeBaseStore
from clinic_front_desk.handover.live import (
    LiveCallRegistry,
    LiveHandoverService,
    UNATTENDED_MESSAGE,
    unattended_message,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ServiceConfig,
    clinic_kb_from_item,
    clinic_kb_to_item,
    format_money,
    is_ok,
)
from clinic_front_desk.tools.faq import DOCUMENT_FALLBACK_TOPICS, answer_faq
from clinic_front_desk.voice.clinic_briefing import build_clinic_briefing

CONTACT = "1234567890"


def _kb(contact: str = CONTACT, fee: float | None = 500.0) -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location="12 Tilak Road, Tirupati",
        hours={index: DayHours(open="09:00", close="19:30") for index in range(6)},
        services=[ServiceConfig(name="ENT Consultation", price=fee)],
        contact_phone=contact,
        providers=[Provider(id="prov-1", name="Dr Raana", specialty="ENT")],
        configured=True,
    )


def _store(kb: ClinicKnowledgeBase) -> Any:
    store = MemoryClinicKnowledgeBaseStore()
    assert is_ok(store.save(kb))
    return store


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- the number -------------------------------------------------------------


def test_the_agent_can_give_the_clinic_number() -> None:
    result = answer_faq(_store(_kb()), "contact")

    assert is_ok(result)
    assert CONTACT in result.value


def test_an_unconfigured_number_is_unavailable_not_invented() -> None:
    """The failure that matters: a wrong number sends the caller to a stranger."""
    result = answer_faq(_store(_kb(contact="")), "contact")

    assert not is_ok(result)
    assert result.error.kind == "not_found"


def test_the_number_is_never_taken_from_a_document() -> None:
    """A number in a PDF could be a fax line, a supplier's, or a previous practice's."""
    assert "contact" not in DOCUMENT_FALLBACK_TOPICS


def test_the_briefing_tells_the_agent_the_number_and_to_use_no_other() -> None:
    briefing = build_clinic_briefing(_store(_kb()), None)

    assert CONTACT in briefing
    assert "never say any other number" in briefing.lower()


def test_the_briefing_mentions_no_number_when_none_is_configured() -> None:
    briefing = build_clinic_briefing(_store(_kb(contact="")), None)

    assert "clinic phone number" not in briefing.lower()


# -- the number in the nobody-picked-up line --------------------------------


def test_the_apology_names_the_number() -> None:
    """"Ring the clinic" is not actionable without it."""
    message = unattended_message(CONTACT)

    assert CONTACT in message
    assert "been able to pick up" in message


def test_the_apology_is_unchanged_when_there_is_no_number() -> None:
    """Degrades to the previous wording rather than reading out a blank."""
    assert unattended_message("") == UNATTENDED_MESSAGE
    assert unattended_message("   ") == UNATTENDED_MESSAGE


def test_the_watcher_speaks_the_number_it_is_given() -> None:
    """End to end through the service, since that is what a caller actually hears."""
    registry = LiveCallRegistry()
    spoken: list[str] = []

    async def send(message: dict[str, Any]) -> None:
        if message.get("message_type") == "transcript":
            spoken.append(str(message.get("text")))

    registry.register("s1", send)
    registry.mark_needs_human("s1", "patient_request")
    service = LiveHandoverService(
        registry, contact_phone_provider=lambda: CONTACT
    )
    service.synthesize = lambda _text: b""  # type: ignore[method-assign]

    _run(service.watch_unattended("s1", after_seconds=0, poll_seconds=0.01))

    assert any(CONTACT in line for line in spoken), spoken


def test_a_failing_contact_lookup_still_apologises() -> None:
    """A store hiccup must cost the number, not the apology."""
    registry = LiveCallRegistry()
    spoken: list[str] = []

    async def send(message: dict[str, Any]) -> None:
        if message.get("message_type") == "transcript":
            spoken.append(str(message.get("text")))

    def explode() -> str:
        raise RuntimeError("table unavailable")

    registry.register("s1", send)
    registry.mark_needs_human("s1", "patient_request")
    service = LiveHandoverService(registry, contact_phone_provider=explode)
    service.synthesize = lambda _text: b""  # type: ignore[method-assign]

    told = _run(service.watch_unattended("s1", after_seconds=0, poll_seconds=0.01))

    assert told is True
    assert any("been able to pick up" in line for line in spoken), spoken


# -- the fee ----------------------------------------------------------------


def test_the_fee_is_quoted_in_rupees() -> None:
    result = answer_faq(_store(_kb()), "pricing", service="ENT Consultation")

    assert is_ok(result)
    assert "500 rupees" in result.value
    assert "$" not in result.value


def test_a_whole_fee_is_not_read_out_with_decimals() -> None:
    """A receptionist says "five hundred rupees", not "five hundred point zero zero"."""
    assert format_money(500.0) == "500 rupees"


def test_a_fractional_fee_keeps_its_paise() -> None:
    assert format_money(499.5) == "499.50 rupees"


def test_the_briefing_states_the_fee_in_rupees() -> None:
    briefing = build_clinic_briefing(_store(_kb()), None)

    assert "500 rupees" in briefing
    assert "$" not in briefing


@pytest.mark.parametrize("fee", [None])
def test_an_unpriced_service_is_still_not_priced(fee: float | None) -> None:
    result = answer_faq(_store(_kb(fee=fee)), "pricing", service="ENT Consultation")

    assert not is_ok(result)


# -- it survives a round trip through storage -------------------------------


def test_the_number_survives_dynamodb_serialisation() -> None:
    restored = clinic_kb_from_item(clinic_kb_to_item(_kb()))

    assert restored.contact_phone == CONTACT
    assert restored.services[0].price == 500.0


def test_a_config_written_before_the_field_existed_still_loads() -> None:
    """Old items have no contact_phone key; loading must not raise."""
    item = clinic_kb_to_item(_kb())
    del item["contact_phone"]

    restored = clinic_kb_from_item(item)

    assert restored.contact_phone == ""
