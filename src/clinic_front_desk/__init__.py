"""Clinic Front-Desk Voice Agent.

A two-agent system for a solo-doctor ENT clinic:

- ``Voice_Front_Desk`` — a reactive, patient-facing Strands ``BidiAgent`` voiced
  by Amazon Nova Sonic. It books, reschedules, cancels, answers FAQs, waitlists,
  looks up patients, and escalates to a human. Strictly administrative.
- ``Practice_Intelligence`` — an autonomous, doctor-facing background agent that
  analyzes accumulated data and surfaces actionable Decisions.

All persistence goes through the swappable, provider-aware ``data_layer``
interfaces; agents never touch storage directly.

Package layout
--------------
- :mod:`clinic_front_desk.models` — domain data models and shared Result/error types.
- :mod:`clinic_front_desk.data_layer` — per-entity store interfaces, in-memory
  fakes, DynamoDB implementations, and change events.
- :mod:`clinic_front_desk.config` — clinic onboarding/configuration domain logic.
- :mod:`clinic_front_desk.tools` — the Strands tool suite.
- :mod:`clinic_front_desk.voice` — Nova Sonic voice integration and orchestration.
- :mod:`clinic_front_desk.intelligence` — Practice_Intelligence detectors/scheduler.
- :mod:`clinic_front_desk.dashboard` — the role-aware dashboard backend-for-frontend.
- :mod:`clinic_front_desk.app` — the composition root: ``build_application`` wires
  all four subsystems over one shared Data_Layer + change channel (Req 16.1).
- :mod:`clinic_front_desk.runtime` — the AgentCore Runtime entrypoint surface
  (voice WebSocket handler + scheduled intelligence handler).
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
