"""Publishing a provider's bookable calendar.

Availability is created here and nowhere else. The Strands tools can book a slot
and release one, but nothing in the patient-facing path can bring a slot into
existence — that is the difference between an agent that fills a doctor's day and
one that invents appointments to satisfy a caller.
"""

from __future__ import annotations

from .day_slots import (
    DAY_END,
    DAY_START,
    DEFAULT_SLOT_MINUTES,
    MAX_PUBLISH_DAYS,
    MAX_SLOTS_PER_DAY,
    SlotGenerationError,
    generate_day_slots,
    generate_range_slots,
    slot_id_for,
)

__all__ = [
    "DEFAULT_SLOT_MINUTES",
    "DAY_START",
    "DAY_END",
    "MAX_SLOTS_PER_DAY",
    "MAX_PUBLISH_DAYS",
    "SlotGenerationError",
    "generate_day_slots",
    "generate_range_slots",
    "slot_id_for",
]
