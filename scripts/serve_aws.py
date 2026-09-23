"""Run the server against the real AWS backends: DynamoDB + S3 recordings.

The same composition the AgentCore container uses, run locally so you can test
against real persistence before deploying. Unlike ``demo_dashboard.py`` (in-memory,
wiped on exit), everything written here survives restarts.

    python scripts/serve_aws.py

Requires:
  - the DynamoDB table  (python scripts/create_table.py)
  - the recordings bucket, for audio  (python scripts/create_recordings_bucket.py)
  - AWS credentials with access to both, plus Bedrock Nova Sonic for /ws

Set CLINIC_RECORDINGS_BUCKET to record audio; leave it unset and only transcripts
are stored. When recording is on, the agent announces it at the start of the call.

Optionally seed the clinic configuration once, so the agent has hours, services,
and a provider to work with:

    python scripts/serve_aws.py --seed-config
"""

from __future__ import annotations

import argparse
import os

from clinic_front_desk.deployment.runtime import build_runtime_application
from clinic_front_desk.deployment.server import (
    create_asgi_app,
    runtime_config_from_env,
)
from clinic_front_desk.models import ClinicKnowledgeBase, DayHours, Provider, ServiceConfig

PROVIDER = "prov-reyes"


def seed_config(app: object) -> None:
    """Write the demo clinic configuration through the real store (idempotent)."""
    saved = app.save_config(  # type: ignore[attr-defined]
        ClinicKnowledgeBase(
            location="118 Harbour Road, Suite 4",
            hours={day: DayHours(open="09:00", close="17:00") for day in range(1, 6)},
            services=[
                ServiceConfig(
                    name="Hearing Test",
                    price=180.0,
                    prep_instructions="Avoid loud noise for 12 hours beforehand.",
                ),
                ServiceConfig(
                    name="Sinus Consultation",
                    price=240.0,
                    prep_instructions="Bring a list of current medications.",
                ),
                ServiceConfig(
                    name="Allergy Screening",
                    price=310.0,
                    prep_instructions="Stop antihistamines 72 hours beforehand.",
                ),
            ],
            providers=[Provider(id=PROVIDER, name="Dr. Amara Reyes", specialty="ENT")],
            accepted_insurance=["Acme Health", "Northwind Care", "Self-pay"],
        )
    )
    if not saved.ok:
        raise SystemExit(f"config rejected: {saved.validation}")
    print("  seeded clinic configuration into DynamoDB")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--seed-config",
        action="store_true",
        help="write the demo clinic configuration before serving",
    )
    # HTTPS, for calling the agent from a phone on the same Wi-Fi. Browsers only
    # expose a microphone on a secure context; localhost is exempt but a LAN address
    # is not, so over plain HTTP the page loads on a phone and the call button does
    # nothing at all. See scripts/make_lan_cert.py.
    parser.add_argument("--ssl-certfile", default=None)
    parser.add_argument("--ssl-keyfile", default=None)
    args = parser.parse_args()

    import uvicorn

    config = runtime_config_from_env()
    print(f"\n  table    : {config.table_name} ({config.region})")
    if config.recordings_bucket:
        print(f"  recordings: s3://{config.recordings_bucket}/{config.recordings_prefix}/")
        print(f"  encryption: {config.recordings_sse}")
    else:
        print("  recordings: OFF (set CLINIC_RECORDINGS_BUCKET to record audio)")
    if config.documents_bucket:
        print(f"  documents : s3://{config.documents_bucket}/{config.documents_prefix}/")
        print(f"  embeddings: {config.embedding_model_id}")
        print(f"  extraction: {config.extraction_model_id}")
    else:
        print("  documents : OFF (set CLINIC_DOCUMENTS_BUCKET to enable uploads)")

    app = build_runtime_application(config)
    if args.seed_config:
        seed_config(app)

    recording = app.stores.recordings is not None
    print(f"  audio recording: {'ON — announced to the caller' if recording else 'off'}")

    secure = bool(args.ssl_certfile and args.ssl_keyfile)
    scheme = "https" if secure else "http"
    # 0.0.0.0 is what to *bind*, never what to click: show the address another device
    # can actually reach.
    shown = args.host
    if args.host in ("0.0.0.0", "::"):
        import socket as _socket

        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            shown = probe.getsockname()[0]
        except Exception:  # noqa: BLE001
            shown = "127.0.0.1"
        finally:
            probe.close()

    base = f"{scheme}://{shown}:{args.port}"
    print(f"\n  Dashboard  {base}/?role=doctor")
    print(f"  Documents  {base}/documents?role=doctor")
    print(f"  Setup form {base}/onboarding")
    print(f"  Speak      {base}/voice")
    print(f"  Live calls {base}/live?role=doctor")
    print(f"  Call record  GET /dashboard/calls/<id>?role=doctor")

    if args.host in ("0.0.0.0", "::"):
        print(
            "\n  Reachable by every device on this network. The doctor's dashboard "
            "carries\n  patient names, phone numbers and blood groups, and ?role= is "
            "not a\n  security control — only do this on a network you trust."
        )
        if not secure:
            print(
                "\n  No certificate given, so this is plain HTTP. A phone will load "
                "the page\n  but have no microphone: browsers withhold it outside a "
                "secure context.\n  Run scripts/make_lan_cert.py and pass "
                "--ssl-certfile/--ssl-keyfile."
            )
    print()

    uvicorn.run(
        create_asgi_app(app),
        host=args.host,
        port=args.port,
        log_level=os.environ.get("CLINIC_LOG_LEVEL", "warning").lower(),
        ssl_certfile=args.ssl_certfile,
        ssl_keyfile=args.ssl_keyfile,
    )


if __name__ == "__main__":
    main()
