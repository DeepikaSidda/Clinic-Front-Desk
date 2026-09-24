"""Throwaway: finish updating money assertions to the spoken rupee format."""

from __future__ import annotations

import re
from pathlib import Path

# "150.00" -> "150 rupees" style assertions, and the format_money import the
# property test now needs.
SIMPLE: list[tuple[str, str, str]] = [
    ("tests/unit/test_faq_tool.py", 'assert "150.00" in result.value', 'assert "150 rupees" in result.value'),
]

for path_text, old, new in SIMPLE:
    path = Path(path_text)
    text = path.read_text(encoding="utf-8")
    if old in text:
        path.write_text(text.replace(old, new), encoding="utf-8")
        print(f"  ok   {path_text}: {old!r} -> {new!r}")
    else:
        print(f"  MISS {path_text}: {old!r}")

# Report any remaining decimal-money assertions so nothing is missed silently.
print("\n  remaining money-shaped assertions in tests:")
for path in sorted(Path("tests").rglob("*.py")):
    text = path.read_text(encoding="utf-8", errors="replace")
    for number, line in enumerate(text.splitlines(), start=1):
        if re.search(r'assert.*"\d+\.\d\d"', line) or "$" in line and "assert" in line:
            print(f"    {path}:{number}  {line.strip()}")

# Does the property test import format_money?
prop = Path("tests/property/test_tool_properties.py")
text = prop.read_text(encoding="utf-8")
print(f"\n  property test imports format_money: {'format_money' in text.split('def ')[0]}")
print(f"  property test uses format_money:    {'format_money(' in text}")
