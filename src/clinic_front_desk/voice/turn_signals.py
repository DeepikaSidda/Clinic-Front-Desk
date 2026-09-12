"""Deterministic transcript → :class:`Turn` signal extraction (Req 9.x, 10.x).

:class:`~clinic_front_desk.voice.guardrails.GuardrailPolicy` decides over
*structured signals* (``requests_clinical_content``, ``names_symptom``,
``expresses_distress``, …), not over raw text. Nothing produced those signals from
what a caller actually said, so on a live call every finalized turn bypassed the
guardrail entirely: clinical content was declined only by the system prompt, and
the Req 9.1/9.2/9.4 escalation was never recorded unless the model itself chose to
call ``flag_for_human``. This module is the missing step.

Why deterministic rather than model-driven
------------------------------------------
The guardrail is a **backstop**, and a backstop that asks the model "was that
clinical?" inherits exactly the failure it exists to catch — a model that decides
to answer a medical question will also decide it was not clinical. So extraction
is pure pattern matching over the transcript: no network call, no model
discretion, fully unit-testable, and identical on every run.

That buys safety at the cost of recall (paraphrases this module has not seen slip
through) — but it is strictly additive: the prompt-layer guardrail still applies,
and the model may still call ``flag_for_human`` itself. This layer can only *add*
escalations, never suppress one.

Precision matters as much as recall
-----------------------------------
An over-eager matcher is not "safely conservative" — it escalates ordinary
bookings to a human and makes the agent useless. Two rules keep it tight:

- Match **phrases, not bare words**. ``"hearing"`` appears in the offered service
  "Hearing Test", so the symptom pattern is ``"hearing loss"`` / ``"can't hear"``,
  never ``"hearing"``.
- Qualify open-ended stems with clinical objects. ``"do I need"`` alone is an
  admin question (*"do I need to bring anything?"*); ``"do I need surgery"`` is
  clinical. The patterns require the object.

Ordering note: the policy checks clinical content first, so anything routed to
``requests_clinical_content`` escalates immediately and declines. Medical
emergencies are deliberately routed there.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from clinic_front_desk.tools.service_matcher import normalize_service_name

from .guardrails import Turn

__all__ = [
    "ExtractedTurn",
    "extract_turn",
    "normalize_transcript",
    "offers_escalation",
]


def normalize_transcript(text: str) -> str:
    """Lower-case, collapse whitespace, and drop most punctuation.

    Speech recognition punctuates inconsistently ("what's" vs "whats", trailing
    periods mid-phrase), so patterns are written against this form. Apostrophes
    are removed rather than kept so ``"can't"`` and ``"cant"`` both match one
    pattern.
    """
    lowered = text.lower().replace("’", "'").replace("'", "")
    stripped = re.sub(r"[^a-z0-9\s]", " ", lowered)
    return re.sub(r"\s+", " ", stripped).strip()


def _patterns(*phrases: str) -> tuple[re.Pattern[str], ...]:
    """Compile phrases as whole-word patterns against a normalized transcript."""
    return tuple(re.compile(rf"\b{phrase}\b") for phrase in phrases)


# ---------------------------------------------------------------------------
# Signal vocabularies. Each is a tuple of patterns matched against the
# normalized transcript; the first match becomes the recorded evidence.
# ---------------------------------------------------------------------------

#: A medical emergency. Routed to clinical content so it declines and escalates
#: immediately rather than being treated as mere distress.
_EMERGENCY = _patterns(
    r"emergency",
    r"cant breathe",
    r"cannot breathe",
    r"struggling to breathe",
    r"bleeding heavily",
    r"wont stop bleeding",
    r"passed out",
    r"lost consciousness",
    r"chest pain",
    r"ambulance",
)

#: Requests for clinical advice, triage, diagnosis, treatment, or medication
#: (Req 10.1, 10.6, 9.1). Open-ended stems are qualified with a clinical object so
#: ordinary admin questions do not match.
_CLINICAL_CONTENT = _patterns(
    # Diagnosis / triage
    r"whats wrong with me",
    r"what is wrong with me",
    r"what do i have",
    r"do i have (an )?(infection|infections|tinnitus|cancer|a tumour|a tumor)",
    r"diagnose",
    r"diagnosis",
    r"is (it|this) serious",
    r"is (it|this) dangerous",
    r"is (it|this) normal",
    r"should i (be )?(worried|concerned)",
    r"how bad is (it|this)",
    r"whats causing",
    r"what is causing",
    r"what could (it|this) be",
    # Treatment / procedure
    r"(do|will|would) i need (surgery|an operation|treatment|antibiotics|medication|"
    r"medicine|a scan|an x ray|an xray|stitches|drops)",
    r"what treatment",
    r"how (do|can) (i|you) treat",
    r"how (do|can) i cure",
    r"can (i|you) cure",
    r"should i have surgery",
    r"what are the side effects",
    # Medication
    r"what (medication|medicine|antibiotic|antibiotics|painkiller|painkillers) should",
    r"(can|should) i take (any )?(medication|medicine|antibiotics|painkillers|ibuprofen|"
    r"paracetamol|aspirin)",
    r"prescribe",
    r"prescription for",
    r"what dose",
    r"what dosage",
    r"how much (medicine|medication) should",
    # Self-care advice
    r"what should i do about (my|the|this)",
    r"how do i (stop|fix|relieve|ease)",
    r"is it safe to",
)

#: The patient described a symptom (Req 10.3). Multi-word where a bare word would
#: collide with an offered service name or an ordinary booking phrase.
_SYMPTOM = _patterns(
    r"hurts",
    r"hurting",
    r"in pain",
    r"painful",
    r"aches",
    r"aching",
    r"earache",
    r"ear ache",
    r"headache",
    r"sore throat",
    r"my throat is sore",
    r"dizzy",
    r"dizziness",
    r"vertigo",
    r"nauseous",
    r"nausea",
    r"vomiting",
    r"throwing up",
    r"fever",
    r"feverish",
    r"temperature is",
    r"swollen",
    r"swelling",
    r"bleeding",
    r"discharge",
    r"pus",
    r"rash",
    r"itchy",
    r"itching",
    r"burning sensation",
    r"numb",
    r"numbness",
    # Hearing/ENT specifics — never the bare word "hearing" (see module docstring).
    r"hearing loss",
    r"losing my hearing",
    r"lost my hearing",
    r"cant hear",
    r"cannot hear",
    r"hard of hearing",
    r"ringing in my ears?",
    r"ears? (is|are) blocked",
    r"blocked ears?",
    r"blocked nose",
    r"cant smell",
    r"cannot smell",
    r"cant breathe through",
    r"sinuses hurt",
    r"stuffy",
    r"congested",
    r"congestion",
    r"coughing",
    r"sneezing",
    r"snoring",
)

#: The patient asked for a clinical or clinic-policy decision (Req 10.5).
_POLICY_DECISION = _patterns(
    r"make an exception",
    r"waive",
    r"override",
    r"bend the rules",
    r"can you approve",
    r"give me a discount",
    r"reduce the price",
    r"free of charge",
    r"is it covered",
    r"will (my )?insurance cover",
    r"do you accept my insurance plan",
    r"can you bill",
    r"how urgent(ly)? (should|do) i",
)

#: The caller asked which service to book (Req 10.3).
#:
#: Kept apart from :data:`_POLICY_DECISION`, where these lived. Grouped with policy
#: decisions they escalated to a human, which turned "which service should I book?"
#: — the most ordinary question a caller can ask a front desk — into a handover.
#: It is not a policy decision and it is not a clinical question; it is answerable
#: without interpreting anything, by offering the general consultation.
_WHICH_SERVICE = _patterns(
    r"which (service|test|appointment) (do|should) i (need|book|have|take|go for)",
    r"which (service|test|appointment) is right",
    r"which (one |service |test )?(do|would) you (recommend|suggest)",
    r"what (service|test|appointment) (do|should) i (need|book|have|take|go for)",
    r"what (kind|type|sort) of (service|test|appointment|doctor)",
    r"which (service|test) should i (prefer|choose|pick)",
    r"what should i book",
    r"which should i book",
    r"(im|i am) not sure (which|what)",
    r"(i )?dont know (which|what) (service|test|one)",
    r"(can|could) you (recommend|suggest) (a|an|any|the right)",
    r"help me (choose|decide|pick)",
)

#: Requests that fall outside the agent's administrative rules (Req 9.2).
_OUTSIDE_ADMIN_RULES = _patterns(
    r"refund",
    r"complaint",
    r"complain",
    r"sue",
    r"lawyer",
    r"solicitor",
    r"malpractice",
    r"negligence",
    r"medical records",
    r"my records",
    r"test results",
    r"my results",
    r"referral letter",
    r"sick note",
    r"sick leave",
    r"fit note",
    r"insurance claim",
    r"speak to billing",
    r"dispute",
)

#: Words a caller uses for "a human being", as opposed to this agent.
#: Longer forms come first: regex alternation takes the first branch that matches
#: at a position, so "member of staff" must be offered before bare "staff".
_HUMAN_NOUN = (
    r"(member of (the )?(staff|team)|staff member|team member|"
    r"human|humans|person|people|someone|somebody|agent|representative|rep|"
    r"operator|staff|nurse|doctor|dr|receptionist|manager|"
    r"assistant|colleague|specialist)"
)

#: The patient explicitly asked for a human (Req 9.7).
#:
#: Built from a verb set × the human-noun set rather than hand-listed phrases.
#: The hand-listed version missed every one of these, observed or plausible:
#: "connect me with a human agent", "i want to speak *with* a human",
#: "i need a human", "can i get a human agent", "human agent please". On a real
#: call the model *said* it had escalated while the backstop stayed silent and the
#: call ended `interrupted` — the exact failure this layer exists to prevent.
_HUMAN_REQUEST = _patterns(
    # "…speak to / talk with / connect me with / get me / put me through to…"
    rf"(speak|talk|chat|connect|transfer|escalate)\s+(me\s+)?(to|with)\s+"
    rf"(a|an|the|any)?\s*(real\s+|actual\s+|live\s+|human\s+)?{_HUMAN_NOUN}",
    # Bare "connect me" / "transfer me" are unambiguous. "put me" deliberately is
    # not included on its own — "put me on the waitlist" is an ordinary
    # administrative request, and matching it escalated a booking to a human.
    r"(connect|transfer|escalate)\s+me\b",
    r"put me through",
    # "…want / need / get / like a human…"
    rf"(want|need|like|get|prefer)\s+(to\s+)?(speak|talk)?\s*(to|with)?\s*"
    rf"(a|an|the|any)?\s*(real\s+|actual\s+|live\s+|human\s+)?{_HUMAN_NOUN}",
    # Bare "human agent please" / "a real person".
    rf"(real|actual|live|human)\s+{_HUMAN_NOUN}",
    rf"{_HUMAN_NOUN}\s+please",
    # "is anyone there / available", "can someone call me back"
    r"is (there )?(anyone|somebody|someone) (else )?(there|available)",
    rf"(have|get|ask)\s+(a|an|the)?\s*{_HUMAN_NOUN}\s+(to\s+)?(call|ring|phone)\s+me",
    r"(call|ring|phone) me back",
)

#: The patient stated anger, dissatisfaction, or distress (Req 9.3).
_DISTRESS = _patterns(
    r"angry",
    r"furious",
    r"upset",
    r"frustrated",
    r"frustrating",
    r"annoyed",
    r"fed up",
    r"unacceptable",
    r"ridiculous",
    r"appalling",
    r"disgusted",
    r"terrible service",
    r"awful service",
    r"worst",
    r"complete waste",
    r"im scared",
    r"im terrified",
    r"im worried sick",
    r"panicking",
    r"in tears",
    r"crying",
    r"desperate",
)

#: Affirmatives that accept an outstanding escalation offer (Req 9.8). Only
#: consulted when an offer is actually outstanding, since a bare "yes" otherwise
#: means agreement with whatever was just asked.
_ACCEPTANCE = _patterns(
    r"yes",
    r"yeah",
    r"yep",
    r"sure",
    r"ok",
    r"okay",
    r"please do",
    r"that would be (great|good|helpful)",
    r"id like that",
    r"go ahead",
    r"do that",
)


#: Phrases in the *agent's own* speech that offer to hand the call to a human.
#:
#: The guardrail can only arm the "a bare yes accepts the offer" path (Req 9.8) for
#: offers it made itself. But the model also offers handovers on its own initiative
#: — observed live: *"Would you like me to connect you with a human
#: representative?"*, the caller said "yes", and nothing escalated because our
#: state did not know an offer was outstanding. Reading the agent's transcript
#: closes that gap using data the stream already delivers.
_AGENT_ESCALATION_OFFER = _patterns(
    rf"(connect|transfer|escalate|put)\s+you\s+(to|with|through)\s+(a|an|the)?\s*"
    rf"(real\s+|actual\s+|live\s+|human\s+)?{_HUMAN_NOUN}",
    rf"(would|shall|should|can)\s+i\s+(connect|transfer|escalate|have|get|ask)\s+"
    rf"(you\s+)?(to\s+|with\s+)?(a|an|the)?\s*{_HUMAN_NOUN}",
    rf"(speak|talk)\s+(to|with)\s+(a|an|the)?\s*(real\s+|human\s+)?{_HUMAN_NOUN}",
    r"(have|get)\s+(a|an|the)?\s*\w*\s*(call|ring|phone)\s+you",
    r"take a message",
    r"pass (this|it) (on )?to",
    # `_patterns` appends \b, so a bare stem like "escalat" can never match
    # "escalate" — the trailing \w* is what lets the boundary land after the word.
    r"escalat\w*",
)


def offers_escalation(text: str) -> bool:
    """Whether the *agent's* utterance offers to hand the call to a human.

    Used to arm the Req 9.8 acceptance path for offers the model made on its own,
    which the guardrail would otherwise have no record of.
    """
    return _first_match(normalize_transcript(text), _AGENT_ESCALATION_OFFER) is not None


def _first_match(
    normalized: str, patterns: Iterable[re.Pattern[str]]
) -> str | None:
    """Return the first pattern that matches, as its matched text."""
    for pattern in patterns:
        found = pattern.search(normalized)
        if found:
            return found.group(0)
    return None


def _named_service(normalized: str, offered_services: Iterable[str]) -> str | None:
    """Return the offered service the caller named, if the transcript contains it.

    Only whole-phrase containment of a configured service name counts, which keeps
    this the single path from an utterance to a service (Req 2.1, 10.2). Longer
    names are tested first so "Allergy Screening" is not shadowed by a shorter
    name that happens to be a prefix.
    """
    candidates = sorted(offered_services, key=len, reverse=True)
    for offered in candidates:
        needle = normalize_service_name(offered)
        if needle and re.search(rf"\b{re.escape(needle)}\b", normalized):
            return offered
    return None


@dataclass(frozen=True)
class ExtractedTurn:
    """The signals extracted from one patient utterance.

    Attributes:
        turn: The :class:`Turn` to hand to
            :meth:`~clinic_front_desk.voice.guardrails.GuardrailPolicy.classify`.
        transcript: The original transcript, unmodified.
        evidence: Signal name → the phrase that triggered it. Recorded on the
            escalation so a human picking up the handover can see *why* it
            escalated rather than guessing.
    """

    turn: Turn
    transcript: str
    evidence: dict[str, str] = field(default_factory=dict)

    @property
    def matched_any(self) -> bool:
        """Whether any non-administrative signal fired."""
        return bool(self.evidence)

    def describe(self) -> str:
        """A one-line, human-readable summary for the escalation context."""
        if not self.evidence:
            return f"patient said: {self.transcript!r}"
        signals = ", ".join(
            f"{name}={phrase!r}" for name, phrase in sorted(self.evidence.items())
        )
        return f"patient said: {self.transcript!r} | signals: {signals}"


def extract_turn(
    transcript: str,
    offered_services: Iterable[str] = (),
    *,
    escalation_offered: bool = False,
) -> ExtractedTurn:
    """Extract guardrail signals from a patient transcript.

    Args:
        transcript: The finalized patient utterance from the voice stream.
        offered_services: The clinic's offered-service names. A service is only
            recorded when the transcript names one of these (Req 2.1, 10.2).
        escalation_offered: Whether the agent has an outstanding offer to hand the
            call to a human. Only then is a bare affirmative read as accepting it
            (Req 9.8).

    Returns:
        An :class:`ExtractedTurn` carrying the :class:`Turn` and the evidence for
        each signal that fired.
    """
    normalized = normalize_transcript(transcript)
    evidence: dict[str, str] = {}

    def record(signal: str, patterns: Iterable[re.Pattern[str]]) -> bool:
        matched = _first_match(normalized, patterns)
        if matched is not None:
            evidence[signal] = matched
            return True
        return False

    # An emergency is clinical content: the policy checks that first, so this
    # declines and escalates immediately instead of merely offering.
    emergency = record("emergency", _EMERGENCY)
    clinical = record("requests_clinical_content", _CLINICAL_CONTENT)

    named = _named_service(normalized, offered_services)
    if named is not None:
        evidence["named_service"] = named

    turn = Turn(
        named_service=named,
        names_symptom=record("names_symptom", _SYMPTOM),
        asks_which_service=record("asks_which_service", _WHICH_SERVICE),
        requests_clinical_content=emergency or clinical,
        requests_policy_decision=record("requests_policy_decision", _POLICY_DECISION),
        outside_admin_rules=record("outside_admin_rules", _OUTSIDE_ADMIN_RULES),
        explicit_human_request=record("explicit_human_request", _HUMAN_REQUEST),
        accepts_escalation_offer=(
            escalation_offered and record("accepts_escalation_offer", _ACCEPTANCE)
        ),
        expresses_distress=record("expresses_distress", _DISTRESS),
    )
    return ExtractedTurn(turn=turn, transcript=transcript, evidence=evidence)
