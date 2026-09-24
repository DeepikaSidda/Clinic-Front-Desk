"""Generate the shared secret that publishes the live-call console.

Written to ``.secrets/console_token.txt`` (gitignored) rather than printed and
forgotten: it has to be pasted into a URL every time the console is opened, and
regenerating it silently locks the previous link out.

    python scripts/make_console_token.py            # create if absent
    python scripts/make_console_token.py --rotate    # replace, revoking the old link
"""

from __future__ import annotations

import argparse
import secrets
from pathlib import Path

TOKEN_FILE = Path(".secrets/console_token.txt")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rotate",
        action="store_true",
        help="replace an existing token, revoking any link already shared",
    )
    args = parser.parse_args()

    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)

    if TOKEN_FILE.exists() and not args.rotate:
        token = TOKEN_FILE.read_text(encoding="utf-8").strip()
        print(f"  existing token reused ({len(token)} chars)")
    else:
        # 32 bytes of urandom, url-safe: it travels in a query string.
        token = secrets.token_urlsafe(32)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        print(f"  new token written ({len(token)} chars)")
        if args.rotate:
            print("  the previously shared link no longer works")

    print(f"  file: {TOKEN_FILE}")
    print(f"  token: {token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
