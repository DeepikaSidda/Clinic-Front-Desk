"""Reading structured clinic configuration out of an uploaded document.

This is the other half of what an upload is for. :mod:`.retrieval` answers
descriptive questions straight from the text; this module tries to fill in the
*structured* fields — location, hours, services, insurance, providers — so the
doctor confirms a pre-filled onboarding form instead of typing everything a second
time from a document they already wrote.

**Nothing here saves anything.** The output is a candidate plus a list of notes,
handed to the wizard for review. The doctor pressing "Save configuration" is what
persists it, through the same :func:`~clinic_front_desk.config.save_clinic_config`
path as hand-entered values. That is deliberate and load-bearing: these fields
*drive behaviour* — offered-service names gate what the agent will book, prices are
quoted as the clinic's word, provider schedules decide which slots exist — so a
model's reading of a PDF must never become the clinic's configuration without a
human agreeing to it.

Three properties keep the candidate trustworthy enough to be worth reviewing.

**The model transcribes; it does not compose.** Output is constrained to a tool
schema (Bedrock ``converse`` with ``toolChoice``), so there is no prose to parse and
no room for commentary.

**Every extracted value must appear in the document.** :func:`_grounded` re-checks
each name, price and instruction against the source text and drops anything that is
not there, recording it in the notes. This is what catches the model filling a
plausible gap: in testing it confidently gave a provider working hours the document
never mentioned. Grounding removes that class of error mechanically rather than
relying on the prompt to discourage it.

**Weekdays are transcribed as names, never as numbers.** Asked for a numeric
weekday index, the model returned Friday as ``4`` and read "Monday through Thursday"
as three days. Asked for ``"friday"``, both were correct. The arithmetic is done
here, where it is deterministic.

Provider *hours* are not extracted at all, only which days a provider works. Hours
determine which appointment slots exist, and a wrong schedule produces offers for
appointments that cannot happen. The doctor knows their own hours; a document rarely
states them per provider, and the one time the model was asked it invented them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from clinic_front_desk.config.validation import (
    ConfigValidationResult,
    validate_config,
)
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    DayHours,
    Provider,
    ScheduleRule,
    ServiceConfig,
)

logger = logging.getLogger(__name__)

#: Amazon Nova Lite: cheap, fast, and supports tool-constrained output, which is
#: all this needs. Transcribing a page of text into fields is not a task that
#: rewards a larger model, and this runs while a doctor waits on an upload.
DEFAULT_EXTRACTION_MODEL_ID = "amazon.nova-lite-v1:0"

#: How much document text to send. Clinic details live in the first page or two of
#: a practice sheet, and the whole point is a quick pre-fill, not an exhaustive
#: read of a 40-page policy binder. Truncation is reported in the notes.
MAX_EXTRACT_CHARS = 24_000

#: Weekday name -> index, matching ``ClinicKnowledgeBase.hours`` keys and
#: ``ScheduleRule.day_of_week`` (0 = Sunday).
DAY_INDEX: dict[str, int] = {
    "sunday": 0,
    "monday": 1,
    "tuesday": 2,
    "wednesday": 3,
    "thursday": 4,
    "friday": 5,
    "saturday": 6,
}

_DAY_NAMES = list(DAY_INDEX)

#: The name of the tool the model is forced to call. Only its schema matters; it is
#: never executed.
TOOL_NAME = "record_clinic_config"

_DAY_ENUM: dict[str, Any] = {"type": "string", "enum": _DAY_NAMES}

#: The output contract. Constraining the model to this shape is what removes JSON
#: parsing and prose handling from this module entirely.
CONFIG_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "location": {
            "type": "string",
            "description": (
                "The clinic's full street address exactly as written. "
                "Empty string if the document does not give one."
            ),
        },
        "hours": {
            "type": "array",
            "description": (
                "One entry per day the CLINIC is open. Omit days it is closed. "
                "These are the clinic's overall opening hours, not one person's."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "day": _DAY_ENUM,
                    "open": {"type": "string", "description": "24-hour HH:MM"},
                    "close": {"type": "string", "description": "24-hour HH:MM"},
                },
                "required": ["day", "open", "close"],
            },
        },
        "services": {
            "type": "array",
            "description": "Services the clinic offers, named exactly as written.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "price": {
                        "type": "string",
                        "description": (
                            "The price as digits only, e.g. 150 or 150.00. "
                            "Empty string if the document does not state a price."
                        ),
                    },
                    "prep_instructions": {
                        "type": "string",
                        "description": (
                            "Any preparation instructions for this service, copied "
                            "word for word. Empty string if there are none."
                        ),
                    },
                },
                "required": ["name"],
            },
        },
        "accepted_insurance": {
            "type": "array",
            "description": "Insurance plans accepted, named exactly as written.",
            "items": {"type": "string"},
        },
        "providers": {
            "type": "array",
            "description": "The clinicians the document names.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "specialty": {"type": "string"},
                    "days": {
                        "type": "array",
                        "description": (
                            "The days THIS provider works, only if the document "
                            "states them for this person. Otherwise omit."
                        ),
                        "items": _DAY_ENUM,
                    },
                },
                "required": ["name"],
            },
        },
    },
    "required": ["location", "hours", "services", "accepted_insurance", "providers"],
}

_SYSTEM_PROMPT = (
    "You transcribe clinic details from a document into a structured form for a "
    "receptionist to check. You are a transcriber, not an assistant: copy what the "
    "document says and nothing more. If the document does not state something, "
    "leave it out or empty rather than supplying a sensible default. Never infer a "
    "price, a service, or a person's working hours."
)

_USER_PROMPT = (
    "Extract this clinic's details from the document below.\n\n"
    "Rules:\n"
    "- Copy service names, provider names, insurance names and prices exactly as "
    "written, including capitalisation.\n"
    "- Under 'hours', list only days the clinic is open.\n"
    "- Give a provider's working days only if the document states them for that "
    "specific person.\n"
    "- If a value is not in the document, leave it out. Do not guess.\n\n"
    "<document>\n{text}\n</document>"
)


class ConfigExtractor(Protocol):
    """Turns document text into the raw schema dict. Narrow, so tests can fake it."""

    def extract(self, text: str) -> dict[str, Any]:
        """Return the model's structured reading of ``text``."""
        ...


class BedrockConfigExtractor:
    """Extracts clinic configuration with a tool-constrained Bedrock model.

    Args:
        client: A ``bedrock-runtime`` client, injected so nothing here constructs
            AWS clients and tests never reach the network.
        model_id: The model to use. Must support ``converse`` tool use.
    """

    def __init__(
        self, client: Any, *, model_id: str = DEFAULT_EXTRACTION_MODEL_ID
    ) -> None:
        self._client = client
        self._model_id = model_id

    def extract(self, text: str) -> dict[str, Any]:
        response = self._client.converse(
            modelId=self._model_id,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[
                {
                    "role": "user",
                    "content": [{"text": _USER_PROMPT.format(text=text)}],
                }
            ],
            toolConfig={
                "tools": [
                    {
                        "toolSpec": {
                            "name": TOOL_NAME,
                            "description": (
                                "Record the clinic configuration stated in the "
                                "document."
                            ),
                            "inputSchema": {"json": CONFIG_SCHEMA},
                        }
                    }
                ],
                # Forcing the tool is what guarantees a schema-shaped answer instead
                # of prose that happens to contain JSON.
                "toolChoice": {"tool": {"name": TOOL_NAME}},
            },
            # Transcription has one right answer; sampling would only invent
            # variation where none is wanted.
            inferenceConfig={"temperature": 0.0, "maxTokens": 3000},
        )
        for block in response["output"]["message"]["content"]:
            if "toolUse" in block:
                payload = block["toolUse"].get("input")
                if isinstance(payload, dict):
                    return payload
        return {}


@dataclass
class ExtractedConfig:
    """A candidate configuration read from a document, for the doctor to confirm.

    Attributes:
        candidate: The configuration to pre-fill the wizard with. Never saved by
            this module.
        validation: The result of the *same* validation a hand-entered config
            faces, so gaps are known before the doctor submits.
        notes: Plain-language remarks for the doctor — what was found, what was
            dropped for not appearing in the document, what still needs entering.
        error: Set when extraction could not run at all.
    """

    candidate: ClinicKnowledgeBase
    validation: ConfigValidationResult
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Whether extraction produced something worth showing the doctor."""
        return self.error is None and self.found_anything

    @property
    def found_anything(self) -> bool:
        """Whether any field was populated at all."""
        kb = self.candidate
        return bool(
            kb.location
            or any(v is not None for v in kb.hours.values())
            or kb.services
            or kb.accepted_insurance
            or kb.providers
        )

    @property
    def complete(self) -> bool:
        """Whether the candidate would pass validation as-is (Req 1.5).

        Passing validation is not the same as being ready to take calls: provider
        working hours are never extracted, and a provider with no hours has no
        bookable slots even though the configuration is valid. The notes say so.
        """
        return self.validation.ok


# ---------------------------------------------------------------------------
# Grounding — the check that a value is actually in the document.
# ---------------------------------------------------------------------------


def _comparable(text: str) -> str:
    """Casefold and collapse whitespace/punctuation for tolerant containment."""
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def _grounded(value: str, source: str) -> bool:
    """Whether ``value`` appears in ``source``, ignoring case and punctuation.

    Formatting differences are forgiven (the model re-spaces and re-punctuates
    freely); invented content is not. This is the mechanical stop on the model
    supplying a plausible value the document never contained.
    """
    needle = _comparable(value)
    return bool(needle) and needle in _comparable(source)


def _grounded_loosely(value: str, source: str, *, ratio: float = 0.7) -> bool:
    """Whether most of ``value``'s words appear in ``source``.

    Used for the address only. A model reliably reorders an address's parts
    ("Suite 302, 123 Main St" for "123 Main St, Suite 302"), so demanding a
    contiguous match would reject correct readings; demanding nothing would accept
    an invented street. Requiring most of the words to be present separates those.
    """
    words = _comparable(value).split()
    if not words:
        return False
    haystack = set(_comparable(source).split())
    hits = sum(1 for word in words if word in haystack)
    return hits / len(words) >= ratio


_TIME_PATTERNS = (
    re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ap>am|pm)?$", re.IGNORECASE),
    re.compile(r"^(?P<h>\d{1,2})\s*(?P<ap>am|pm)$", re.IGNORECASE),
)


def normalize_time(raw: str) -> str | None:
    """Normalize a time to zero-padded 24-hour ``HH:MM``, or ``None`` if unreadable.

    Nothing downstream parses or format-checks these strings — hours are compared
    and displayed as-is — so ``"9:00"`` or ``"9am"`` reaching the store would be
    silently wrong rather than loudly wrong. The model produced ``"9:00"`` on the
    first probe, so this is a live concern, not a defensive flourish.
    """
    text = raw.strip()
    if not text:
        return None
    for pattern in _TIME_PATTERNS:
        match = pattern.match(text)
        if match is None:
            continue
        # groupdict, not group(): the bare "9am" pattern has no minute group at
        # all, and group("m") raises rather than returning None for that.
        groups = match.groupdict()
        hour = int(groups["h"])
        minute = int(groups.get("m") or 0)
        meridiem = (groups.get("ap") or "").lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return None
    return None


def _price(raw: str) -> float | None:
    """Parse a price to a float, or ``None`` when absent/unreadable."""
    cleaned = re.sub(r"[^0-9.]", "", raw.strip())
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Assembling the candidate.
# ---------------------------------------------------------------------------


def _build_hours(
    raw: Any, notes: list[str]
) -> dict[int, DayHours | None]:
    """Build the weekday -> hours map, defaulting every unlisted day to closed."""
    hours: dict[int, DayHours | None] = {day: None for day in range(7)}
    if not isinstance(raw, list):
        return hours
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        day = DAY_INDEX.get(str(entry.get("day", "")).strip().casefold())
        if day is None:
            continue
        open_ = normalize_time(str(entry.get("open", "")))
        close = normalize_time(str(entry.get("close", "")))
        if open_ is None or close is None:
            # An open day with unreadable times is worse than no entry: it would
            # look configured while comparing wrongly. Report it instead.
            if entry.get("open") or entry.get("close"):
                notes.append(
                    f"Could not read the opening times for "
                    f"{_DAY_NAMES[day].capitalize()} — please enter them."
                )
            continue
        hours[day] = DayHours(open=open_, close=close)
    return hours


def _build_services(raw: Any, source: str, notes: list[str]) -> list[ServiceConfig]:
    """Build offered services, dropping any name or price not in the document."""
    services: list[ServiceConfig] = []
    if not isinstance(raw, list):
        return services
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        if not _grounded(name, source):
            notes.append(
                f"Ignored the service {name!r} because it does not appear in the "
                f"document."
            )
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)

        price_raw = str(entry.get("price", "") or "")
        price = _price(price_raw)
        if price is not None and not _grounded(price_raw, source):
            notes.append(
                f"Left the price for {name!r} blank because the figure does not "
                f"appear in the document."
            )
            price = None

        prep = str(entry.get("prep_instructions", "") or "").strip()
        if prep and not _grounded(prep, source):
            notes.append(
                f"Left the preparation instructions for {name!r} blank because "
                f"they do not appear in the document."
            )
            prep = ""

        services.append(
            ServiceConfig(name=name, prep_instructions=prep or None, price=price)
        )
    return services


def _build_providers(raw: Any, source: str, notes: list[str]) -> list[Provider]:
    """Build providers with days but deliberately no hours (see module docstring)."""
    providers: list[Provider] = []
    if not isinstance(raw, list):
        return providers
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", "")).strip()
        if not name:
            continue
        if not _grounded(name, source):
            notes.append(
                f"Ignored the provider {name!r} because that name does not appear "
                f"in the document."
            )
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)

        specialty = str(entry.get("specialty", "") or "").strip()
        if specialty and not _grounded(specialty, source):
            specialty = ""

        days_raw = entry.get("days")
        days: list[int] = []
        if isinstance(days_raw, list):
            for value in days_raw:
                day = DAY_INDEX.get(str(value).strip().casefold())
                if day is not None and day not in days:
                    days.append(day)
        # No start/end: a provider's hours are a scheduling commitment and are the
        # doctor's to enter. The days survive as a hint, inert until hours are set.
        schedule = [
            ScheduleRule(day_of_week=day, start="", end="") for day in sorted(days)
        ]
        providers.append(
            Provider(
                id=_slug(name) or f"provider-{len(providers) + 1}",
                name=name,
                specialty=specialty,
                schedule=schedule,
            )
        )
    return providers


def _slug(text: str) -> str:
    """Lowercase hyphenated id derived from ``text`` (matches the wizard's rule)."""
    return re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")


def _build_insurance(raw: Any, source: str, notes: list[str]) -> list[str]:
    """Build the accepted-insurance list, dropping plans not in the document."""
    plans: list[str] = []
    if not isinstance(raw, list):
        return plans
    for value in raw:
        name = str(value).strip()
        if not name:
            continue
        if not _grounded(name, source):
            notes.append(
                f"Ignored the insurance plan {name!r} because it does not appear "
                f"in the document."
            )
            continue
        if name.casefold() not in {p.casefold() for p in plans}:
            plans.append(name)
    return plans


def extract_clinic_config(
    text: str,
    extractor: ConfigExtractor,
    *,
    source_label: str = "",
) -> ExtractedConfig:
    """Read a candidate :class:`ClinicKnowledgeBase` out of document ``text``.

    Args:
        text: The document's full text (from
            :func:`~clinic_front_desk.documents.text.extract_text`).
        extractor: The model wrapper that returns the structured reading.
        source_label: The filename, used only in the notes shown to the doctor.

    Returns:
        An :class:`ExtractedConfig` carrying the candidate, its validation result,
        and notes. Never raises for a document it cannot read, and never persists
        anything.
    """
    notes: list[str] = []
    blank = ClinicKnowledgeBase(location="", hours={day: None for day in range(7)})

    body = text.strip()
    if not body:
        return ExtractedConfig(
            candidate=blank,
            validation=validate_config(blank),
            error="the document contained no text to read",
        )

    if len(body) > MAX_EXTRACT_CHARS:
        body = body[:MAX_EXTRACT_CHARS]
        notes.append(
            "Only the first part of this document was read, so later sections may "
            "not be reflected here."
        )

    try:
        raw = extractor.extract(body)
    except Exception as exc:  # noqa: BLE001 - a failed read must not fail the upload
        logger.warning("config extraction failed for %s: %s", source_label or "?", exc)
        return ExtractedConfig(
            candidate=blank,
            validation=validate_config(blank),
            notes=notes,
            error="the document could not be read for clinic details",
        )

    location = str(raw.get("location", "") or "").strip()
    if location and not _grounded_loosely(location, body):
        notes.append(
            "Left the address blank because the one suggested does not appear in "
            "the document."
        )
        location = ""

    candidate = ClinicKnowledgeBase(
        location=location,
        hours=_build_hours(raw.get("hours"), notes),
        services=_build_services(raw.get("services"), body, notes),
        accepted_insurance=_build_insurance(raw.get("accepted_insurance"), body, notes),
        providers=_build_providers(raw.get("providers"), body, notes),
    )

    validation = validate_config(candidate)
    result = ExtractedConfig(candidate=candidate, validation=validation, notes=notes)

    if result.found_anything:
        # Say what still needs doing in the doctor's terms. The raw violation
        # details read as rejections of something they typed, which is the wrong
        # framing for a form nobody has filled in yet.
        missing = validation.missing_required_fields
        if missing:
            notes.append(
                "The document did not give: "
                + ", ".join(_MISSING_LABELS.get(f, f) for f in missing)
                + ". Please add these before saving."
            )
        if candidate.providers and not any(
            rule.start for prov in candidate.providers for rule in prov.schedule
        ):
            notes.append(
                "Provider working hours are never taken from a document — please "
                "set each provider's start and end times."
            )
    else:
        result.error = "no clinic details could be found in this document"

    return result


_MISSING_LABELS = {
    "hours": "clinic opening hours",
    "location": "the clinic address",
    "services": "at least one offered service",
    "providers": "at least one provider",
}


def create_config_extractor(
    *, region: str | None = None, model_id: str = DEFAULT_EXTRACTION_MODEL_ID
) -> BedrockConfigExtractor:
    """Build a :class:`BedrockConfigExtractor`, creating a boto3 client lazily."""
    import boto3  # type: ignore[import-untyped]

    return BedrockConfigExtractor(
        boto3.client("bedrock-runtime", region_name=region), model_id=model_id
    )


__all__ = [
    "DEFAULT_EXTRACTION_MODEL_ID",
    "MAX_EXTRACT_CHARS",
    "DAY_INDEX",
    "TOOL_NAME",
    "CONFIG_SCHEMA",
    "ConfigExtractor",
    "BedrockConfigExtractor",
    "ExtractedConfig",
    "extract_clinic_config",
    "create_config_extractor",
    "normalize_time",
]
