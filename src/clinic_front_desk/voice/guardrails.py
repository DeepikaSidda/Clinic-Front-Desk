"""``GuardrailPolicy`` — administrative-only turn classification (task 7.3).

The Voice_Front_Desk enforces its administrative-only rule with defense in depth
(design "Guardrail enforcement"):

- **Prompt layer** — the administrative-only system prompt in
  :mod:`clinic_front_desk.voice.prompts` (Req 10.1, 10.6).
- **Tool layer** — *this module*. ``GuardrailPolicy`` classifies each patient
  turn as administrative vs. clinical / symptom-routing / out-of-rules and
  drives the refuse-and-escalate behavior. Crucially there is **no symptom →
  service mapping code path**: a service is selected only when the patient
  explicitly names an offered service (exact match). When routing would require
  interpreting a symptom, no service is selected and ``flag_for_human`` must be
  invoked (Req 10.2, 10.3, 10.4).

The policy is a deterministic decision over the *structured signals* a turn
carries (the interpretation the reasoning layer / Nova Sonic extracts), modelled
by :class:`Turn`. Keeping it a pure function of those signals is what lets the
guardrail be unit- and property-tested against every labelled turn category
without the voice stack (design "Testing Strategy": administrative, clinical,
symptom-only, symptom+named-service, distress, explicit-human, out-of-rules).

Classification precedence (safety first):

1. Clinical content (advice / triage / diagnosis / treatment / medication) —
   decline and escalate as ``clinical_content`` (Req 10.1, 10.6, 9.1).
2. Symptom routing with no patient-named offered service — no service selected,
   escalate as ``clinical_content`` (Req 10.3, 10.4).
3. Explicit request to speak to a human — escalate as ``patient_request``
   (Req 9.7).
4. Accepted an escalation offer — escalate as ``patient_distress`` (Req 9.8).
5. Clinical/policy decision or any request outside the administrative rules —
   escalate as ``outside_admin_rules`` (Req 9.2, 10.5).
6. Explicit anger / dissatisfaction / distress — *offer* to escalate as
   ``patient_distress`` (Req 9.3).
7. Otherwise administrative.

Note that service selection is independent of the escalation branch: if the
patient names an offered service, ``selected_service`` is set even on a turn
that also escalates (Req 10.2 — "a service is selected iff an offered service is
named").
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from clinic_front_desk.models import EscalationReason, is_ok
from clinic_front_desk.tools.service_matcher import match_offered_service


class TurnClassification(StrEnum):
    """The category the :class:`GuardrailPolicy` assigns to a patient turn."""

    #: A normal administrative/operational request (book, reschedule, cancel,
    #: waitlist, FAQ, patient lookup).
    ADMINISTRATIVE = "administrative"
    #: Clinical advice, triage, diagnosis, treatment, or medication — declined
    #: and escalated (Req 10.1, 10.6).
    CLINICAL_CONTENT = "clinical_content"
    #: A symptom for which routing would require interpretation and no offered
    #: service was named — escalated, no service selected (Req 10.3, 10.4).
    SYMPTOM_ROUTING = "symptom_routing"
    #: The caller described a symptom, or asked which service they need, and named
    #: no offered service — the general consultation is *offered* and no service is
    #: selected (Req 10.2, 10.3, 10.4). See
    #: :attr:`GuardrailDecision.offer_general_consultation`.
    UNSURE_WHICH_SERVICE = "unsure_which_service"
    #: A clinical/policy decision or a request outside the administrative rules
    #: (Req 9.2, 10.5).
    OUTSIDE_ADMIN_RULES = "outside_admin_rules"
    #: Explicit anger, dissatisfaction, or distress — an escalation is *offered*
    #: (Req 9.3).
    PATIENT_DISTRESS = "patient_distress"
    #: The patient explicitly asked for a human, or accepted an escalation offer
    #: (Req 9.7, 9.8).
    HUMAN_REQUEST = "human_request"


@dataclass(frozen=True)
class Turn:
    """The structured signals of a single patient turn.

    These are the interpreted features the reasoning layer / Nova Sonic extracts
    from what the patient said; ``GuardrailPolicy`` is a deterministic decision
    over them. All flags default to ``False`` / ``None`` so a turn only needs to
    set the signals that are present.

    Attributes:
        named_service: A service the patient *explicitly named*, if any. This is
            the only input that can lead to a service selection; it is matched
            against the clinic's offered services by exact name (Req 2.1, 10.2).
        names_symptom: The patient described a symptom (e.g. "my ear hurts").
        asks_which_service: The patient asked which service they should book.
            Never answered by interpreting their symptom; the general consultation
            is offered instead (Req 10.3).
        requests_clinical_content: The patient asked for clinical advice, symptom
            triage, a diagnosis, treatment, or medication guidance (Req 10.1,
            10.6, 9.1).
        requests_policy_decision: The patient asked for a clinical or
            clinic-policy decision (Req 10.5).
        outside_admin_rules: The request otherwise falls outside the agent's
            administrative rules (Req 9.2).
        explicit_human_request: The patient explicitly asked to speak with a
            human (Req 9.7).
        accepts_escalation_offer: The patient accepted an offer to escalate to a
            human (Req 9.8).
        expresses_distress: The patient explicitly stated anger, dissatisfaction,
            or distress (Req 9.3).
    """

    named_service: str | None = None
    names_symptom: bool = False
    asks_which_service: bool = False
    requests_clinical_content: bool = False
    requests_policy_decision: bool = False
    outside_admin_rules: bool = False
    explicit_human_request: bool = False
    accepts_escalation_offer: bool = False
    expresses_distress: bool = False


@dataclass(frozen=True)
class GuardrailDecision:
    """The outcome of classifying a :class:`Turn`.

    Attributes:
        classification: The assigned :class:`TurnClassification`.
        is_administrative: ``True`` iff the turn is a plain administrative
            request the agent may handle itself.
        requires_escalation: ``True`` iff ``flag_for_human`` must be invoked now
            (Req 10.4, 9.1, 9.2, 9.7, 9.8, 10.5). Distress alone does not set
            this — it is *offered* via :attr:`offer_escalation` (Req 9.3).
        escalation_reason: The :class:`~clinic_front_desk.models.EscalationReason`
            to record if the turn escalates (or would escalate on an accepted
            offer); ``None`` for a purely administrative turn.
        selected_service: The exact offered-service name when the patient named
            an offered service, else ``None``. Set independently of escalation
            (Req 10.2 — selected iff an offered service is named).
        offer_escalation: ``True`` when the agent should *offer* to escalate
            rather than escalate immediately (distress, Req 9.3).
        decline_clinical_content: ``True`` when the agent must decline the
            clinical content and state that clinical questions are handled by
            clinic staff (Req 10.6).
        offer_general_consultation: The general consultation to *offer* when the
            caller described a symptom or asked which service they need, else
            ``None``. Offering it is not triage: it is the same answer for every
            symptom, so it conveys no clinical judgement — the doctor examines the
            patient and decides. The service is still not *selected* here; the
            caller has to accept it, at which point they have named it themselves
            and the ordinary named-service path applies (Req 10.2).
    """

    classification: TurnClassification
    is_administrative: bool
    requires_escalation: bool
    escalation_reason: EscalationReason | None
    selected_service: str | None
    offer_escalation: bool = False
    decline_clinical_content: bool = False
    offer_general_consultation: str | None = None

    @property
    def should_flag_for_human(self) -> bool:
        """Tool-layer signal: ``flag_for_human`` must be invoked for this turn.

        Alias of :attr:`requires_escalation`, named to match the design's
        tool-layer guardrail note (Req 10.4).
        """
        return self.requires_escalation


class GuardrailPolicy:
    """Classifies patient turns and enforces the administrative-only rule.

    The policy is constructed with the clinic's offered-service names so it can
    resolve a patient-named service to an offered service (exact match). It holds
    no other state; :meth:`classify` is a pure function of the given
    :class:`Turn` and the offered services.
    """

    def __init__(
        self,
        offered_services: Iterable[str] = (),
        *,
        general_consultation: str | None = None,
    ) -> None:
        """Create a policy.

        Args:
            offered_services: The clinic's offered-service names (e.g. from
                :func:`clinic_front_desk.tools.service_matcher.offered_service_names`).
                A patient-named service is selected only when it exactly matches
                one of these.
            general_consultation: The clinic's general consultation, offered when
                a caller describes a symptom or cannot say which service they
                need. Must exactly match one of ``offered_services`` or it is
                ignored — an offer for a service the clinic does not have is worse
                than no offer. ``None`` (the default) restores the original
                behaviour of escalating such a turn to a human.
        """
        self._offered_services: tuple[str, ...] = tuple(offered_services)
        resolved: str | None = None
        if general_consultation:
            matched = match_offered_service(general_consultation, self._offered_services)
            resolved = matched.value if is_ok(matched) else None
        self._general_consultation = resolved

    @property
    def offered_services(self) -> tuple[str, ...]:
        """The offered-service names this policy matches against."""
        return self._offered_services

    @property
    def general_consultation(self) -> str | None:
        """The consultation offered when a caller cannot name a service."""
        return self._general_consultation

    def resolve_named_service(self, named_service: str | None) -> str | None:
        """Return the offered service the patient named, or ``None``.

        This is the *only* path from a patient utterance to a service: an exact
        match against the offered services (Req 2.1, 10.2). A symptom or any
        unnamed/unoffered value never yields a service — there is deliberately
        no symptom→service mapping (Req 10.3).
        """
        if named_service is None:
            return None
        matched = match_offered_service(named_service, self._offered_services)
        return matched.value if is_ok(matched) else None

    def classify(self, turn: Turn) -> GuardrailDecision:
        """Classify a patient turn and decide the guardrail response.

        Args:
            turn: The structured signals of the patient's turn.

        Returns:
            A :class:`GuardrailDecision` stating the classification, whether the
            turn is administrative, whether it requires escalation and with what
            :class:`~clinic_front_desk.models.EscalationReason`, and the selected
            offered service (if the patient named one).
        """
        # Service selection is independent of the escalation decision: a service
        # is selected iff the patient named an offered service (Req 10.2).
        selected_service = self.resolve_named_service(turn.named_service)

        # 1. Clinical content — always declined and escalated (Req 10.1, 10.6, 9.1).
        if turn.requests_clinical_content:
            return GuardrailDecision(
                classification=TurnClassification.CLINICAL_CONTENT,
                is_administrative=False,
                requires_escalation=True,
                escalation_reason=EscalationReason.CLINICAL_CONTENT,
                selected_service=selected_service,
                decline_clinical_content=True,
            )

        # 3. Explicit request to speak with a human (Req 9.7).
        if turn.explicit_human_request:
            return GuardrailDecision(
                classification=TurnClassification.HUMAN_REQUEST,
                is_administrative=False,
                requires_escalation=True,
                escalation_reason=EscalationReason.PATIENT_REQUEST,
                selected_service=selected_service,
            )

        # 4. Accepted an escalation offer (Req 9.8) — escalate now.
        if turn.accepts_escalation_offer:
            return GuardrailDecision(
                classification=TurnClassification.HUMAN_REQUEST,
                is_administrative=False,
                requires_escalation=True,
                escalation_reason=EscalationReason.PATIENT_DISTRESS,
                selected_service=selected_service,
            )

        # 5. Clinical/policy decision or any out-of-rules request (Req 9.2, 10.5).
        if turn.requests_policy_decision or turn.outside_admin_rules:
            return GuardrailDecision(
                classification=TurnClassification.OUTSIDE_ADMIN_RULES,
                is_administrative=False,
                requires_escalation=True,
                escalation_reason=EscalationReason.OUTSIDE_ADMIN_RULES,
                selected_service=selected_service,
            )

        # 6. Distress — offer to escalate rather than escalate immediately (Req 9.3).
        if turn.expresses_distress:
            return GuardrailDecision(
                classification=TurnClassification.PATIENT_DISTRESS,
                is_administrative=False,
                requires_escalation=False,
                escalation_reason=EscalationReason.PATIENT_DISTRESS,
                selected_service=selected_service,
                offer_escalation=True,
            )

        # 7. A symptom, or "which service do I need", with no service named.
        #
        #    Routing this by interpreting the symptom is forbidden and always will
        #    be (Req 10.3): "nasal itching therefore Allergy Testing" is triage, and
        #    a wrong test delays a real diagnosis. But refusing outright and
        #    offering a human, which is what this used to do, sends away a caller
        #    the clinic could have seen. Observed live: a caller described itching
        #    and asked which service to pick, and the call ended in a handover.
        #
        #    So the general consultation is *offered* instead. That is not triage:
        #    it is the same answer for every symptom, so it carries no clinical
        #    information, and a consultation is precisely the appointment at which
        #    the doctor decides what is needed. The service is not selected here —
        #    the caller must accept it, and then they have named it themselves, so
        #    "a service is selected iff the patient named one" still holds
        #    (Req 10.2).
        #
        #    This branch sits after distress and human-request handling on purpose:
        #    a caller who is upset, or who asked for a person, still gets a person.
        #    Clinical questions and emergencies are caught by branch 1 above and
        #    never reach here.
        if (turn.names_symptom or turn.asks_which_service) and selected_service is None:
            if self._general_consultation is not None:
                return GuardrailDecision(
                    classification=TurnClassification.UNSURE_WHICH_SERVICE,
                    is_administrative=False,
                    requires_escalation=False,
                    escalation_reason=None,
                    selected_service=None,
                    decline_clinical_content=True,
                    offer_general_consultation=self._general_consultation,
                )
            # No general consultation configured: there is nothing safe to offer,
            # so fall back to the original behaviour rather than guess a service.
            return GuardrailDecision(
                classification=TurnClassification.SYMPTOM_ROUTING,
                is_administrative=False,
                requires_escalation=True,
                escalation_reason=EscalationReason.CLINICAL_CONTENT,
                selected_service=None,
                decline_clinical_content=True,
            )

        # 8. Otherwise a plain administrative turn.
        return GuardrailDecision(
            classification=TurnClassification.ADMINISTRATIVE,
            is_administrative=True,
            requires_escalation=False,
            escalation_reason=None,
            selected_service=selected_service,
        )


__all__ = [
    "TurnClassification",
    "Turn",
    "GuardrailDecision",
    "GuardrailPolicy",
]
