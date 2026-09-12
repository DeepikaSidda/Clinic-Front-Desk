"""Answering the descriptive long tail from the doctor's uploaded documents.

The six FAQ topics cover the fields the onboarding wizard collects. They cannot
answer "is there parking", "which floor", "are you open on Thanksgiving", "what is
your cancellation policy" — none of which has a column in
:class:`~clinic_front_desk.models.ClinicKnowledgeBase`, and all of which the doctor
already has written down somewhere. :class:`DocumentKnowledge` is the bridge: it
searches those uploads and hands back a passage.

Three rules make this safe to put in front of a caller.

**The answer is the doctor's own words, verbatim.** No summarizing model runs over
the retrieved passage. The passage is returned as stored, so the worst case is an
irrelevant true sentence, never an invented one. There is no generation step in
which a plausible falsehood could appear.

**A passage must clear an absolute relevance floor.** Embeddings are unit-normalized
at ingest, so cosine scores are comparable across passages and an absolute threshold
is meaningful. Below :data:`MIN_RELEVANCE` the lookup reports nothing found — which
routes into the FAQ tool's existing "I don't have that information" path. A weak
match is the exact circumstance in which retrieval would otherwise read out
something confidently wrong, so the floor is the no-fabrication guarantee, not a
tuning knob.

**Clinical passages are refused.** A practice handout routinely mixes logistics
with clinical guidance — the allergy-shot sheet that explains walk-in hours also
lists the dosing schedule. The caller-facing :class:`GuardrailPolicy` does escalate
clinical turns, but it decides over *signals Nova Sonic extracted*: it escalates
when the model set ``requests_clinical_content``, and a turn the model does not flag
is classified administrative and reaches the tools normally. Verified by
construction — a bare ``Turn()`` classifies as administrative with no escalation.

So the upstream check is model-dependent, and :func:`looks_clinical` is the only
*deterministic* barrier on this path. It screens the retrieved text rather than the
question, which is the property that matters here: whatever the caller asked and
however the model classified it, clinical prose does not leave this module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from clinic_front_desk.data_layer.interfaces import ClinicDocumentStore
from clinic_front_desk.models import (
    DocumentChunk,
    Err,
    NotFound,
    Ok,
    RetrievedChunk,
    ToolResult,
    is_err,
)

from .embeddings import Embedder, search_chunks

logger = logging.getLogger(__name__)

#: Minimum cosine similarity for a passage to be spoken to a caller.
#:
#: Measured, not guessed. Against live Titan V2 (512 dimensions, normalized) over a
#: representative clinic information sheet chunked one topic per section:
#:
#: - questions the sheet answers scored **0.21 – 0.71** (typically 0.43+, and every
#:   one retrieved the correct section)
#: - questions the sheet does not mention scored **0.05 – 0.26**
#: - clinical questions scored **0.09 – 0.17**
#:
#: 0.30 clears the highest unrelated score observed while keeping the clearly
#: on-topic cases. The two on-topic questions below it ("which floor are you on" at
#: 0.21, "can I pay by cheque" at 0.30) fall to the honest "I don't have that"
#: path, which is the right direction to err: a floor too high loses an answer the
#: clinic had and the caller is offered a message, while a floor too low reads out
#: a passage that does not answer the question — the failure this module exists to
#: prevent.
#:
#: The margin above unrelated text is real but not large, which is worth knowing
#: before widening this. It is only this workable because chunks are one topic
#: each; with chunks spanning several topics the two populations overlapped
#: completely and **no** threshold separated them. If scores start looking wrong,
#: suspect the chunking before this number.
MIN_RELEVANCE = 0.30

#: How many passages to consider. Only the best is spoken; the rest are scored so
#: the margin between first and second is visible in the logs when tuning.
SEARCH_LIMIT = 4

#: A passage longer than this is trimmed at a sentence boundary before being
#: spoken. Chunks run to ~900 characters, which is far more than anyone wants read
#: aloud, and a caller stops listening long before the end.
MAX_SPOKEN_CHARS = 480

# Markers of clinical content in a *retrieved passage*.
#
# Deliberately separate from the caller-turn guardrail: that one classifies
# questions ("should I take my medication"), this one classifies prose from a
# document ("take 500 mg twice daily"). The vocabularies barely overlap, and
# merging them would make both worse.
#
# Tuned to avoid the obvious administrative false positives: a clinic document
# saying "bring a list of your medications" or "we treat patients of all ages" is
# administrative and must still be answerable, so bare "medication" and "treat"
# are not markers on their own.
_CLINICAL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Dosing instructions in any form.
        r"\b\d+\s*(?:mg|mcg|ml|g|iu|units?)\b",
        r"\b(?:once|twice|thrice|\d+\s*times)\s+(?:a|per)\s+day\b",
        r"\b(?:take|administer|inject|apply)\s+\d",
        r"\bdos(?:e|es|age|ing)\b",
        # Diagnosis, prescription, and treatment-selection language.
        r"\bdiagnos(?:e|ed|is|es|ing|tic)\b",
        r"\bprescrib(?:e|ed|es|ing)\b",
        r"\bprescription\s+(?:for|of)\b",
        # Only a treatment *decision*, not a service that happens to be named
        # "Sinus Treatment for sinusitis" — a menu entry is administrative, and
        # matching bare "treatment for" silently dropped the whole services list.
        r"\btreatment\s+(?:plan|protocol|regimen)\b",
        r"\b(?:recommended|prescribed|suggested)\s+treatment\b",
        r"\bcontraindicat\w*",
        r"\bside\s+effects?\b",
        r"\badverse\s+(?:reaction|event)s?\b",
        # Triage / symptom-interpretation language.
        r"\bsymptoms?\s+(?:of|include|may\s+include)\b",
        # "if you experience/develop/notice" precedes triage advice. "if you have"
        # and "if you feel" were included and should not have been: "if you have
        # insurance" and "if you have had a hearing test elsewhere, bring the
        # audiogram" are logistics, and matching them dropped the entire
        # what-to-bring section.
        r"\bif\s+you\s+(?:experience|develop|notice)\b",
        r"\bseek\s+(?:immediate\s+)?(?:medical|emergency)\b",
        r"\bcall\s+9-?1-?1\b",
    )
)


def looks_clinical(text: str) -> bool:
    """Whether ``text`` reads as clinical guidance rather than practice logistics.

    Used to refuse a retrieved passage, so it is intentionally trigger-happy: the
    cost of a false positive is one unanswered administrative question, and the
    cost of a false negative is an AI voice reading medical instructions to a
    caller. Those are not comparable, so this fails closed.

    Measured against a handout mixing both kinds of content: the "Dosing schedule"
    and "Reactions" sections are flagged, while "Scheduling your injections",
    "Waiting period after your injection" and "Billing for injections" are not — so
    the walk-in hours stay answerable while the dosing table does not.

    This screens a *passage*, which is why it is not shared with the caller-turn
    guardrail. Document prose says "take 25 mg of diphenhydramine"; a caller says
    "should I take something for the itching". Same concern, almost no shared
    vocabulary, and one list tuned for both would be worse at each.
    """
    return any(pattern.search(text) for pattern in _CLINICAL_PATTERNS)


def _trim_for_speech(text: str, limit: int = MAX_SPOKEN_CHARS) -> str:
    """Collapse whitespace and cut ``text`` to ``limit`` at a sentence boundary.

    Cutting on a sentence end rather than mid-word matters here: the passage is
    spoken, and a sentence that stops halfway sounds like the agent malfunctioned.
    """
    collapsed = re.sub(r"\s+", " ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    window = collapsed[: limit + 1]
    cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
    if cut >= limit // 3:
        return window[: cut + 1].strip()
    # No sentence break in range: fall back to the last word boundary so the
    # answer at least ends on a whole word.
    space = window.rfind(" ")
    return (window[:space] if space > 0 else window[:limit]).strip() + "\u2026"


@dataclass
class DocumentKnowledge:
    """Looks up clinic questions in the documents the doctor uploaded.

    Holds the store and the embedder together because a lookup needs both, and
    threading two collaborators through every call site of ``answer_faq`` would be
    noise. Construct one per application, not per call.

    Attributes:
        store: Where the chunks live.
        embedder: Embeds the caller's question for scoring.
        min_relevance: The floor a passage must clear to be spoken.
        cache_chunks: Whether to hold the corpus in memory after the first lookup.
            Worth it in production, where the corpus is a few hundred chunks and
            the alternative is an S3 read on every question mid-call. Call
            :meth:`invalidate` after an upload or delete.
    """

    store: ClinicDocumentStore
    embedder: Embedder
    min_relevance: float = MIN_RELEVANCE
    cache_chunks: bool = True
    _chunks: list[DocumentChunk] | None = field(default=None, init=False, repr=False)

    def invalidate(self) -> None:
        """Drop the cached corpus, so the next lookup re-reads the store.

        Called when a document is added or removed. Without this a mid-session
        upload would not be answerable until the process restarted.
        """
        self._chunks = None

    def _load_chunks(self) -> list[DocumentChunk] | None:
        """Return every chunk, or ``None`` if the store read failed."""
        if self.cache_chunks and self._chunks is not None:
            return self._chunks
        result = self.store.list_chunks()
        if is_err(result):
            # Not fatal: the caller falls back to the structured answer path, which
            # will report the information as unavailable. Logged because a store
            # that cannot be read is an operational problem even when the call
            # degrades gracefully.
            logger.warning("document chunk read failed: %s", result.error.detail)
            return None
        chunks = list(result.value)
        if self.cache_chunks:
            self._chunks = chunks
        return chunks

    def lookup(self, question: str) -> ToolResult[str]:
        """Answer ``question`` from the uploaded documents, or report nothing found.

        Args:
            question: The caller's question, in their own words. Passing a topic
                label instead ("location") works but retrieves far less
                accurately — the embedding of a real question carries the intent
                that makes the right passage win.

        Returns:
            ``Ok(passage)`` with the doctor's own text when a passage clears the
            relevance floor and is not clinical, otherwise
            ``Err(NotFound)``. Never returns generated prose.
        """
        if not question or not question.strip():
            return Err(NotFound(detail="no question to look up in clinic documents"))

        chunks = self._load_chunks()
        if not chunks:
            return Err(NotFound(detail="no clinic documents are available"))

        found = search_chunks(
            question, chunks, self.embedder, limit=SEARCH_LIMIT, min_score=0.0
        )
        if not found.matches:
            return Err(NotFound(detail="no clinic document passage matched"))

        best = found.matches[0]
        if best.score < self.min_relevance:
            # The corpus has nothing on this. Saying so is the whole point: this is
            # where a retrieval system without a floor invents an answer.
            logger.info(
                "document lookup below floor (%.3f < %.3f) for %r",
                best.score,
                self.min_relevance,
                question,
            )
            return Err(
                NotFound(detail="no clinic document covers that question closely enough")
            )

        if looks_clinical(best.chunk.text):
            # An administrative question must not become a route to medical
            # guidance just because a document happens to contain some.
            logger.info(
                "document lookup refused clinical passage %s#%d for %r",
                best.chunk.document_id,
                best.chunk.index,
                question,
            )
            return Err(
                NotFound(
                    detail="the matching document passage is clinical, "
                    "which this agent does not answer"
                )
            )

        _log_citation(question, best, found.matches)
        return Ok(_trim_for_speech(best.chunk.text))


def _log_citation(
    question: str, best: RetrievedChunk, matches: list[RetrievedChunk]
) -> None:
    """Record which passage answered, for the audit trail.

    The citation is logged rather than spoken: a caller does not care that the
    answer came from page 2 of ``practice-info.pdf``, but when someone later asks
    why the agent said what it said, that is exactly what is needed. The
    runner-up score is included because the gap between first and second is the
    signal for whether the floor is set sensibly.
    """
    runner_up = matches[1].score if len(matches) > 1 else 0.0
    logger.info(
        "document lookup answered %r from %s#%d page=%s score=%.3f runner_up=%.3f",
        question,
        best.chunk.document_id,
        best.chunk.index,
        best.chunk.page,
        best.score,
        runner_up,
    )


__all__ = [
    "DocumentKnowledge",
    "MIN_RELEVANCE",
    "SEARCH_LIMIT",
    "MAX_SPOKEN_CHARS",
    "looks_clinical",
]
