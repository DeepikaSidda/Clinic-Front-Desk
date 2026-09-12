"""Prove the public URL actually works, rather than assuming it does.

Checks, in order:

    /ping            the app is reachable through CloudFront over HTTPS
    /voice           the call page renders and pulls in its client script
    /static/...      the assets the page needs are served
    /ws              the WebSocket upgrade survives CloudFront
    dashboard paths  are *not* routed (404, not 403)

The WebSocket check is the one that matters most. CloudFront only forwards the
``Upgrade``/``Connection`` headers under an origin request policy that passes all
viewer headers, and if that is wrong the page will load perfectly and then fail
the moment someone presses the microphone button.

    python deploy/verify_public.py
    python deploy/verify_public.py --wait   # poll until CloudFront is ready
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.request

HOST = "d21u7cmj563imv.cloudfront.net"
BASE = f"https://{HOST}"

#: Routes the voice-only build must refuse to serve. These carry patient names,
#: mobile numbers and blood groups, and dashboard access is decided by a
#: ``?role=`` parameter that is explicitly not a security control.
#:
#: Taken from the real route table in deployment/server.py, not guessed. An
#: earlier version of this list checked /patients, /calendar, /day and /metrics,
#: none of which are routes in *either* mode — so it reported four passes while
#: leaving /slots, /onboarding and the whole /dashboard/* tree untested.
PRIVATE = (
    "/",
    "/onboarding",
    "/slots",
    "/documents",
    "/dashboard/schedule",
    "/dashboard/activity",
    "/dashboard/metrics",
    "/dashboard/decisions",
    "/dashboard/events",
)

#: Asking as the doctor, because that is what a judge with the link would do.
#: The role is a query parameter, so there is nothing stopping them.
AS_DOCTOR = "?role=doctor"


def get(path: str, timeout: int = 15) -> tuple[int | str, bytes]:
    request = urllib.request.Request(
        BASE + path, headers={"User-Agent": "clinic-deploy-verify"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001 - report, do not raise
        return type(exc).__name__, b""


def post_json(path: str, payload: dict[str, object], timeout: int = 15) -> tuple[int | str, bytes]:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "User-Agent": "clinic-deploy-verify",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, b""


def websocket_upgrade(path: str = "/ws", timeout: int = 15) -> tuple[bool, str]:
    """Open a real WSS handshake and confirm CloudFront returns 101.

    Hand-rolled rather than pulled from a library: this has to be exactly what a
    browser sends, and it must go through CloudFront, not to the origin.
    """
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {HOST}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Origin: https://" + HOST + "\r\n"
        "\r\n"
    )
    context = ssl.create_default_context()
    try:
        with socket.create_connection((HOST, 443), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=HOST) as tls:
                tls.sendall(request.encode())
                data = tls.recv(4096).decode("utf-8", "replace")
        first = data.split("\r\n", 1)[0]
        return "101" in first, first
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def wait_for_ping(timeout: int = 900) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _ = get("/ping", timeout=10)
        if code == 200:
            return True
        print(f"    /ping = {code}, waiting ...", flush=True)
        time.sleep(20)
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wait", action="store_true", help="poll until /ping is 200")
    args = parser.parse_args()

    failures: list[str] = []

    if args.wait and not wait_for_ping():
        print("FAIL  /ping never returned 200")
        raise SystemExit(1)

    print("PUBLIC SURFACE")
    code, body = get("/ping")
    ok = code == 200
    print(f"  {'ok  ' if ok else 'FAIL'} /ping -> {code}")
    if not ok:
        failures.append("/ping")

    code, body = get("/voice")
    text = body.decode("utf-8", "replace")
    ok = code == 200 and "<html" in text.lower()
    print(f"  {'ok  ' if ok else 'FAIL'} /voice -> {code}, {len(body)} bytes")
    if not ok:
        failures.append("/voice")

    # Whatever the page asks for, ask for it too. A 404 on the client script is
    # a page that loads and then does nothing.
    assets = sorted(
        {
            fragment.split('"')[0]
            for fragment in text.split('src="/static/')[1:]
            + text.split('href="/static/')[1:]
        }
    )
    for asset in assets:
        code, body = get(f"/static/{asset}")
        ok = code == 200
        print(f"  {'ok  ' if ok else 'FAIL'} /static/{asset} -> {code}, {len(body)} bytes")
        if not ok:
            failures.append(f"/static/{asset}")
    if not assets:
        print("  note  /voice referenced no /static assets")

    print("WEBSOCKET")
    upgraded, detail = websocket_upgrade()
    print(f"  {'ok  ' if upgraded else 'FAIL'} wss://{HOST}/ws -> {detail}")
    if not upgraded:
        failures.append("/ws")

    print("DASHBOARD MUST NOT BE PUBLIC")
    for path in PRIVATE:
        # Ask as the doctor, since ?role= is not a security control and a judge
        # with the link could do exactly this.
        code, body = get(path + AS_DOCTOR)
        # 404 is the intended answer: in voice-only mode there is no handler at
        # all, so there is no role check to get past.
        ok = code in (404, 405)
        print(f"  {'ok  ' if ok else 'FAIL'} {path}{AS_DOCTOR} -> {code}")
        if not ok:
            failures.append(f"exposed {path}")

    # /invocations is POST-only and is the JSON tool surface: booking, cancelling
    # and patient lookup without going through speech at all. GET would report a
    # misleading 405 in a full build, so ask the way a caller actually would.
    code, _ = post_json("/invocations", {"prompt": "list patients"})
    ok = code in (404, 405)
    print(f"  {'ok  ' if ok else 'FAIL'} POST /invocations -> {code}")
    if not ok:
        failures.append("exposed /invocations")

    print()
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        raise SystemExit(1)
    print(f"All checks passed. Judges can call {BASE}/voice")


if __name__ == "__main__":
    main()
