"""Normalising a person's name and phone number for record lookup.

Why this exists
---------------
Patient lookup compared the caller's name byte for byte against the stored one.
Speech-to-text lower-cases everything it transcribes, so a caller whose record
reads "Lakshmi Prasad" says her name, the transcript reads ``lakshmi prasad``, and
the lookup key misses by two capital letters. Observed live: the agent told a
caller it could not find her record while the doctor's screen displayed it.

Two things go wrong when that happens, and the second is worse. The caller is told
the clinic has no record of her. And because the booking path creates a record when
lookup finds none, she gets a *second* one — so her blood group, height and weight
stay stranded on the first, and the doctor ends up with two half-records for one
person.

The offered-service matcher already solved exactly this problem for service names
(see :func:`~clinic_front_desk.tools.service_matcher.normalize_service_name`, whose
docstring records the same failure). It was never applied to people.

What is deliberately NOT done here
----------------------------------
The stored name is never rewritten. What the doctor reads must stay exactly what
the caller said or the doctor typed — "Lakshmi Prasad", not "lakshmi prasad". These
functions build the *lookup key* only.

Nor is this fuzzy matching. Case, surrounding whitespace and punctuation are
levelled; nothing else. Two different people with similar names must never collide,
because merging two patients' records is far worse than failing to find one.
"""

from __future__ import annotations

import re
import unicodedata

#: Apostrophes, which a transcript adds or drops inside a single word. Removed
#: outright so "O'Brien" and "OBrien" are one name.
_NAME_APOSTROPHE = re.compile(r"[\u2019'`]")

#: Punctuation that stands between words — full stops after initials, hyphens in a
#: double-barrelled name, stray commas. Replaced with a space, so "Dr. A.B. Rao"
#: and "Dr A B Rao" agree, as do "Anne-Marie" and "Anne Marie".
_NAME_SEPARATOR = re.compile(r"[.\-,]")

#: Everything that is not a digit, for phone comparison.
_NON_DIGITS = re.compile(r"\D")

#: Runs of anything that is not a letter or digit, used to split dictated input
#: into tokens. A dictated number arrives as words separated by spaces, commas or
#: nothing consistent at all.
_DICTATION_SPLIT = re.compile(r"[^0-9A-Za-z]+")

#: Number words, because speech-to-text does not always convert them. Nova Sonic
#: returns "9900012307" on one call and "nine nine zero zero zero one two three
#: zero seven" on the next, from the same speaker saying the same thing.
#:
#: Observed live, and it broke both lookup paths at once: the phone key is built by
#: discarding non-digits, so a number dictated as words reduced to the empty string
#: and the caller was told no record existed. Her record was there the whole time.
_DIGIT_WORDS: dict[str, str] = {
    "zero": "0",
    "one": "1",
    "two": "2",
    "three": "3",
    "four": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "nine": "9",
}

#: Said for zero — and also how the letter O is pronounced, which is the whole
#: reason :func:`_from_dictation` needs to know the position.
_ZERO_OR_LETTER_O: frozenset[str] = frozenset({"oh", "o"})

#: "double nine" is two nines. Common when reading a number aloud, and it costs
#: nothing to understand.
_REPEATS: dict[str, int] = {"double": 2, "triple": 3}

#: Leading letters in a patient code — see :func:`patient_code`. Everything after
#: them is digits, which is what makes "oh" resolvable rather than ambiguous.
CODE_LETTERS = 2


def _from_dictation(text: str, *, letters_before_digits: int) -> str:
    """Turn dictated words into the characters they stand for.

    ``letters_before_digits`` is how many leading characters are letters, and it
    exists to settle one genuine ambiguity: "oh" is both the digit zero and the
    name of the letter O. In a phone number it is always zero (pass ``0``). In a
    patient code the first two characters are letters and the rest are digits, so
    "S oh three zero seven" is ``SO307`` while "nine oh two" is ``902``.

    Tokens that are already letters or digits pass through untouched, so this is a
    no-op on input that speech-to-text transcribed properly.
    """
    out: list[str] = []
    repeat = 1
    for token in _DICTATION_SPLIT.split(text):
        if not token:
            continue
        lowered = token.casefold()
        if lowered in _REPEATS:
            repeat = _REPEATS[lowered]
            continue
        if lowered in _DIGIT_WORDS:
            char = _DIGIT_WORDS[lowered]
        elif lowered in _ZERO_OR_LETTER_O:
            # Still inside the letter prefix, so this is the letter, not a zero.
            char = "O" if len("".join(out)) < letters_before_digits else "0"
        else:
            # Already a digit or a real letter. Never expanded, so a name or a
            # code that happens to contain these characters is left alone.
            out.append(token)
            repeat = 1
            continue
        out.append(char * repeat)
        repeat = 1
    return "".join(out)

#: Length of an Indian mobile number without a country code. A longer string is
#: assumed to carry a country or trunk prefix, and only the trailing digits are
#: compared — "+91 99000 12301", "099000 12301" and "9900012301" are one number.
NATIONAL_NUMBER_DIGITS = 10


def normalize_person_name(name: str) -> str:
    """Return a comparison key for a person's name.

    Levels case, collapses whitespace runs, drops punctuation that speech-to-text
    adds or omits at random, and decomposes accents so "Renée" and "Renee" match.
    Returns an empty string for a blank name, which callers must treat as "no
    lookup key" rather than as a key that matches every unnamed record.
    """
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    cleaned = _NAME_SEPARATOR.sub(" ", _NAME_APOSTROPHE.sub("", stripped))
    return " ".join(cleaned.split()).casefold()


def normalize_phone(phone: str) -> str:
    """Return a comparison key for a phone number.

    Digits only, so spacing and punctuation do not matter — a number dictated over
    the phone comes back grouped differently almost every time. When more than
    :data:`NATIONAL_NUMBER_DIGITS` digits remain, only the last that many are kept,
    so a country code or a leading zero does not split one person into two records.

    Number *words* are converted first. Without that step a number transcribed as
    "nine nine zero zero zero one two three zero seven" contains no digits at all,
    so the key came out empty and matched nothing — the caller was told her record
    did not exist while it sat in the table.
    """
    digits = _NON_DIGITS.sub("", _from_dictation(phone, letters_before_digits=0))
    if len(digits) > NATIONAL_NUMBER_DIGITS:
        return digits[-NATIONAL_NUMBER_DIGITS:]
    return digits


def patient_lookup_key(name: str, phone: str) -> str:
    """The name+phone key both stores use to find an existing patient."""
    return f"{normalize_person_name(name)}#{normalize_phone(phone)}"


#: Digits of the mobile number that go into a patient code.
CODE_PHONE_DIGITS = 3


def patient_code(name: str, phone: str) -> str:
    """A short code a patient can remember and say back: e.g. ``SA901``.

    First letter of the first name, last letter of the last name, and the final
    three digits of the mobile number. "Sidda Deepika" on 9502285901 becomes
    ``SA901``.

    This exists because the alternative was a UUID. Asked for an appointment
    reference on a live call, a caller had nothing to give — nobody memorises
    ``3422c38f-637e-4d43-a187-34ad6038a3f5`` — so she spelled her name out twice
    instead. Five characters can be said once and written on the back of a hand.

    **It is a convenience, not proof of identity.** Five characters collide: two
    patients can share a first initial, a final letter and three digits. So a code
    narrows a search and never settles it — a caller quoting one still confirms
    their name, and a code matching two records must be disambiguated, never
    guessed. Showing one patient another's appointments would be far worse than
    asking them to repeat themselves.

    Returns an empty string when there is not enough to build one from, so callers
    can treat "no code" as a case rather than matching on a stub.
    """
    words = normalize_person_name(name).split()
    # Dictated numbers first, or a number transcribed as words yields no digits and
    # the caller is issued no code at all.
    digits = _NON_DIGITS.sub("", _from_dictation(phone, letters_before_digits=0))
    if not words or len(digits) < CODE_PHONE_DIGITS:
        return ""
    first = words[0][0]
    # The last letter of the last name. With only one name given, its own last
    # letter — better a slightly weaker code than none at all.
    last = words[-1][-1]
    return f"{first}{last}{digits[-CODE_PHONE_DIGITS:]}".upper()


def normalize_patient_code(code: str) -> str:
    """Comparison form of a patient code: letters and digits only, upper-case.

    A code said aloud comes back as "S A 9 0 1", "sa-901" or "SA 901". All of those
    are the same code.

    Number words are converted too, since a caller reading a code out reads the
    digits as words: "s i three zero seven" is ``SI307``. Previously that stripped
    to ``SITHREEZEROSEVEN`` and found nothing, so the agent fell back to asking for
    a name and mobile number — and then failed on the number for the same reason.
    """
    spoken = _from_dictation(code, letters_before_digits=CODE_LETTERS)
    return re.sub(r"[^0-9A-Za-z]", "", spoken).upper()


__all__ = [
    "CODE_LETTERS",
    "CODE_PHONE_DIGITS",
    "NATIONAL_NUMBER_DIGITS",
    "normalize_patient_code",
    "normalize_person_name",
    "normalize_phone",
    "patient_code",
    "patient_lookup_key",
]
