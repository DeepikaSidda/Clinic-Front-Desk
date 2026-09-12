"""The Strands tool suite exposed to the agents.

Populated across tasks 6.x/10.1: ``check_availability``, ``book_appointment``,
``reschedule``, ``cancel``, ``lookup_patient``, ``answer_faq``,
``add_to_waitlist``, ``fill_gap_from_waitlist``, ``flag_for_human``, and
``analyze_patterns``. Each tool returns a discriminated ``{ ok, ... }`` result.
"""
