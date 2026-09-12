"""Dictated numbers must match the same record as typed ones.

Speech-to-text does not reliably convert number words to digits. The same speaker
saying the same mobile number gets "9900012307" on one call and "nine nine zero
zero zero one two three zero seven" on the next. Both must find the same patient.

Observed live, and it failed in the worst possible order: the caller quoted her
patient code, that missed, so the agent fell back to name-and-mobile — and that
missed too, for the same underlying reason. She was told twice that the clinic had
no record of her. It did: ``SI307 / Sailaja Devi / 9900012307``.
"""

from __future__ import annotations

import pytest

from clinic_front_desk.models.matching import (
    normalize_patient_code,
    normalize_phone,
    patient_code,
    patient_lookup_key,
)

#: The live record the calls below failed to find.
NAME = "Sailaja Devi"
PHONE = "9900012307"
CODE = "SI307"


# -- the exact strings from the failed call ---------------------------------


def test_code_dictated_as_words_matches_the_written_code() -> None:
    """'s i three zero seven' is SI307, not SITHREEZEROSEVEN."""
    assert normalize_patient_code("s i three zero seven") == CODE


def test_phone_dictated_as_words_matches_the_written_number() -> None:
    """The failure that mattered: this used to normalize to the empty string."""
    spoken = "nine nine zero zero zero one two three zero seven"
    assert normalize_phone(spoken) == PHONE


def test_dictated_phone_never_yields_an_empty_key() -> None:
    """An empty key is worse than a wrong one: it matches nothing, silently."""
    spoken = "nine nine zero zero zero one two three zero seven"
    assert normalize_phone(spoken) != ""


def test_lookup_key_agrees_whether_dictated_or_typed() -> None:
    spoken = "nine nine zero zero zero one two three zero seven"
    assert patient_lookup_key("sailaja devi", spoken) == patient_lookup_key(NAME, PHONE)


def test_code_issued_from_a_dictated_number_is_the_real_code() -> None:
    """Registration must not mint a different code just because it heard words."""
    spoken = "nine nine zero zero zero one two three zero seven"
    assert patient_code(NAME, spoken) == patient_code(NAME, PHONE) == CODE


# -- the ways a number actually gets read out ------------------------------


@pytest.mark.parametrize(
    "spoken",
    [
        "9900012307",
        "nine nine zero zero zero one two three zero seven",
        "nine nine zero zero zero one two three zero seven.",
        "nine-nine-zero-zero-zero-one-two-three-zero-seven",
        "nine nine 000 one two 307",  # half converted, which is what really happens
        "double nine zero zero zero one two three zero seven",
        "+91 9900012307",
        "099000 12307",
    ],
)
def test_every_spoken_form_of_one_number_agrees(spoken: str) -> None:
    assert normalize_phone(spoken) == PHONE


@pytest.mark.parametrize(
    "spoken",
    ["SI307", "si307", "s i three zero seven", "S-I-3-0-7", "si 307", "S I 307"],
)
def test_every_spoken_form_of_one_code_agrees(spoken: str) -> None:
    assert normalize_patient_code(spoken) == CODE


# -- "oh" is both a digit and a letter -------------------------------------


def test_oh_is_zero_inside_a_phone_number() -> None:
    """Nobody says "nine zero two"; they say "nine oh two"."""
    assert normalize_phone("nine nine oh oh oh one two three oh seven") == PHONE


def test_oh_is_the_letter_while_still_in_a_codes_letter_prefix() -> None:
    """A code is two letters then three digits, so position settles it."""
    assert normalize_patient_code("s oh three zero seven") == "SO307"


def test_oh_is_zero_once_a_codes_digits_have_started() -> None:
    assert normalize_patient_code("s i three oh seven") == CODE


# -- what must NOT change --------------------------------------------------


def test_typed_input_is_untouched() -> None:
    """The conversion is a no-op when speech-to-text did its job."""
    assert normalize_phone(PHONE) == PHONE
    assert normalize_patient_code(CODE) == CODE


def test_a_name_containing_a_number_word_is_not_mangled() -> None:
    """Only phones and codes are converted; names go through the name path."""
    assert patient_lookup_key("Ono Sixtus", PHONE) == patient_lookup_key(
        "Ono Sixtus", PHONE
    )
    # And the code built from that name keeps its real letters.
    assert patient_code("Ono Sixtus", PHONE) == "OS307"


def test_blank_phone_still_has_no_key() -> None:
    """A blank must not become a key that matches every unnumbered record."""
    assert normalize_phone("") == ""
    assert normalize_phone("   ") == ""


def test_a_code_with_no_digits_spoken_is_not_invented() -> None:
    assert normalize_patient_code("") == ""
