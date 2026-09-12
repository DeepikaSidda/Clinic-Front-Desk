"""Unit tests for structured config extraction (``documents/extraction.py``).

Two themes. First, the model's output is not trusted: every value is re-checked
against the document text, and anything absent is dropped with a note. Second,
values are normalized here rather than downstream — nothing else in the system
format-checks ``"HH:MM"``, so ``"9am"`` reaching the store would be silently wrong.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from clinic_front_desk.documents.extraction import (
    CONFIG_SCHEMA,
    DAY_INDEX,
    MAX_EXTRACT_CHARS,
    TOOL_NAME,
    BedrockConfigExtractor,
    extract_clinic_config,
    normalize_time,
)

SHEET = """Springfield Hearing Clinic

We are located at 123 Main Street, Suite 302, Springfield.

Office hours
Monday to Thursday 9:00 am to 5:00 pm. Friday 9:00 am to 1:00 pm.

Services we offer
Hearing Test - $150
Allergy Consultation - $220
Please avoid loud noise for 24 hours before a Hearing Test.

Our providers
Dr. Alice Nguyen, Audiologist, sees patients Monday through Thursday.

Insurance
We accept Aetna and Medicare.
"""

FULL_READING: dict[str, Any] = {
    "location": "123 Main Street, Suite 302, Springfield",
    "hours": [
        {"day": "monday", "open": "9:00 am", "close": "5:00 pm"},
        {"day": "friday", "open": "9:00 am", "close": "1:00 pm"},
    ],
    "services": [
        {
            "name": "Hearing Test",
            "price": "150",
            "prep_instructions": "Please avoid loud noise for 24 hours before a Hearing Test.",
        },
        {"name": "Allergy Consultation", "price": "220"},
    ],
    "accepted_insurance": ["Aetna", "Medicare"],
    "providers": [
        {
            "name": "Dr. Alice Nguyen",
            "specialty": "Audiologist",
            "days": ["monday", "tuesday", "wednesday", "thursday"],
        }
    ],
}


class Reading:
    """Returns a fixed model reading, so grounding can be tested in isolation."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.seen: list[str] = []

    def extract(self, text: str) -> dict[str, Any]:
        self.seen.append(text)
        return self.payload


class Exploding:
    def extract(self, text: str) -> dict[str, Any]:
        raise RuntimeError("bedrock refused the request")


def _extract(payload: dict[str, Any], text: str = SHEET) -> Any:
    return extract_clinic_config(text, Reading(payload), source_label="info.txt")


# ---------------------------------------------------------------------------
# normalize_time
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09:00", "09:00"),
        ("9:00", "09:00"),  # what the model actually returned on the first probe
        ("9am", "09:00"),
        ("9 AM", "09:00"),
        ("9:30 am", "09:30"),
        ("5:30 pm", "17:30"),
        ("5pm", "17:00"),
        ("17:00", "17:00"),
        ("12am", "00:00"),
        ("12pm", "12:00"),
        ("12:15 am", "00:15"),
        ("00:00", "00:00"),
        ("23:59", "23:59"),
        ("  9:00  ", "09:00"),
    ],
)
def test_times_are_normalized_to_zero_padded_24_hour(raw: str, expected: str) -> None:
    assert normalize_time(raw) == expected


@pytest.mark.parametrize(
    "raw", ["", "   ", "noon", "midday", "25:00", "9:99", "-1:00", "half past nine",
            "9:00:00", "abc"]
)
def test_an_unreadable_time_is_rejected_rather_than_guessed(raw: str) -> None:
    assert normalize_time(raw) is None


# ---------------------------------------------------------------------------
# The schema the model is held to
# ---------------------------------------------------------------------------


def test_weekdays_are_constrained_to_names_not_numbers() -> None:
    # Asked for a numeric index the model read Friday as 4 and "Monday through
    # Thursday" as three days; with names both were right. The arithmetic belongs
    # here, where it is deterministic.
    day_field = CONFIG_SCHEMA["properties"]["hours"]["items"]["properties"]["day"]

    assert day_field["type"] == "string"
    assert day_field["enum"] == list(DAY_INDEX)


def test_provider_hours_are_not_in_the_schema_at_all() -> None:
    # Provider hours decide which appointment slots exist. The model invented them
    # when asked, and a wrong schedule produces offers for appointments that
    # cannot happen.
    provider_fields = CONFIG_SCHEMA["properties"]["providers"]["items"]["properties"]

    assert "start" not in provider_fields
    assert "end" not in provider_fields
    assert "days" in provider_fields


def test_day_index_follows_the_knowledge_base_convention() -> None:
    assert DAY_INDEX["sunday"] == 0
    assert DAY_INDEX["saturday"] == 6
    assert len(DAY_INDEX) == 7


# ---------------------------------------------------------------------------
# A clean reading
# ---------------------------------------------------------------------------


def test_a_grounded_reading_becomes_a_complete_candidate() -> None:
    result = _extract(FULL_READING)

    assert result.ok
    assert result.error is None
    kb = result.candidate
    assert kb.location == "123 Main Street, Suite 302, Springfield"
    assert kb.hours[1] is not None and (kb.hours[1].open, kb.hours[1].close) == ("09:00", "17:00")
    assert kb.hours[5] is not None and (kb.hours[5].open, kb.hours[5].close) == ("09:00", "13:00")
    assert [s.name for s in kb.services] == ["Hearing Test", "Allergy Consultation"]
    assert [s.price for s in kb.services] == [150.0, 220.0]
    assert kb.accepted_insurance == ["Aetna", "Medicare"]
    assert [p.name for p in kb.providers] == ["Dr. Alice Nguyen"]


def test_days_not_listed_are_recorded_as_closed() -> None:
    result = _extract(FULL_READING)

    # Every weekday key present, so "closed" is explicit rather than absent.
    assert set(result.candidate.hours) == set(range(7))
    assert result.candidate.hours[0] is None
    assert result.candidate.hours[6] is None


def test_provider_days_map_to_indices_with_no_hours_attached() -> None:
    result = _extract(FULL_READING)

    schedule = result.candidate.providers[0].schedule
    assert [rule.day_of_week for rule in schedule] == [1, 2, 3, 4]
    # Blank hours: the days are a hint, inert until the doctor sets times.
    assert all(rule.start == "" and rule.end == "" for rule in schedule)


def test_a_provider_gets_a_stable_slug_id() -> None:
    result = _extract(FULL_READING)

    assert result.candidate.providers[0].id == "dr-alice-nguyen"


def test_the_candidate_is_never_marked_configured() -> None:
    result = _extract(FULL_READING)

    # Only save_clinic_config sets this, and only when the doctor submits.
    assert result.candidate.configured is False
    assert result.candidate.updated_at == ""


def test_a_provider_with_no_hours_still_prompts_the_doctor() -> None:
    result = _extract(FULL_READING)

    assert any("working hours" in note for note in result.notes)


def test_extraction_reports_completeness_using_the_real_validator() -> None:
    result = _extract(FULL_READING)

    assert result.validation.ok
    assert result.complete


# ---------------------------------------------------------------------------
# Grounding: invented values are dropped
# ---------------------------------------------------------------------------


def test_an_invented_address_is_dropped() -> None:
    result = _extract({**FULL_READING, "location": "742 Evergreen Terrace, Shelbyville"})

    assert result.candidate.location == ""
    assert any("address" in note for note in result.notes)


def test_a_reordered_address_is_still_accepted() -> None:
    # Models reorder address parts freely; demanding a contiguous match would
    # reject a correct reading.
    result = _extract(
        {**FULL_READING, "location": "Suite 302, 123 Main Street, Springfield"}
    )

    assert result.candidate.location == "Suite 302, 123 Main Street, Springfield"


def test_a_service_not_in_the_document_is_dropped() -> None:
    reading = {**FULL_READING, "services": [
        {"name": "Hearing Test", "price": "150"},
        {"name": "Rhinoplasty", "price": "5000"},
    ]}

    result = _extract(reading)

    assert [s.name for s in result.candidate.services] == ["Hearing Test"]
    assert any("Rhinoplasty" in note for note in result.notes)


def test_a_price_not_in_the_document_is_blanked_but_the_service_kept() -> None:
    reading = {**FULL_READING, "services": [{"name": "Hearing Test", "price": "999"}]}

    result = _extract(reading)

    assert [s.name for s in result.candidate.services] == ["Hearing Test"]
    assert result.candidate.services[0].price is None
    assert any("price" in note for note in result.notes)


def test_invented_prep_instructions_are_blanked() -> None:
    reading = {**FULL_READING, "services": [
        {"name": "Hearing Test", "price": "150",
         "prep_instructions": "Fast for twelve hours and bring a chaperone."},
    ]}

    result = _extract(reading)

    assert result.candidate.services[0].prep_instructions is None
    assert any("preparation" in note for note in result.notes)


def test_an_insurance_plan_not_in_the_document_is_dropped() -> None:
    result = _extract({**FULL_READING, "accepted_insurance": ["Aetna", "Cigna"]})

    assert result.candidate.accepted_insurance == ["Aetna"]
    assert any("Cigna" in note for note in result.notes)


def test_a_provider_not_in_the_document_is_dropped() -> None:
    reading = {**FULL_READING, "providers": [
        {"name": "Dr. Alice Nguyen", "specialty": "Audiologist"},
        {"name": "Dr. Hibbert", "specialty": "Surgeon"},
    ]}

    result = _extract(reading)

    assert [p.name for p in result.candidate.providers] == ["Dr. Alice Nguyen"]
    assert any("Hibbert" in note for note in result.notes)


def test_an_invented_specialty_is_blanked_but_the_provider_kept() -> None:
    reading = {**FULL_READING, "providers": [
        {"name": "Dr. Alice Nguyen", "specialty": "Neurosurgeon"},
    ]}

    result = _extract(reading)

    assert result.candidate.providers[0].name == "Dr. Alice Nguyen"
    assert result.candidate.providers[0].specialty == ""


def test_grounding_tolerates_punctuation_and_case_differences() -> None:
    reading = {**FULL_READING, "services": [{"name": "hearing test", "price": "150"}]}

    result = _extract(reading)

    assert [s.name for s in result.candidate.services] == ["hearing test"]


# ---------------------------------------------------------------------------
# Malformed readings
# ---------------------------------------------------------------------------


def test_an_unreadable_open_time_is_reported_and_the_day_left_blank() -> None:
    reading = {**FULL_READING, "hours": [
        {"day": "monday", "open": "whenever", "close": "late"},
    ]}

    result = _extract(reading)

    assert result.candidate.hours[1] is None
    assert any("Monday" in note for note in result.notes)


def test_an_unknown_day_name_is_ignored() -> None:
    reading = {**FULL_READING, "hours": [{"day": "funday", "open": "9:00", "close": "17:00"}]}

    result = _extract(reading)

    assert all(hours is None for hours in result.candidate.hours.values())


def test_a_duplicate_service_is_kept_once() -> None:
    reading = {**FULL_READING, "services": [
        {"name": "Hearing Test", "price": "150"},
        {"name": "Hearing Test", "price": "150"},
    ]}

    result = _extract(reading)

    assert len(result.candidate.services) == 1


def test_a_nameless_service_is_skipped() -> None:
    reading = {**FULL_READING, "services": [{"name": "", "price": "150"}]}

    result = _extract(reading)

    assert result.candidate.services == []


@pytest.mark.parametrize(
    "reading",
    [
        {},
        {"location": None, "hours": None, "services": None,
         "accepted_insurance": None, "providers": None},
        {"hours": "not a list", "services": "nope", "providers": 42,
         "accepted_insurance": {}, "location": ""},
        {"services": [None, "string", 7], "providers": [None], "hours": [None],
         "accepted_insurance": [None], "location": ""},
    ],
)
def test_a_junk_reading_yields_nothing_rather_than_raising(reading: Any) -> None:
    result = extract_clinic_config(SHEET, Reading(reading))

    assert not result.ok
    assert result.error is not None
    assert not result.found_anything


def test_a_document_with_no_clinic_details_says_so() -> None:
    notice = "The waiting room will be repainted next week. Excuse the smell."

    result = extract_clinic_config(
        notice,
        Reading({"location": "", "hours": [], "services": [],
                 "accepted_insurance": [], "providers": []}),
    )

    assert not result.ok
    assert result.error is not None


@pytest.mark.parametrize("text", ["", "   \n "])
def test_an_empty_document_is_reported_without_calling_the_model(text: str) -> None:
    extractor = Reading(FULL_READING)

    result = extract_clinic_config(text, extractor)

    assert not result.ok
    assert extractor.seen == []


def test_a_model_failure_is_reported_not_raised() -> None:
    result = extract_clinic_config(SHEET, Exploding(), source_label="info.txt")

    assert not result.ok
    assert result.error is not None
    assert "could not be read" in result.error


def test_an_over_long_document_is_truncated_and_the_doctor_told() -> None:
    extractor = Reading(FULL_READING)
    long_text = SHEET + ("\n\nfiller sentence." * 20_000)

    result = extract_clinic_config(long_text, extractor)

    assert len(extractor.seen[0]) == MAX_EXTRACT_CHARS
    assert any("first part" in note for note in result.notes)


def test_missing_required_fields_are_named_in_the_doctors_words() -> None:
    reading = {**FULL_READING, "providers": [], "location": ""}

    result = _extract(reading)

    joined = " ".join(result.notes)
    assert "provider" in joined
    assert "address" in joined
    # Not the raw validator text, which reads as a rejection of something typed.
    assert "is required" not in joined


# ---------------------------------------------------------------------------
# BedrockConfigExtractor: the request it sends
# ---------------------------------------------------------------------------


class RecordingConverseClient:
    def __init__(self, payload: dict[str, Any] | None = None, *, blocks: Any = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.payload = payload if payload is not None else FULL_READING
        self.blocks = blocks

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        content = (
            self.blocks
            if self.blocks is not None
            else [{"toolUse": {"name": TOOL_NAME, "input": self.payload}}]
        )
        return {"output": {"message": {"content": content}}, "stopReason": "tool_use"}


def test_the_model_is_forced_to_answer_through_the_tool_schema() -> None:
    client = RecordingConverseClient()

    BedrockConfigExtractor(client).extract("some document")

    tool_config = client.calls[0]["toolConfig"]
    # Forcing the tool is what guarantees a schema-shaped answer instead of prose
    # that happens to contain JSON.
    assert tool_config["toolChoice"] == {"tool": {"name": TOOL_NAME}}
    assert tool_config["tools"][0]["toolSpec"]["inputSchema"]["json"] is CONFIG_SCHEMA


def test_extraction_runs_at_zero_temperature() -> None:
    client = RecordingConverseClient()

    BedrockConfigExtractor(client).extract("some document")

    # Transcription has one right answer; sampling would invent variation.
    assert client.calls[0]["inferenceConfig"]["temperature"] == 0.0


def test_the_document_is_sent_in_the_user_message() -> None:
    client = RecordingConverseClient()

    BedrockConfigExtractor(client).extract("PARKING IS FREE")

    text = client.calls[0]["messages"][0]["content"][0]["text"]
    assert "PARKING IS FREE" in text
    assert client.calls[0]["messages"][0]["role"] == "user"


def test_the_system_prompt_tells_the_model_not_to_guess() -> None:
    client = RecordingConverseClient()

    BedrockConfigExtractor(client).extract("x")

    system = client.calls[0]["system"][0]["text"].lower()
    assert "guess" in system or "transcrib" in system


def test_the_configured_model_id_is_used() -> None:
    client = RecordingConverseClient()

    BedrockConfigExtractor(client, model_id="my-model").extract("x")

    assert client.calls[0]["modelId"] == "my-model"


def test_the_tool_input_is_returned_as_the_reading() -> None:
    client = RecordingConverseClient({"location": "somewhere"})

    result = BedrockConfigExtractor(client).extract("x")

    assert result == {"location": "somewhere"}


@pytest.mark.parametrize(
    "blocks",
    [
        [{"text": "I could not find anything."}],
        [],
        [{"toolUse": {"name": TOOL_NAME, "input": "not a dict"}}],
    ],
)
def test_a_response_without_usable_tool_input_yields_an_empty_reading(blocks: Any) -> None:
    client = RecordingConverseClient(blocks=blocks)

    assert BedrockConfigExtractor(client).extract("x") == {}


def test_the_schema_is_valid_json() -> None:
    # It crosses the wire, so it has to serialize.
    assert json.loads(json.dumps(CONFIG_SCHEMA)) == CONFIG_SCHEMA
