"""Brief the agent on the clinic before the call starts.

Why this exists
---------------
The clinic's basic details do not change during a call, so making the model fetch
them mid-sentence buys nothing and costs correctness. Measured on the live stream,
Nova Sonic emits ``TOOL_USE`` and then reaches ``END_TURN`` *before* the tool
executes — the result arrives after it has stopped speaking. Whatever it said in
that turn was therefore ungrounded, and with nothing to go on it produced the
statistically obvious answer instead: "Monday through Friday, 8:00 AM to 5:00 PM"
for a clinic whose document says Monday to Saturday.

No prompt wording fixes that ordering, because the model cannot repeat what it has
not yet received. So the static facts are handed to it up front, in the system
prompt, where they are available the instant it starts talking.

This does not replace the tools. Retrieval still answers the descriptive long tail
and anything the briefing had to truncate, and the tools remain the only path for
anything that can change during a call — slots, bookings, patient records. What
moves into the prompt is only the part that is genuinely static.

Two rules the briefing must not break
-------------------------------------
**Configuration outranks documents.** The doctor entered and confirmed the
configured values; a document is prose that happens to mention something. So
configured facts are stated first and labelled as authoritative, and the model is
told to prefer them.

**Clinical passages are excluded.** A practice handout mixes logistics with
clinical guidance. Pasting the whole corpus into the prompt would put dosing
instructions one question away from a caller's ear, with no tool boundary left to
screen them. Every passage is filtered through
:func:`~clinic_front_desk.documents.retrieval.looks_clinical` first — the same
deterministic check the retrieval path uses.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import quote

from clinic_front_desk.data_layer.interfaces import (
    AppointmentStore,
    ClinicDocumentStore,
    ClinicKnowledgeBaseStore,
)
from clinic_front_desk.models import ClinicKnowledgeBase, format_money, is_err

logger = logging.getLogger(__name__)

#: Weekday names indexed to match ``ClinicKnowledgeBase.hours`` keys.
_WEEKDAYS = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)

#: Budget for the document half of the briefing.
#:
#: A system prompt is charged on every turn of every call, and Nova Sonic has its
#: own limit, so the whole corpus cannot go in unconditionally. A practice
#: information sheet fits comfortably; a 40-page policy binder does not, and what
#: does not fit stays reachable through retrieval rather than being lost.
MAX_DOCUMENT_CHARS = 6000

_HEADER = "WHAT YOU KNOW ABOUT THIS CLINIC"

_PREAMBLE = """\
Everything in this section is the clinic's own information, already given to you.
It is your own knowledge, not something to look up.

Do NOT call answer_faq for anything written here. You already have it, so calling a
tool only delays the caller. Answer from this section immediately, in the same
breath, in your own words.

Say these details exactly as written here. Do not round a time, reformat a date,
shorten an address, or change a value because it looks unusual to you — an unusual
value is still the clinic's actual answer.

If something is not in this section, you do not know it. Use your tools for
anything else, and if a tool reports the information is unavailable, say the clinic
has not provided it and offer to take a message. Never substitute a likely-sounding
answer of your own."""

_CONFIG_HEADER = "Confirmed clinic details (these outrank anything below):"
_CALENDAR_HEADER = "The booking calendar (read this before you answer about a date):"
_DOCUMENT_HEADER = "From the clinic's own documents:"
_TRUNCATION_NOTE = (
    "(Some document sections are not shown here. If a caller asks about something "
    "not listed above, use answer_faq with topic clinic_info to look it up.)"
)


def _format_hours(kb: ClinicKnowledgeBase) -> str | None:
    """Opening hours as one spoken-friendly line, or ``None`` if unconfigured."""
    parts = [
        f"{_WEEKDAYS[day]} {hours.open} to {hours.close}"
        for day in range(len(_WEEKDAYS))
        if (hours := kb.hours.get(day)) is not None
    ]
    return "; ".join(parts) if parts else None


def _config_lines(kb: ClinicKnowledgeBase) -> list[str]:
    """The configured facts worth stating up front."""
    lines: list[str] = []
    if kb.location and kb.location.strip():
        lines.append(f"- Address: {kb.location.strip()}")

    hours = _format_hours(kb)
    if hours:
        lines.append(f"- Opening hours: {hours}")

    # The number to give a caller the agent cannot finish helping. Stated as the only
    # number it may say, because the alternative is a model reaching for a
    # plausible-looking one — and a wrong phone number is worse than none: the caller
    # rings it, reaches a stranger, and still has not reached the clinic.
    if kb.contact_phone and kb.contact_phone.strip():
        lines.append(
            f"- Clinic phone number: {kb.contact_phone.strip()} — give this when the "
            "caller asks how to reach the clinic, or when you cannot help them "
            "yourself. Never say any other number."
        )

    if kb.services:
        named = ", ".join(service.name for service in kb.services if service.name)
        if named:
            lines.append(f"- Services offered (exact names): {named}")
        priced = [
            f"{service.name} {format_money(service.price)}"
            for service in kb.services
            if service.name and service.price is not None
        ]
        if priced:
            lines.append(f"- Prices: {'; '.join(priced)}")
        for service in kb.services:
            prep = (service.prep_instructions or "").strip()
            if service.name and prep:
                lines.append(f"- Preparation for {service.name}: {prep}")

    accepted = [name for name in kb.accepted_insurance if name and name.strip()]
    if accepted:
        lines.append(f"- Accepted insurance: {', '.join(accepted)}")

    if kb.providers:
        names = ", ".join(p.name for p in kb.providers if p.name)
        if names:
            lines.append(f"- Providers: {names}")
    return lines


def _document_passages(
    store: ClinicDocumentStore, *, max_chars: int
) -> tuple[list[str], bool]:
    """Non-clinical document passages that fit the budget, and whether any were cut."""
    from clinic_front_desk.documents.retrieval import looks_clinical

    result = store.list_chunks()
    if is_err(result):
        logger.warning("clinic briefing could not read documents: %s", result.error.detail)
        return [], False

    kept: list[str] = []
    used = 0
    truncated = False
    for chunk in result.value:
        text = " ".join(chunk.text.split())
        if not text:
            continue
        if looks_clinical(text):
            # The prompt has no tool boundary left to screen this, so it must not
            # go in at all.
            logger.info(
                "clinic briefing skipped a clinical passage (%s#%d)",
                chunk.document_id,
                chunk.index,
            )
            continue
        if used + len(text) > max_chars:
            truncated = True
            continue
        kept.append(f"- {text}")
        used += len(text)
    return kept, truncated


def _calendar_lines(
    appointments: AppointmentStore, kb: ClinicKnowledgeBase | None
) -> list[str]:
    """What the agent must know about the booking calendar before it speaks.

    Not the slot times — those change during a call and are what
    ``check_availability`` is for. This is the *shape* of the calendar: how far it
    is published and which days are open. That does not change mid-call, and it is
    the fact the agent was missing when it turned a caller away.

    Measured cause: ``check_availability`` took 8.1 s against a year of published
    slots, while Nova Sonic runs tool calls concurrently with speech and had
    already finished its turn. With nothing in hand it produced the plausible
    answer — "there are no open slots on September 10" — for a day holding 48 of
    them. Faster queries narrow that window but cannot close it, because nothing
    in this system decides when the model stops talking. Stating the span up front
    removes the need to guess: the agent can tell that a named date is inside the
    published calendar without waiting for anything.
    """
    providers = [p.id for p in kb.providers if p.id] if kb is not None else []
    if not providers:
        return []

    today = datetime.now(UTC).date().isoformat()
    spans = []
    for provider_id in providers:
        result = appointments.open_slot_span(provider_id, today)
        if is_err(result):
            logger.warning(
                "clinic briefing could not read the calendar for %s: %s",
                provider_id,
                result.error.detail,
            )
            continue
        if result.value is not None:
            spans.append(result.value)

    if not spans:
        # No open time anywhere. Say so plainly rather than staying silent, or the
        # agent will keep offering to book against an unpublished calendar.
        return [
            "- The booking calendar has no open slots published at all right now. "
            "Do not offer to book. Take the caller's details and say reception "
            "will call them back to arrange a time."
        ]

    earliest = min(span.earliest_start for span in spans)
    latest = max(span.latest_start for span in spans)
    lines = [
        f"- The booking calendar is published and has open slots from "
        f"{earliest[:10]} through {latest[:10]}.",
        "- Any date in that range is a published, bookable day unless "
        "check_availability tells you otherwise. Never tell a caller a date in "
        "that range has nothing free until the tool has actually said so.",
        f"- Outside {earliest[:10]} to {latest[:10]} nothing is published yet, so "
        "there is genuinely nothing to book.",
    ]

    if kb is not None and kb.symptom_routes:
        urgent_phrases = [
            phrase
            for route in kb.symptom_routes
            if route.urgent
            for phrase in route.phrases
        ]
        # Told up front so the agent does not answer a described symptom from the
        # generic script before it thinks to look. The routing itself is NOT listed
        # here on purpose: it must be read through suggest_service_for_problem so the
        # doctor's exact wording is what reaches the caller, rather than a paraphrase
        # the model reconstructs from a briefing it read minutes earlier.
        lines.append(
            "- The doctor has written her own routing for described symptoms. When a "
            "caller says what is wrong instead of naming a service, ALWAYS call "
            "suggest_service_for_problem with their words and relay what it returns. "
            "Do not answer a described symptom without calling it first."
        )
        if urgent_phrases:
            lines.append(
                "- Some problems are marked as needing attention sooner than the next "
                f"free slot (for example: {', '.join(urgent_phrases[:6])}). For those "
                "the tool returns urgent and you must NOT offer an appointment."
            )

    open_days = _open_weekday_names(kb)
    if open_days:
        lines.append(f"- Days the clinic has hours for: {', '.join(open_days)}.")
        shut = [day for day in _WEEKDAYS if day not in open_days]
        if shut:
            # Naming the closed days explicitly, rather than leaving the agent to
            # subtract one list from another mid-call. A caller asking for a closed
            # day was being told "I could not find anything", which reads as fully
            # booked and sends them away instead of to the next working day.
            joined = ", ".join(shut)
            lines.append(
                f"- {joined} is the clinic's HOLIDAY. The clinic is closed and "
                "there are no slots on those days, ever. If a caller names a date "
                f"that falls on {joined}, tell them that date is a {shut[0]} and "
                "the clinic is closed for its holiday, then offer the nearest "
                "working day. Never answer a closed day with 'I could not find "
                "anything' — that sounds fully booked, and they will give up "
                "instead of taking the next working day."
            )
    return lines


def _open_weekday_names(kb: ClinicKnowledgeBase | None) -> list[str]:
    """Weekday names the clinic has configured hours for."""
    if kb is None:
        return []
    return [_WEEKDAYS[day] for day in range(len(_WEEKDAYS)) if kb.hours.get(day)]


def build_clinic_briefing(
    knowledge_base: ClinicKnowledgeBaseStore | None = None,
    documents: ClinicDocumentStore | None = None,
    *,
    appointments: AppointmentStore | None = None,
    max_document_chars: int = MAX_DOCUMENT_CHARS,
) -> str:
    """Build the clinic-knowledge section for the system prompt.

    Args:
        knowledge_base: Read for the confirmed configuration, which is stated
            first and marked as outranking the documents.
        documents: Read for the uploaded practice information.
        appointments: Read for how far the booking calendar is published. Omitted
            means the calendar section is left out entirely.
        max_document_chars: Budget for the document half.

    Returns:
        A prompt section, or an empty string when there is nothing to say — so an
        unconfigured clinic with no uploads gets no empty scaffolding.
    """
    kb: ClinicKnowledgeBase | None = None
    config_lines: list[str] = []
    if knowledge_base is not None:
        result = knowledge_base.get()
        if is_err(result):
            logger.warning(
                "clinic briefing could not read configuration: %s", result.error.detail
            )
        elif result.value is not None:
            kb = result.value
            config_lines = _config_lines(kb)

    calendar_lines: list[str] = []
    if appointments is not None:
        calendar_lines = _calendar_lines(appointments, kb)

    document_lines: list[str] = []
    truncated = False
    if documents is not None:
        document_lines, truncated = _document_passages(
            documents, max_chars=max_document_chars
        )

    if not config_lines and not document_lines and not calendar_lines:
        return ""

    sections = [f"\n\n{_HEADER}\n{_PREAMBLE}"]
    if config_lines:
        sections.append(f"\n{_CONFIG_HEADER}\n" + "\n".join(config_lines))
    if calendar_lines:
        sections.append(f"\n{_CALENDAR_HEADER}\n" + "\n".join(calendar_lines))
    if document_lines:
        sections.append(f"\n{_DOCUMENT_HEADER}\n" + "\n".join(document_lines))
    if truncated:
        sections.append(f"\n{_TRUNCATION_NOTE}")
    return "\n".join(sections)


def with_clinic_briefing(
    prompt: str,
    knowledge_base: ClinicKnowledgeBaseStore | None = None,
    documents: ClinicDocumentStore | None = None,
    *,
    appointments: AppointmentStore | None = None,
    max_document_chars: int = MAX_DOCUMENT_CHARS,
) -> str:
    """Return ``prompt`` with the clinic briefing appended, if there is one."""
    return prompt + build_clinic_briefing(
        knowledge_base,
        documents,
        appointments=appointments,
        max_document_chars=max_document_chars,
    )


#: Matches a decimal coordinate pair in document text, e.g.
#: "13.62849 degrees north, 79.46382 degrees east" or "13.62849, 79.46382".
_COORDINATES = re.compile(
    r"(-?\d{1,3}\.\d{4,})\s*(?:degrees\s+)?(?:north|n)?\s*[,;]?\s*"
    r"(-?\d{1,3}\.\d{4,})\s*(?:degrees\s+)?(?:east|e)?",
    re.IGNORECASE,
)

#: Section headings whose text is the clinic's location, best-first.
_LOCATION_HEADINGS = ("where to find us", "directions", "address", "how to find us")


def _without_heading(text: str) -> str:
    """A passage with its section heading removed, whitespace collapsed.

    Chunks keep their heading because it carries retrieval signal, but a card
    displaying "Where to find us The address is..." reads like a bug, so the
    heading comes off for presentation.
    """
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) > 1:
        first = lines[0].strip()
        if len(first) <= 45 and "," not in first and not first.endswith((".", "!", "?")):
            lines = lines[1:]
    return " ".join(" ".join(lines).split())


def _maps_url(*, address: str, coordinates: tuple[str, str] | None) -> str:
    """A Google Maps URL for the clinic.

    Coordinates win when the document states them, because a pin is exact where a
    text search is a guess. Otherwise a search URL built from the address, which
    needs nothing stored and stays correct if the address changes.
    """
    if coordinates is not None:
        latitude, longitude = coordinates
        return f"https://www.google.com/maps/search/?api=1&query={latitude},{longitude}"
    return (
        "https://www.google.com/maps/search/?api=1&query="
        + quote(address, safe="")
    )


def build_clinic_card(
    knowledge_base: ClinicKnowledgeBaseStore | None = None,
    documents: ClinicDocumentStore | None = None,
) -> dict[str, str] | None:
    """The clinic's address and a map link, for the client to display.

    Sent to the caller's client when the call connects, not fetched mid-answer.
    That matters for two reasons. A map link cannot be *spoken* — a caller cannot
    transcribe a hundred characters of percent-encoded URL from audio — so it has
    to arrive as something they can tap. And building it at connect time avoids
    the tool-result timing problem entirely: it is on screen before the first
    question is asked.

    Returns ``None`` when the clinic has no address anywhere, so the client shows
    nothing rather than an empty card or a link to nowhere.
    """
    address = ""
    if knowledge_base is not None:
        result = knowledge_base.get()
        if not is_err(result) and result.value is not None:
            address = (result.value.location or "").strip()

    directions = ""
    coordinates: tuple[str, str] | None = None
    if documents is not None:
        chunks = documents.list_chunks()
        if is_err(chunks):
            logger.warning("clinic card could not read documents: %s", chunks.error.detail)
        else:
            passages = [_without_heading(chunk.text) for chunk in chunks.value]
            for heading in _LOCATION_HEADINGS:
                for raw, body in zip(
                    (c.text for c in chunks.value), passages, strict=True
                ):
                    if raw.strip().lower().startswith(heading):
                        directions = directions or body
                        break
                if directions:
                    break
            for text in passages:
                match = _COORDINATES.search(text)
                if match is not None:
                    coordinates = (match.group(1), match.group(2))
                    break

    # The configured address is authoritative; the document's directions text is
    # the fallback and also the richer description when both exist.
    display = address or directions
    if not display and coordinates is None:
        return None

    card: dict[str, str] = {
        "address": display or "",
        "maps_url": _maps_url(address=display, coordinates=coordinates),
    }
    if directions and directions != display:
        card["directions"] = directions
    if coordinates is not None:
        card["coordinates"] = f"{coordinates[0]}, {coordinates[1]}"
    return card


__all__ = [
    "MAX_DOCUMENT_CHARS",
    "build_clinic_briefing",
    "build_clinic_card",
    "with_clinic_briefing",
]
