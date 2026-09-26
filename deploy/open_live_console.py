"""Open the doctor's live console in the default browser.

The console token is read from ``.secrets/console_token.txt`` and never printed.
"""

from __future__ import annotations

import pathlib
import urllib.error
import urllib.request
import webbrowser

BASE = "https://d21u7cmj563imv.cloudfront.net"
TOKEN_FILE = pathlib.Path(__file__).resolve().parent.parent / ".secrets" / "console_token.txt"

if not TOKEN_FILE.exists():
    raise SystemExit(f"no token at {TOKEN_FILE}")

token = TOKEN_FILE.read_text(encoding="utf-8").strip()
url = f"{BASE}/live?role=doctor&k={token}"

try:
    with urllib.request.urlopen(url, timeout=25) as r:
        print(f"{r.status}  {len(r.read())} bytes  — console is up")
except urllib.error.HTTPError as exc:
    raise SystemExit(f"console returned HTTP {exc.code}; not opening") from exc

# ``cmd /c start`` splits the URL at the ``&`` before the token, so use the
# stdlib launcher, which passes the whole URL through untouched.
webbrowser.open(url)
print("opened the live console in your default browser (token not shown)")
