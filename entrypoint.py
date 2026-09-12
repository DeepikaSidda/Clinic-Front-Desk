"""AgentCore Runtime container entrypoint (task 14.1, Req 1.8, 16.1).

Amazon Bedrock AgentCore Runtime looks for an ``entrypoint.py`` at the project
root. This module is that file, and it is deliberately thin: everything real
lives in :mod:`clinic_front_desk.deployment.server`, which composes the whole
system and implements the runtime's HTTP protocol contract.

What gets served (all on one ARM64 container, ``0.0.0.0:8080``)::

    GET  /ping         health probe — Healthy / HealthyBusy while a call is up
    POST /invocations  scheduled Practice_Intelligence run + role-gated
                       Dashboard BFF reads and decision approve/dismiss
    WS   /ws           Voice_Front_Desk bidirectional transport (Nova Sonic)

Both agents share **one** Data_Layer and **one** change-event channel inside this
process (Req 16.1), which is what lets a Decision approved on the dashboard and a
booking made by voice act on the same records.

Run it locally::

    # no DynamoDB needed; /ping and /invocations work offline
    CLINIC_BACKEND=memory python entrypoint.py

    # against a real DynamoDB table
    CLINIC_TABLE_NAME=clinic-front-desk AWS_REGION=us-east-1 python entrypoint.py

``CLINIC_BACKEND=memory`` swaps the *stores* only — ``/ws`` still opens a real
Nova Sonic stream, so an actual voice call needs AWS credentials and Nova Sonic
access in the configured region regardless of the store backend.

Or point an ASGI server at the module-level ``app``::

    uvicorn entrypoint:app --host 0.0.0.0 --port 8080

Configuration is entirely environment-driven (see
:func:`clinic_front_desk.deployment.server.runtime_config_from_env`) so the same
image runs against any table, region, or Nova Sonic model without a rebuild.
"""

from __future__ import annotations

from clinic_front_desk.deployment.server import build_asgi_app_from_env, main

#: The ASGI application AgentCore Runtime (or any ASGI server) serves.
#: Built at import time from the environment, so the container fails fast on a
#: misconfigured table/region rather than on the first request.
app = build_asgi_app_from_env()


if __name__ == "__main__":
    main()
