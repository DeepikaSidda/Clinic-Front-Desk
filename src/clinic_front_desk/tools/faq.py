"""The ``answer_faq`` Strands tool (task 6.8, Req 6.1–6.6).

``answer_faq`` retrieves clinic information from the
:class:`~clinic_front_desk.data_layer.interfaces.ClinicKnowledgeBaseStore` and
returns a spoken-ready answer string wrapped in a :data:`ToolResult`. It answers
six topics — ``hours``, ``location``, ``what_to_bring``, ``prep``, ``insurance``
and ``pricing`` (Req 6.1).

Guiding invariants (design "Strands Tool Suite", Property 9):

- **Never fabricate.** When the requested information is absent from the
  Clinic_Knowledge_Base the tool returns an ``Err`` (an "unavailable" result),
  never a made-up answer (Req 6.3, 6.5).
- **Pricing requires a service.** ``pricing`` without a ``service`` is a
  validation error; a service that is not offered, or that has no configured
  price, yields an unavailable result (Req 6.4, 6.5).
- **Store/tool failures surface as ``store_failure``.** If the knowledge-base
  read fails, the tool returns a ``StoreFailure`` so the orchestrator can offer
  to take a message (Req 6.6).

The tool is a pure function over the store: deterministic given the store state,
returning a typed result object (design "All tools are pure-ish functions over
the Data_Layer").
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, get_args

from clinic_front_desk.data_layer.interfaces import ClinicKnowledgeBaseStore
from clinic_front_desk.models import (
    ClinicKnowledgeBase,
    Err,
    NotFound,
    NotOffered,
    Ok,
    ServiceConfig,
    StoreFailure,
    ToolError,
    ToolResult,
    Validation,
    is_err,
    is_ok,
)

if TYPE_CHECKING:  # pragma: no cover - import kept out of runtime
    from clinic_front_desk.documents.retrieval import DocumentKnowledge

# The six FAQ topics the tool answers (design ``answer_faq`` signature, Req 6.1).
FaqTopic = Literal["hours", "location", "what_to_bring", "prep", "insurance", "pricing"]

#: The set of recognised **structured** topics, derived from :data:`FaqTopic` so the
#: two stay in lock-step. Each maps to fields the onboarding wizard collects.
VALID_TOPICS: frozenset[str] = frozenset(get_args(FaqTopic))

#: The document-backed topic (Req 6.1 long tail).
#:
#: The six structured topics mirror the Clinic_Knowledge_Base schema, so anything
#: the doctor knows but the schema has no field for — parking, which floor, holiday
#: closures, accessibility, cancellation policy, what the practice actually does —
#: had no topic at all and was simply unanswerable. This one carries the caller's
#: own question into the uploaded documents. It is separate from the six rather
#: than folded into them because it has no structured answer to be authoritative
#: over: it is document-only by definition.
DOCUMENT_TOPIC = "clinic_info"

#: Every topic the tool accepts.
ANSWERABLE_TOPICS: frozenset[str] = VALID_TOPICS | {DOCUMENT_TOPIC}

#: Structured topics that may fall back to the documents when the configuration
#: has no answer.
#:
#: ``pricing`` is deliberately excluded. A price is money, and a number lifted out
#: of a document could be last year's, a different plan's, or another clinic's on a
#: comparison sheet — quoted in a voice the caller will reasonably treat as the
#: clinic's word. Prices come from the configured service record or the caller is
#: told the price is unavailable. This is the same reasoning that keeps offered
#: services out of retrieval: values that *drive commitments* stay structured.
DOCUMENT_FALLBACK_TOPICS: frozenset[str] = VALID_TOPICS - {"pricing"}

#: Queries used when the caller's own wording was not passed through.
#:
#: Embedding the bare topic token ("hours") retrieves poorly — it carries none of
#: the intent that makes the right passage win. These stand in as a description of
#: what is being asked for. A real question is still much better, which is why
#: ``question`` exists.
_TOPIC_QUERIES: dict[str, str] = {
    "hours": "clinic opening hours, days open, and holiday closures",
    "location": "clinic address, directions, parking, and which floor",
    "insurance": "accepted insurance plans and payment options",
    "prep": "how to prepare before an appointment",
    "what_to_bring": "what to bring to an appointment",
}

_STORE_LABEL = "ClinicKnowledgeBaseStore"

# Weekday index (0 = Sunday .. 6 = Saturday) -> display name, matching the
# ``hours`` map keys on :class:`ClinicKnowledgeBase`.
_WEEKDAY_NAMES: tuple[str, ...] = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)


def answer_faq(
    store: ClinicKnowledgeBaseStore,
    topic: str,
    service: str | None = None,
    *,
    question: str | None = None,
    documents: DocumentKnowledge | None = None,
) -> ToolResult[str]:
    """Answer a clinic FAQ from the Clinic_Knowledge_Base (Req 6.1–6.6).

    Configuration is authoritative. When ``documents`` is supplied, the doctor's
    uploaded documents act as a *fallback* for the descriptive topics — consulted
    only where the configuration has no answer, never in place of one. The order
    matters: a document is prose that happened to match a question, while the
    configuration is what the doctor explicitly entered and confirmed.

    Args:
        store: The knowledge-base store to read configuration from.
        topic: One of :data:`ANSWERABLE_TOPICS`. An unrecognised topic is a
            validation error — the tool never guesses among topics (Req 6.7).
        service: The named service. Required for ``pricing`` (Req 6.4); used to
            scope ``prep``/``what_to_bring`` answers when provided.
        question: The caller's question in their own words. Only used for
            document retrieval, where it substantially improves which passage is
            found. Required in practice for :data:`DOCUMENT_TOPIC`.
        documents: The uploaded-document corpus. ``None`` — the default — means
            the tool answers purely from configuration, exactly as before.

    Returns:
        ``Ok(answer)`` with a spoken-ready string when the information is
        present, or an ``Err(ToolError)`` when it is unavailable
        (``not_found``/``not_offered``), the input is invalid (``validation``),
        or the store read fails (``store_failure``). The tool never fabricates
        an answer for absent information — including in the document path, where
        the answer is the stored passage verbatim and no text is generated.
    """
    if topic not in ANSWERABLE_TOPICS:
        return Err(
            Validation(
                field="topic",
                detail=(
                    f"unknown FAQ topic {topic!r}; expected one of "
                    f"{', '.join(sorted(ANSWERABLE_TOPICS))}"
                ),
            )
        )

    if topic == DOCUMENT_TOPIC:
        # Document-only by definition: there is no configured field to be
        # authoritative here, and no knowledge-base read to make. This also means
        # the long tail is answerable before onboarding is finished.
        if question is None or not question.strip():
            return Err(
                Validation(
                    field="question",
                    detail=(
                        f"the {DOCUMENT_TOPIC!r} topic needs the caller's question"
                    ),
                )
            )
        return _from_documents(documents, question)

    # Read the knowledge base. A read failure is surfaced as store_failure so
    # the orchestrator can offer to take a message (Req 6.6). Note this is *not*
    # papered over with a document guess: a Data_Layer that cannot be read is a
    # problem the caller should be told about via "let me take a message", not
    # hidden behind an answer that may contradict the configuration.
    result = store.get()
    if is_err(result):
        return Err(StoreFailure(store=_STORE_LABEL, detail=result.error.detail))

    kb = result.value
    if kb is None:
        # No configuration onboarded yet: the information is unavailable rather
        # than fabricated (Req 6.3). Documents may still cover it — a doctor can
        # upload the practice sheet before finishing the wizard.
        structured: ToolResult[str] = Err(
            NotFound(detail="clinic information is not yet available")
        )
    elif topic == "pricing":
        structured = _answer_pricing(kb, service)
    elif topic == "hours":
        structured = _answer_hours(kb)
    elif topic == "location":
        structured = _answer_location(kb)
    elif topic == "insurance":
        structured = _answer_insurance(kb)
    else:  # topic in {"prep", "what_to_bring"}
        structured = _answer_prep(kb, service, topic)

    if is_ok(structured):
        return structured
    return _maybe_fall_back(structured, topic, question, documents)


def _maybe_fall_back(
    structured: Err[ToolError],
    topic: str,
    question: str | None,
    documents: DocumentKnowledge | None,
) -> ToolResult[str]:
    """Try the documents for a structured topic the configuration cannot answer.

    Only ``not_found`` falls through. The other failures each mean something a
    document must not override:

    - ``not_offered`` is the guardrail that stops the agent discussing a service
      the clinic does not provide (Req 2.9, 10.2). If a document mentions a
      service the doctor did not configure, answering from it would re-introduce
      exactly the inference that check exists to prevent.
    - ``validation`` means the request was malformed (pricing with no service, an
      unknown topic). Retrieval cannot repair a malformed request, and trying
      would turn a clear error into a vague answer.
    - ``store_failure`` needs to stay visible so the caller is offered a message
      (Req 6.6).
    """
    if documents is None or not isinstance(structured.error, NotFound):
        return structured
    if topic not in DOCUMENT_FALLBACK_TOPICS:
        return structured

    retrieved = _from_documents(documents, question or _TOPIC_QUERIES.get(topic, topic))
    # A retrieval miss must not replace the configuration's more specific reason
    # for having no answer ("no configured price for 'X'"), so the original error
    # is what surfaces.
    return retrieved if is_ok(retrieved) else structured


def _from_documents(
    documents: DocumentKnowledge | None, question: str
) -> ToolResult[str]:
    """Look ``question`` up in the uploaded documents, if any are configured."""
    if documents is None:
        return Err(NotFound(detail="clinic information is not available"))
    return documents.lookup(question)


# ---------------------------------------------------------------------------
# Per-topic answer builders. Each returns an unavailable ``Err`` rather than a
# fabricated string when the underlying data is absent (Req 6.3, 6.5).
# ---------------------------------------------------------------------------


def _answer_pricing(kb: ClinicKnowledgeBase, service: str | None) -> ToolResult[str]:
    """Answer a pricing question for a named service (Req 6.4, 6.5)."""
    if service is None or not service.strip():
        return Err(
            Validation(
                field="service",
                detail="a service name is required to answer a pricing question",
            )
        )

    matched = _match_service(kb, service)
    if matched is None:
        # Service is not offered: price is unavailable (Req 6.5).
        return Err(NotOffered(named_service=service))
    if matched.price is None:
        # Offered but no configured price: unavailable, not fabricated (Req 6.5).
        return Err(
            NotFound(detail=f"no configured price for service {matched.name!r}")
        )

    return Ok(f"The price for {matched.name} is ${matched.price:.2f}.")


def _answer_hours(kb: ClinicKnowledgeBase) -> ToolResult[str]:
    """Answer a clinic-hours question (Req 6.1)."""
    open_days = [
        (day, kb.hours[day])
        for day in range(len(_WEEKDAY_NAMES))
        if kb.hours.get(day) is not None
    ]
    if not open_days:
        return Err(NotFound(detail="clinic hours are not available"))

    parts = [
        f"{_WEEKDAY_NAMES[day]} {hours.open}\u2013{hours.close}"
        for day, hours in open_days
        if hours is not None
    ]
    return Ok("Our hours are: " + "; ".join(parts) + ".")


def _answer_location(kb: ClinicKnowledgeBase) -> ToolResult[str]:
    """Answer a clinic-location question (Req 6.1)."""
    if not kb.location or not kb.location.strip():
        return Err(NotFound(detail="clinic location is not available"))
    return Ok(f"The clinic is located at {kb.location}.")


def _answer_insurance(kb: ClinicKnowledgeBase) -> ToolResult[str]:
    """Answer an accepted-insurance question (Req 6.1)."""
    accepted = [ins for ins in kb.accepted_insurance if ins and ins.strip()]
    if not accepted:
        return Err(NotFound(detail="accepted insurance information is not available"))
    return Ok("We accept the following insurance: " + ", ".join(accepted) + ".")


def _answer_prep(
    kb: ClinicKnowledgeBase, service: str | None, topic: str
) -> ToolResult[str]:
    """Answer a preparation / what-to-bring question (Req 6.1).

    Preparation instructions are configured per service. When a service is
    named the answer is scoped to that service; otherwise the instructions of
    every service that has them are combined. Absence yields an unavailable
    result, never a fabricated one (Req 6.3).
    """
    label = "what to bring" if topic == "what_to_bring" else "preparation instructions"

    if service is not None and service.strip():
        matched = _match_service(kb, service)
        if matched is None:
            return Err(NotOffered(named_service=service))
        if not matched.prep_instructions or not matched.prep_instructions.strip():
            return Err(
                NotFound(detail=f"no {label} available for service {matched.name!r}")
            )
        return Ok(matched.prep_instructions)

    with_prep = [
        svc
        for svc in kb.services
        if svc.prep_instructions and svc.prep_instructions.strip()
    ]
    if not with_prep:
        return Err(NotFound(detail=f"{label} are not available"))
    if len(with_prep) == 1:
        return Ok(with_prep[0].prep_instructions or "")
    return Ok("; ".join(f"{svc.name}: {svc.prep_instructions}" for svc in with_prep))


def _match_service(kb: ClinicKnowledgeBase, name: str) -> ServiceConfig | None:
    """Return the offered service whose name equals ``name`` (Req 2.1), else ``None``.

    Matching is exact on the trimmed name and case-insensitive so ordinary
    spoken variation ("MRI" vs "mri") still resolves to the offered service;
    it never infers a service from anything other than the name.
    """
    target = name.strip().casefold()
    for svc in kb.services:
        if svc.name.strip().casefold() == target:
            return svc
    return None


__all__ = [
    "answer_faq",
    "FaqTopic",
    "VALID_TOPICS",
    "DOCUMENT_TOPIC",
    "ANSWERABLE_TOPICS",
    "DOCUMENT_FALLBACK_TOPICS",
]
