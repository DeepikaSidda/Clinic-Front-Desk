"""Unit test: an analysis run invokes ``analyze_patterns`` (task 10.8, Req 13.2).

Practice_Intelligence must, on each run, "read the accumulated appointment,
waitlist, and call-session data and run pattern analysis" (Req 13.2). The
scheduled analysis entrypoint is
:meth:`~clinic_front_desk.deployment.app.ClinicFrontDeskApplication.run_intelligence`,
which assembles a :class:`~clinic_front_desk.intelligence.detectors.PatternInput`
snapshot from the shared Data_Layer and hands it to ``analyze_patterns``.

This test seeds one appointment, one waitlist entry, and one call session through
the shared stores, spies on the ``analyze_patterns`` call the run makes, and
asserts the run invokes it exactly once over a snapshot carrying all three record
types.
"""

from __future__ import annotations

import clinic_front_desk.deployment.app as app_module
from clinic_front_desk.deployment.app import build_memory_application
from clinic_front_desk.intelligence import analyze_patterns as real_analyze_patterns
from clinic_front_desk.intelligence.detectors import PatternInput
from clinic_front_desk.models import (
    Appointment,
    AppointmentStatus,
    CallSession,
    ClinicKnowledgeBase,
    Provider,
    ServiceConfig,
    WaitlistEntry,
    is_ok,
)

TODAY = "2024-05-10"


def test_analysis_run_invokes_analyze_patterns_over_all_data(monkeypatch) -> None:
    app = build_memory_application(now_provider=lambda: TODAY)
    stores = app.stores

    # Configure the clinic so the snapshot assembler has a provider + service to
    # read appointments and waitlist entries for.
    saved = stores.knowledge_base.save(
        ClinicKnowledgeBase(
            location="123 Main St",
            services=[ServiceConfig(name="cleaning")],
            providers=[Provider(id="prov-1", name="Dr. Ada", specialty="dentistry")],
            configured=True,
        )
    )
    assert is_ok(saved)

    # Seed one record of each type the run must read over (Req 13.2).
    appt = stores.appointments.create(
        Appointment(
            id="appt-1",
            provider_id="prov-1",
            patient_id="pat-1",
            service="cleaning",
            slot_id="slot-1",
            date=TODAY,
            time="09:00",
            status=AppointmentStatus.NO_SHOW,
        )
    )
    assert is_ok(appt)

    wl = stores.waitlist.add(
        WaitlistEntry(
            id="wl-1",
            patient_id="pat-2",
            service="cleaning",
            preferred_slot_type="any",
            added_at="2024-05-01T09:00:00+00:00",
            seq=0,
        )
    )
    assert is_ok(wl)

    session = stores.call_sessions.create(
        CallSession(id="call-1", started_at="2024-05-09T10:00:00+00:00")
    )
    assert is_ok(session)

    # Spy on the analyze_patterns the run invokes, delegating to the real one so
    # the pass still completes normally.
    seen: list[PatternInput] = []

    def spy(snapshot: PatternInput):
        seen.append(snapshot)
        return real_analyze_patterns(snapshot)

    monkeypatch.setattr(app_module, "analyze_patterns", spy)

    # Fire one analysis run (the scheduler's callback / scheduled entrypoint).
    app.run_intelligence(now=TODAY)

    # The run invoked analyze_patterns exactly once...
    assert len(seen) == 1
    snapshot = seen[0]

    # ...over a snapshot carrying the seeded appointment, waitlist, and
    # call-session data (Req 13.2).
    assert "appt-1" in {a.id for a in snapshot.appointments}
    assert "wl-1" in {e.id for e in snapshot.waitlist}
    assert "call-1" in {s.id for s in snapshot.call_sessions}
