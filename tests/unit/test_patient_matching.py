"""Finding a patient by a name that came off a phone line.

Observed live. The seeded record read "Lakshmi Prasad" with mobile 9900012301, the
doctor's screen displayed it, and the agent told the caller it could not locate any
patient record for her. Speech-to-text lower-cases what it transcribes, the lookup
key was built from the raw string, and the two missed by two capital letters.

The second consequence is the worse one: the booking path creates a record when
lookup finds none, so she would have got a *duplicate* — her blood group, height
and weight stranded on the first record, and the doctor left with two half-records
for one person.

What must NOT happen is fuzzy matching. Two different people with similar names
must never collide: handing one patient another's blood group is far worse than
failing to find a record.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.data_layer.memory import MemoryPatientStore
from clinic_front_desk.models import (
    Patient,
    normalize_patient_code,
    normalize_person_name,
    normalize_phone,
    patient_code,
    patient_lookup_key,
    patient_to_item,
)

NAME = "Lakshmi Prasad"
PHONE = "9900012301"


def _store() -> MemoryPatientStore:
    store = MemoryPatientStore()
    store.create(
        Patient(id="pat-1", name=NAME, callback_phone=PHONE, created_at="2026-09-12")
    )
    return store


# ---------------------------------------------------------------------------
# The live failure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spoken",
    [
        "Lakshmi Prasad",
        "lakshmi prasad",
        "LAKSHMI PRASAD",
        "Lakshmi  Prasad",
        "  lakshmi prasad  ",
    ],
)
def test_a_spoken_name_finds_the_record_whatever_the_casing(spoken: str) -> None:
    found = _store().find_by_name_and_phone(spoken, PHONE)

    assert [p.id for p in found.value] == ["pat-1"], spoken


@pytest.mark.parametrize(
    "dictated",
    [
        "9900012301",
        "99000 12301",
        "99000-12301",
        "+91 99000 12301",
        "099000 12301",
    ],
)
def test_a_dictated_number_finds_the_record_however_it_is_grouped(
    dictated: str,
) -> None:
    """A number read out over the phone comes back grouped differently each time."""
    found = _store().find_by_name_and_phone(NAME, dictated)

    assert [p.id for p in found.value] == ["pat-1"], dictated


def test_the_stored_name_is_never_rewritten() -> None:
    """The doctor must read what was entered, not a normalised version of it."""
    stored = _store().get("pat-1").value

    assert stored is not None
    assert stored.name == "Lakshmi Prasad"
    assert stored.callback_phone == "9900012301"


# ---------------------------------------------------------------------------
# What must still NOT match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "other",
    [
        "Lakshmi Prasadh",
        "Lakshmi",
        "Prasad",
        "Lakshman Prasad",
        "Lakshmi Prasad Rao",
    ],
)
def test_a_different_name_never_matches(other: str) -> None:
    """Not fuzzy. Merging two patients is worse than failing to find one."""
    found = _store().find_by_name_and_phone(other, PHONE)

    assert found.value == [], other


def test_a_different_number_never_matches() -> None:
    found = _store().find_by_name_and_phone(NAME, "9900012302")

    assert found.value == []


def test_a_blank_name_does_not_match_everything() -> None:
    found = _store().find_by_name_and_phone("", PHONE)

    assert found.value == []


# ---------------------------------------------------------------------------
# Both backends must agree, or the agent behaves differently against DynamoDB
# ---------------------------------------------------------------------------


def test_the_dynamo_key_matches_what_the_memory_store_compares() -> None:
    patient = Patient(id="pat-1", name=NAME, callback_phone=PHONE)
    item = patient_to_item(patient)

    assert item["GSI3PK"] == f"NAMEPHONE#{patient_lookup_key(NAME, PHONE)}"
    # And a differently-cased spoken name lands on the same partition.
    assert item["GSI3PK"] == f"NAMEPHONE#{patient_lookup_key('lakshmi prasad', PHONE)}"


# ---------------------------------------------------------------------------
# The normalisers themselves
# ---------------------------------------------------------------------------


def test_accents_are_levelled_so_a_transcript_without_them_still_matches() -> None:
    assert normalize_person_name("Renée") == normalize_person_name("Renee")


def test_punctuation_a_transcript_invents_is_ignored() -> None:
    assert normalize_person_name("Dr. A.B. Rao") == normalize_person_name("Dr A B Rao")
    assert normalize_person_name("O'Brien") == normalize_person_name("OBrien")
    assert normalize_person_name("Anne-Marie") == normalize_person_name("Anne Marie")


def test_a_country_code_does_not_split_one_person_into_two() -> None:
    assert normalize_phone("+919900012301") == normalize_phone("9900012301")
    assert normalize_phone("09900012301") == normalize_phone("9900012301")


def test_a_short_number_is_kept_whole() -> None:
    """A landline or extension shorter than a mobile must not be padded or cut."""
    assert normalize_phone("2345678") == "2345678"


def test_a_blank_name_yields_no_key() -> None:
    assert normalize_person_name("   ") == ""


# ---------------------------------------------------------------------------
# The short patient code
# ---------------------------------------------------------------------------
#
# Asked for an appointment reference on a live call, a caller had nothing to give:
# nobody memorises "3422c38f-637e-4d43-a187-34ad6038a3f5". So she spelled her name
# out twice instead and the call went nowhere. Five characters can be said once and
# written on the back of a hand.


def test_the_code_is_built_as_specified() -> None:
    """First letter of the first name, last of the last, last three digits."""
    assert patient_code("sidda deepika", "9502285901") == "SA901"
    assert patient_code("Lakshmi Prasad", "9900012301") == "LD301"


def test_the_code_ignores_how_the_name_was_transcribed() -> None:
    assert patient_code("SIDDA DEEPIKA", "9502285901") == "SA901"
    assert patient_code("  sidda   deepika  ", "+91 95022 85901") == "SA901"


def test_a_single_name_still_produces_a_code() -> None:
    """Weaker than a full name, but better than refusing the caller one."""
    assert patient_code("Meenakshi", "9900012318") == "MI318"


def test_no_code_when_there_is_not_enough_to_build_one() -> None:
    """An empty string is a case the caller handles, not a stub that matches."""
    assert patient_code("", "9900012301") == ""
    assert patient_code("Lakshmi Prasad", "12") == ""


@pytest.mark.parametrize(
    "spoken",
    ["SA901", "sa901", "sa 901", "s a 9 0 1", "SA-901", "  sa901  "],
)
def test_a_code_said_aloud_is_recognised_however_it_is_grouped(spoken: str) -> None:
    assert normalize_patient_code(spoken) == "SA901", spoken


def test_a_code_finds_the_patient() -> None:
    store = MemoryPatientStore()
    store.create(
        Patient(id="pat-1", name=NAME, callback_phone=PHONE, code="LD301")
    )

    found = store.find_by_code("l d 3 0 1")

    assert [p.id for p in found.value] == ["pat-1"]


def test_a_colliding_code_returns_both_so_the_caller_must_disambiguate() -> None:
    """Five characters collide. Guessing would show one patient another's records."""
    store = MemoryPatientStore()
    store.create(Patient(id="pat-1", name="Lakshmi Prasad", callback_phone="9900012301"))
    store.create(Patient(id="pat-2", name="Lalitha Sharad", callback_phone="9111112301"))
    for patient_id in ("pat-1", "pat-2"):
        record = store.get(patient_id).value
        assert record is not None
        record.code = "LD301"
        store.update(record)

    found = store.find_by_code("LD301")

    assert sorted(p.id for p in found.value) == ["pat-1", "pat-2"]


def test_an_unknown_code_matches_nothing_rather_than_everything() -> None:
    store = MemoryPatientStore()
    store.create(Patient(id="pat-1", name=NAME, callback_phone=PHONE, code="LD301"))

    assert store.find_by_code("ZZ999").value == []
    assert store.find_by_code("").value == []
    assert store.find_by_code("   ").value == []


def test_a_patient_with_no_code_is_not_matched_by_a_blank_one() -> None:
    store = MemoryPatientStore()
    store.create(Patient(id="pat-1", name=NAME, callback_phone=PHONE, code=""))

    assert store.find_by_code("").value == []
