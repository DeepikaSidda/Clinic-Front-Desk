"""The timer that makes "we'll remind you the day before" true.

Reminders began as a button, because nothing in this deployment ran on a clock and a
promise about tomorrow needed something that wakes up. This is that something: an
EventBridge Schedule fires once each evening, invokes this function, and it texts
every patient booked for the next clinic day.

It deliberately shares the loop with the doctor's button rather than reimplementing it
— both call :func:`~clinic_front_desk.notifications.reminders.remind_booked_on_day`.
The ``reminded_at`` stamp is what lets both exist safely: whichever runs first marks the
appointment, and the other skips it. Before there was a schedule that stamp only
protected against a doctor pressing twice; now it is what stops a patient being texted
by a human and a timer on the same evening.

**Why a Lambda and not a cron on the web host.** The reminder needs the table and SNS,
and nothing else. Putting it on the instance would tie a promise to patients to that
instance staying up, and the instance exists to answer calls. A function that AWS wakes,
runs for a second and discards cannot be left half-running, and when it fails it fails
somewhere a CloudWatch alarm can see.

Configuration, all through the environment so one artefact runs against any clinic:

======================================  ===================================
``CLINIC_TABLE_NAME``                   The table. Required.
``AWS_REGION``                          Supplied by Lambda.
``CLINIC_SMS_ENABLED``                  ``1`` to actually send. Unset = dry run.
``CLINIC_REMINDER_LEAD_DAYS``           How far ahead to look. Default ``1``.
``CLINIC_UTC_OFFSET_MINUTES``           Clinic clock vs UTC. Default ``330`` (IST).
======================================  ===================================

The event may override the day for a manual run::

    {"day": "2026-12-15"}              one specific date
    {"lead_days": 0}                   today rather than tomorrow
    {"dry_run": true}                  resolve and count, send nothing
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from clinic_front_desk.notifications.reminders import ReminderRun, remind_booked_on_day
from clinic_front_desk.notifications.sms import NullSmsSender, SmsSender, SnsSmsSender

logger = logging.getLogger()
logger.setLevel(logging.INFO)

#: India does not observe daylight saving, so a fixed offset is exact for this clinic
#: and costs nothing. It also avoids depending on a timezone database being present in
#: the runtime image, which is the kind of thing that works until it doesn't.
DEFAULT_UTC_OFFSET_MINUTES = 330


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        logger.warning("%s is not a number; using %s", name, default)
        return default


def clinic_day(
    *, lead_days: int, offset_minutes: int, now: datetime | None = None
) -> str:
    """The date to remind for, as the clinic's own calendar would name it.

    A schedule that fires at 18:00 in Tirupati is 12:30 UTC, and on most evenings both
    clocks agree about what "tomorrow" is. They stop agreeing after 18:30 local, and a
    reminder run that silently targets the wrong date on some evenings would be worse
    than one that never ran. So the date is always computed in clinic time.
    """
    moment = now or datetime.now(timezone.utc)
    local = moment.astimezone(timezone(timedelta(minutes=offset_minutes)))
    return (local + timedelta(days=lead_days)).date().isoformat()


def run_reminders(
    *,
    stores: Any,
    sms_sender: SmsSender,
    day: str,
    now: Any = None,
) -> list[ReminderRun]:
    """Remind ``day`` for every provider the clinic has configured.

    One run per provider, and a provider whose day cannot be read does not stop the
    others: a solo practice has one provider today, but a clinic that grows should not
    discover that one bad read silenced the whole evening.
    """
    contact = ""
    providers: list[str] = []
    config = stores.clinic_knowledge_base.get()
    value = getattr(config, "value", None)
    if value is not None:
        contact = getattr(value, "contact_phone", "") or ""
        providers = [provider.id for provider in getattr(value, "providers", [])]

    if not providers:
        logger.warning("no providers are configured; nothing to remind")
        return []

    runs: list[ReminderRun] = []
    for provider_id in providers:
        run = remind_booked_on_day(
            day=day,
            provider_id=provider_id,
            appointments=stores.appointments,
            patients=stores.patients,
            sms_sender=sms_sender,
            clinic_phone=contact,
            **({"now": now} if now is not None else {}),
        )
        logger.info("%s", run.summary())
        runs.append(run)
    return runs


def handler(event: Any = None, context: Any = None) -> dict[str, Any]:
    """Lambda entry point. Returns a summary rather than raising on a quiet night."""
    import boto3  # type: ignore[import-untyped]

    from clinic_front_desk.data_layer.dynamodb import create_stores

    payload: dict[str, Any] = event if isinstance(event, dict) else {}

    table_name = os.environ.get("CLINIC_TABLE_NAME", "").strip()
    if not table_name:
        # Refuse rather than guess. A reminder run against the wrong table would text
        # the wrong patients, which is worse than not running.
        raise RuntimeError("CLINIC_TABLE_NAME is not set")

    region = (
        os.environ.get("AWS_REGION")
        or os.environ.get("CLINIC_REGION")
        or "us-east-1"
    )
    offset = _int_env("CLINIC_UTC_OFFSET_MINUTES", DEFAULT_UTC_OFFSET_MINUTES)
    lead_days = int(payload.get("lead_days", _int_env("CLINIC_REMINDER_LEAD_DAYS", 1)))
    day = str(payload.get("day") or "").strip() or clinic_day(
        lead_days=lead_days, offset_minutes=offset
    )

    dry_run = bool(payload.get("dry_run")) or not _flag("CLINIC_SMS_ENABLED")
    sender: SmsSender = (
        NullSmsSender() if dry_run else SnsSmsSender(region=region)
    )

    table = boto3.resource("dynamodb", region_name=region).Table(table_name)
    stores = create_stores(table)

    runs = run_reminders(stores=stores, sms_sender=sender, day=day)

    result = {
        "day": day,
        "dry_run": dry_run,
        "providers": len(runs),
        "booked": sum(run.booked for run in runs),
        "sent": sum(run.sent for run in runs),
        "failed": sum(run.failed for run in runs),
        "skipped": sum(run.skipped for run in runs),
        "errors": [run.read_error for run in runs if run.read_error],
        "summaries": [run.summary() for run in runs],
    }
    logger.info("reminder run: %s", json.dumps(result))
    return result


__all__ = ["clinic_day", "handler", "run_reminders"]
