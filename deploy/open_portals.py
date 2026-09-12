"""Open every portal page in a browser, after checking each one actually renders.

The deployed URL is voice-only on purpose — the dashboard carries patient names,
mobile numbers and blood groups, and it is gated by a ``?role=`` query parameter
that is explicitly not a security control. So the portals are local only, against
the same live DynamoDB table the agent writes to.

Each page is fetched first and only opened if it returns 200 with real HTML, so a
broken page shows up here as a failure line instead of as a blank browser tab.

    python deploy/open_portals.py            # check, then open in the browser
    python deploy/open_portals.py --check    # check only, do not open tabs
"""

from __future__ import annotations

import argparse
import time
import urllib.error
import urllib.request
import webbrowser

BASE = "http://127.0.0.1:8080"

#: (label, path). Roles come straight from dashboard/role_gate.py: the doctor sees
#: every view, the assistant is deliberately denied documents and the decisions
#: feed, so both are worth looking at.
PORTALS: tuple[tuple[str, str], ...] = (
    ("Doctor dashboard", "/?role=doctor"),
    ("Assistant view", "/?role=assistant"),
    ("Day calendar", "/slots?role=doctor"),
    ("Documents", "/documents?role=doctor"),
    ("Clinic setup", "/onboarding?role=doctor"),
    ("Voice client", "/voice"),
)


def probe(path: str, timeout: int = 180) -> tuple[int | str, int, str, float]:
    """Fetch a page, and time it.

    The timeout is generous on purpose: the doctor's view aggregates every
    component, and how long that takes against the live table is exactly the
    thing worth measuring rather than hiding behind a failed request.
    """
    request = urllib.request.Request(
        BASE + path, headers={"User-Agent": "clinic-open-portals"}
    )
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
            elapsed = time.perf_counter() - started
            return response.status, len(body), body.decode("utf-8", "replace"), elapsed
    except urllib.error.HTTPError as exc:
        return exc.code, 0, "", time.perf_counter() - started
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__, 0, "", time.perf_counter() - started


#: The doctor's page is assembled from these, so timing them individually says
#: which component is responsible when the whole page is slow.
PARTIALS: tuple[tuple[str, str], ...] = (
    ("schedule", "/dashboard/schedule?role=doctor"),
    ("activity", "/dashboard/activity?role=doctor"),
    ("metrics", "/dashboard/metrics?role=doctor"),
    ("decisions", "/dashboard/decisions?role=doctor"),
)


def diagnose() -> None:
    print("  component timings (the doctor's page is the sum of these):")
    for label, path in PARTIALS:
        code, length, _, elapsed = probe(path)
        print(f"    {label:<10} {str(code):>12}  {length:>7} bytes  {elapsed:6.2f}s")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="check the pages but do not open them"
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="also time each dashboard component separately",
    )
    args = parser.parse_args()

    print(f"  server {BASE}")
    code, _, _, _ = probe("/ping", timeout=10)
    if code != 200:
        print(f"  FAIL  /ping -> {code}")
        print("  Start it first:")
        print('    $env:CLINIC_TABLE_NAME="clinic-front-desk"; '
              '$env:AWS_REGION="us-east-1"; '
              '.\\.venv\\Scripts\\python.exe scripts\\serve_aws.py '
              "--host 127.0.0.1 --port 8080")
        raise SystemExit(1)
    print("  ok    /ping -> 200")
    print()

    failures: list[str] = []
    healthy: list[tuple[str, str]] = []
    for label, path in PORTALS:
        code, length, text, elapsed = probe(path)
        renders = code == 200 and "<html" in text.lower()
        # An "Access denied" panel is a correct answer for the assistant on some
        # views, not a broken page, so report it rather than calling it a failure.
        denied = 'data-role="access-denied"' in text
        note = "  (access denied panel)" if denied else ""
        # Anything a doctor waits this long for is a finding, not a detail.
        if renders and elapsed > 5:
            note += "  <-- slow"
        print(
            f"  {'ok  ' if renders else 'FAIL'} {label:<18} {str(code):>12}  "
            f"{length:>7} bytes  {elapsed:6.2f}s{note}"
        )
        if renders:
            healthy.append((label, path))
        else:
            failures.append(label)

    print()
    if args.diagnose:
        diagnose()
        print()
    if args.check:
        print(f"  {len(healthy)}/{len(PORTALS)} portals render.")
    else:
        for label, path in healthy:
            webbrowser.open_new_tab(BASE + path)
        print(f"  opened {len(healthy)} tabs.")

    if failures:
        print(f"  did not render: {', '.join(failures)}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
