"""Tests for the pre-call clinic briefing (``voice/clinic_briefing.py``).

The briefing exists because of a measured ordering problem: Nova Sonic reaches
``END_TURN`` before a tool executes, so a detail it had to fetch was unavailable
while it was speaking and it filled the gap from its own priors — answering "Monday
through Friday, 8:00 AM to 5:00 PM" for a clinic whose document says Monday to
Saturday. These tests pin the properties that make briefing safe: configuration
outranks documents, clinical passages never reach the prompt, and an empty clinic
produces no briefing at all.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from clinic_front_desk.data_layer.faults import fail_on, wrap
from clinic_front_desk.data_layer.memory import (
    MemoryAppointmentStore,
    MemoryClinicDocumentStore,
    MemoryClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import (
    ClinicDocument,
    ClinicKnowledgeBase,
    DayHours,
    DocumentChunk,
    Provider,
    ServiceConfig,
    Slot,
    SlotStatus,
)
from clinic_front_desk.voice.clinic_briefing import (
    build_clinic_briefing,
    build_clinic_card,
    with_clinic_briefing,
)


def _kb() -> ClinicKnowledgeBase:
    return ClinicKnowledgeBase(
        location="123 Configured Street, Springfield",
        hours={1: DayHours(open="09:00", close="17:00"), 5: DayHours(open="09:00", close="13:00")},
        services=[
            ServiceConfig(
                name="Hearing Test",
                prep_instructions="Avoid loud noise for 24 hours beforehand.",
                price=150.0,
            ),
            ServiceConfig(name="Consultation", price=None),
        ],
        accepted_insurance=["Aetna", "Medicare"],
        providers=[Provider(id="p1", name="Dr. Amara Reyes", specialty="ENT")],
        configured=True,
    )


def _kb_store(kb: ClinicKnowledgeBase | None) -> MemoryClinicKnowledgeBaseStore:
    store = MemoryClinicKnowledgeBaseStore()
    if kb is not None:
        store.save(kb)
    return store


def _doc_store(*texts: str) -> MemoryClinicDocumentStore:
    store = MemoryClinicDocumentStore()
    store.put(
        ClinicDocument(
            id="doc",
            filename="info.pdf",
            content_type="application/pdf",
            uploaded_at="2026-01-01T00:00:00+00:00",
            byte_size=1,
            chunk_count=len(texts),
        ),
        original=b"x",
        chunks=[
            DocumentChunk(document_id="doc", index=i, text=text, page=1, embedding=(1.0,))
            for i, text in enumerate(texts)
        ],
    )
    return store


PARKING = "Parking Patient parking is free in the surface lot behind the building."
HOLIDAYS = "Holiday closures We are closed on New Year's Day and Thanksgiving."


# ---------------------------------------------------------------------------
# Empty states
# ---------------------------------------------------------------------------


def test_nothing_configured_and_nothing_uploaded_yields_no_briefing() -> None:
    # No empty scaffolding: a prompt section headed "what you know" followed by
    # nothing would invite the model to fill it in.
    assert build_clinic_briefing(_kb_store(None), MemoryClinicDocumentStore()) == ""


def test_no_stores_at_all_yields_no_briefing() -> None:
    assert build_clinic_briefing(None, None) == ""


def test_with_clinic_briefing_leaves_the_prompt_untouched_when_empty() -> None:
    assert with_clinic_briefing("BASE", _kb_store(None), None) == "BASE"


# ---------------------------------------------------------------------------
# Configured facts
# ---------------------------------------------------------------------------


def test_configured_details_are_stated() -> None:
    briefing = build_clinic_briefing(_kb_store(_kb()), None)

    assert "123 Configured Street, Springfield" in briefing
    assert "Monday 09:00 to 17:00" in briefing
    assert "Friday 09:00 to 13:00" in briefing
    assert "Hearing Test" in briefing
    assert "$150.00" in briefing
    assert "Avoid loud noise for 24 hours beforehand." in briefing
    assert "Aetna, Medicare" in briefing
    assert "Dr. Amara Reyes" in briefing


def test_a_service_without_a_price_is_named_but_not_priced() -> None:
    briefing = build_clinic_briefing(_kb_store(_kb()), None)

    assert "Consultation" in briefing
    # No invented figure for the unpriced service.
    assert "Consultation $" not in briefing


def test_closed_days_are_simply_absent() -> None:
    briefing = build_clinic_briefing(_kb_store(_kb()), None)

    assert "Sunday" not in briefing
    assert "Saturday" not in briefing


def test_an_unconfigured_clinic_with_documents_briefs_from_documents_alone() -> None:
    briefing = build_clinic_briefing(_kb_store(None), _doc_store(PARKING))

    assert "surface lot" in briefing
    assert "Confirmed clinic details" not in briefing


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_configuration_is_stated_before_documents_and_marked_authoritative() -> None:
    briefing = build_clinic_briefing(
        _kb_store(_kb()), _doc_store("The clinic is at 999 Document Drive.")
    )

    config_at = briefing.index("123 Configured Street")
    document_at = briefing.index("999 Document Drive")
    # Order is the instruction: the doctor confirmed the configured values, a
    # document merely mentions something.
    assert config_at < document_at
    assert "outrank" in briefing


# ---------------------------------------------------------------------------
# Clinical exclusion
# ---------------------------------------------------------------------------


def test_a_clinical_passage_never_reaches_the_prompt() -> None:
    briefing = build_clinic_briefing(
        None,
        _doc_store(PARKING, "Dosing schedule Take 25 mg twice a day with food."),
    )

    # Once in the prompt there is no tool boundary left to screen it, so it must
    # not go in at all.
    assert "surface lot" in briefing
    assert "25 mg" not in briefing
    assert "Dosing" not in briefing


def test_administrative_passages_that_mention_treatment_are_kept() -> None:
    # The false positives that silently dropped a real clinic's services list and
    # its entire what-to-bring section.
    briefing = build_clinic_briefing(
        None,
        _doc_store(
            "Nose services We offer Sinus Treatment for sinusitis and Nose Bleed Treatment.",
            "What to bring If you have had a hearing test elsewhere, bring the audiogram.",
        ),
    )

    assert "Sinus Treatment for sinusitis" in briefing
    assert "bring the audiogram" in briefing


# ---------------------------------------------------------------------------
# Budget and failure
# ---------------------------------------------------------------------------


def test_the_document_half_is_capped_and_the_overflow_is_flagged() -> None:
    long_passages = [f"Section {i} " + ("filler text " * 40) for i in range(20)]

    briefing = build_clinic_briefing(
        None, _doc_store(*long_passages), max_document_chars=1000
    )

    assert len(briefing) < 3000
    # The model is told the rest is reachable, so it looks it up rather than
    # assuming the clinic has nothing more.
    assert "answer_faq" in briefing
    assert "clinic_info" in briefing


def test_no_overflow_note_when_everything_fits() -> None:
    briefing = build_clinic_briefing(None, _doc_store(PARKING, HOLIDAYS))

    assert "not shown here" not in briefing


def test_whitespace_in_a_passage_is_collapsed_for_the_prompt() -> None:
    briefing = build_clinic_briefing(None, _doc_store("Parking\n\n   is   free\there."))

    assert "Parking is free here." in briefing


def test_a_document_store_failure_degrades_to_the_configured_half() -> None:
    faulty = wrap(_doc_store(PARKING), fail_on("list_chunks"))

    briefing = build_clinic_briefing(_kb_store(_kb()), faulty)

    assert "123 Configured Street" in briefing
    assert "surface lot" not in briefing


def test_a_config_store_failure_degrades_to_the_document_half() -> None:
    faulty = wrap(_kb_store(_kb()), fail_on("get"))

    briefing = build_clinic_briefing(faulty, _doc_store(PARKING))

    assert "surface lot" in briefing
    assert "123 Configured Street" not in briefing


def test_both_stores_failing_yields_no_briefing_rather_than_an_error() -> None:
    briefing = build_clinic_briefing(
        wrap(_kb_store(_kb()), fail_on("get")),
        wrap(_doc_store(PARKING), fail_on("list_chunks")),
    )

    assert briefing == ""


# ---------------------------------------------------------------------------
# The instructions the briefing carries
# ---------------------------------------------------------------------------


def test_the_briefing_forbids_looking_up_what_it_already_contains() -> None:
    briefing = build_clinic_briefing(None, _doc_store(PARKING))

    # Without this the model preferred the tool, whose result arrives after it has
    # finished speaking.
    assert "Do NOT call answer_faq" in briefing


def test_the_briefing_forbids_reformatting_a_value() -> None:
    briefing = build_clinic_briefing(None, _doc_store(PARKING))

    # It saw "12:00 AM to 11:59 PM", judged it implausible, and substituted 8-5.
    lowered = briefing.lower()
    assert "exactly as written" in lowered
    assert "unusual" in lowered


def test_the_briefing_forbids_substituting_a_guess() -> None:
    briefing = build_clinic_briefing(None, _doc_store(PARKING))

    assert "Never substitute" in briefing


def test_with_clinic_briefing_appends_to_the_given_prompt() -> None:
    result = with_clinic_briefing("BASE PROMPT", None, _doc_store(PARKING))

    assert result.startswith("BASE PROMPT")
    assert "surface lot" in result


# ---------------------------------------------------------------------------
# The clinic card: an address and a map link the caller can tap
# ---------------------------------------------------------------------------

DIRECTIONS = (
    "Directions and map Coming along Renigunta Road, look for Vartha Press; the "
    "hospital is immediately beside it. The map location is 13.62849 degrees "
    "north, 79.46382 degrees east."
)
WHERE = (
    "Where to find us The address is Aster Narayanadri Hospital, S Number 73/1A, "
    "Renigunta Road, Srinivasa Nagar, Tirupati."
)


def test_no_address_anywhere_yields_no_card() -> None:
    # Better to show nothing than an empty card or a link to nowhere.
    assert build_clinic_card(_kb_store(None), MemoryClinicDocumentStore()) is None
    assert build_clinic_card(None, None) is None


def test_the_configured_address_is_used_and_linked() -> None:
    card = build_clinic_card(_kb_store(_kb()), None)

    assert card is not None
    assert card["address"] == "123 Configured Street, Springfield"
    assert card["maps_url"].startswith("https://www.google.com/maps/search/?api=1&query=")
    # URL-encoded, so a comma or space in the address cannot break the link.
    assert "123%20Configured%20Street" in card["maps_url"]


def test_coordinates_in_a_document_are_preferred_over_a_text_search() -> None:
    card = build_clinic_card(_kb_store(_kb()), _doc_store(WHERE, DIRECTIONS))

    assert card is not None
    # A pin is exact where a text search is a guess.
    assert card["maps_url"].endswith("query=13.62849,79.46382")
    assert card["coordinates"] == "13.62849, 79.46382"


def test_the_document_address_is_used_when_nothing_is_configured() -> None:
    card = build_clinic_card(_kb_store(None), _doc_store(WHERE))

    assert card is not None
    assert "Renigunta Road" in card["address"]


def test_the_section_heading_is_stripped_for_display() -> None:
    # As chunks are actually stored: heading on its own line, then the body.
    # "Where to find us The address is..." reads like a bug on a card.
    chunk = (
        "Where to find us\nThe address is Aster Narayanadri Hospital, "
        "Renigunta Road, Tirupati."
    )
    card = build_clinic_card(_kb_store(None), _doc_store(chunk))

    assert card is not None
    assert not card["address"].startswith("Where to find us")
    assert card["address"].startswith("The address is Aster")


def test_a_body_only_passage_is_left_intact() -> None:
    # No heading to strip, so nothing may be lost off the front.
    chunk = "The address is 9 Long Road, Springfield, and we are beside the park."
    card = build_clinic_card(_kb_store(None), _doc_store("Address\n" + chunk))

    assert card is not None
    assert card["address"] == chunk


def test_the_configured_address_wins_over_the_document_text() -> None:
    card = build_clinic_card(_kb_store(_kb()), _doc_store(WHERE))

    assert card is not None
    assert card["address"] == "123 Configured Street, Springfield"
    # The document's richer directions text is still carried, not discarded.
    assert "Renigunta Road" in card["directions"]


def test_a_card_is_produced_from_coordinates_alone() -> None:
    card = build_clinic_card(None, _doc_store(DIRECTIONS))

    assert card is not None
    assert card["coordinates"] == "13.62849, 79.46382"


def test_a_document_store_failure_falls_back_to_the_configured_address() -> None:
    faulty = wrap(_doc_store(WHERE, DIRECTIONS), fail_on("list_chunks"))

    card = build_clinic_card(_kb_store(_kb()), faulty)

    assert card is not None
    assert card["address"] == "123 Configured Street, Springfield"
    assert "coordinates" not in card


def test_a_document_with_no_location_section_yields_no_card() -> None:
    card = build_clinic_card(
        _kb_store(None), _doc_store("Fees Consultation fees are not listed here.")
    )

    assert card is None


# ---------------------------------------------------------------------------
# The booking calendar in the briefing
# ---------------------------------------------------------------------------
#
# The measured failure this closes: check_availability took 8.1 s against a year
# of published slots, Nova Sonic runs tool calls concurrently with speech, so it
# had finished its turn and answered "there are no open slots on September 10"
# for a day holding 48 of them. Faster queries shrink the window but cannot close
# it — nothing here decides when the model stops talking. Stating the calendar's
# span up front means an early answer is still a grounded one.


def _appointments_with(day_range: tuple[str, str]) -> MemoryAppointmentStore:
    """A store holding open slots on the first and last of ``day_range``."""
    store = MemoryAppointmentStore()
    first, last = day_range
    store.add_slots(
        [
            Slot(
                id=f"s-{first}",
                provider_id="p1",
                service="Hearing Test",
                start=f"{first}T09:00",
                end=f"{first}T09:30",
                status=SlotStatus.OPEN,
            ),
            Slot(
                id=f"s-{last}",
                provider_id="p1",
                service="Hearing Test",
                start=f"{last}T16:00",
                end=f"{last}T16:30",
                status=SlotStatus.OPEN,
            ),
        ]
    )
    return store


def _future(days: int) -> str:
    return (datetime.now(UTC).date() + timedelta(days=days)).isoformat()


def test_briefing_states_how_far_the_calendar_is_published() -> None:
    first, last = _future(1), _future(90)
    briefing = build_clinic_briefing(
        _kb_store(_kb()), appointments=_appointments_with((first, last))
    )

    assert first in briefing
    assert last in briefing


def test_briefing_forbids_refusing_a_date_inside_the_published_range() -> None:
    """The exact mistake: turning a caller away without checking."""
    briefing = build_clinic_briefing(
        _kb_store(_kb()), appointments=_appointments_with((_future(1), _future(90)))
    )

    lowered = briefing.lower()
    assert "never tell a caller" in lowered
    assert "check_availability" in briefing


def test_briefing_says_plainly_when_nothing_is_published() -> None:
    """An unpublished calendar must not read as "ask the tool and find out"."""
    briefing = build_clinic_briefing(
        _kb_store(_kb()), appointments=MemoryAppointmentStore()
    )

    lowered = briefing.lower()
    assert "no open slots published" in lowered
    assert "do not offer to book" in lowered


def test_briefing_ignores_slots_that_have_already_passed() -> None:
    """A calendar whose last slot was last year is not a published calendar."""
    briefing = build_clinic_briefing(
        _kb_store(_kb()), appointments=_appointments_with(("2020-01-01", "2020-03-01"))
    )

    assert "2020-01-01" not in briefing
    assert "no open slots published" in briefing.lower()


def test_briefing_omits_the_calendar_when_no_store_is_given() -> None:
    briefing = build_clinic_briefing(_kb_store(_kb()))

    assert "booking calendar" not in briefing.lower()


def test_briefing_survives_a_calendar_read_failure() -> None:
    """A calendar the store cannot read must not cost the caller the whole briefing."""
    faulty = wrap(
        _appointments_with((_future(1), _future(90))), fail_on("open_slot_span")
    )

    briefing = build_clinic_briefing(_kb_store(_kb()), appointments=faulty)

    # The configured facts still made it through.
    assert "123 Configured Street, Springfield" in briefing


def test_briefing_needs_a_configured_provider_to_read_a_calendar() -> None:
    """Slots belong to a provider, so with none configured there is nothing to read."""
    kb = ClinicKnowledgeBase(
        location="123 Configured Street, Springfield",
        hours={1: DayHours(open="09:00", close="17:00")},
        configured=True,
    )

    briefing = build_clinic_briefing(
        _kb_store(kb), appointments=_appointments_with((_future(1), _future(90)))
    )

    assert "booking calendar" not in briefing.lower()
