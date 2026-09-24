"""How money is said out loud.

Split into its own module because it is spoken, not printed, and because getting it
wrong is not a formatting nit. Prices were rendered as ``$500.00``. The clinic is in
Tirupati and charges in rupees, so a caller asking the consultation fee would have
been quoted a number roughly eighty times the real one, in a confident voice, on a
recorded line. That is the same class of failure as inventing availability: a
commitment stated as the clinic's word.

Two decisions here, both about the fact that this is heard rather than read:

* **"500 rupees", not "₹500.00".** A speech model given ``₹`` may say "rupee sign",
  skip it, or read the symbol's name. Writing the word removes the guess.
* **No trailing ``.00``.** "Five hundred rupees" is what a receptionist says; "five
  hundred point zero zero rupees" is what a computer says.
"""

from __future__ import annotations

from .entities import Money

#: The currency the clinic charges in.
#:
#: A constant rather than a literal so there is exactly one place to change if this
#: is ever deployed for a clinic billing in something else — and so that grepping for
#: the currency finds a definition instead of a scattering of symbols.
CURRENCY_WORD = "rupees"


def format_money(amount: Money) -> str:
    """Render ``amount`` the way it should be spoken.

    Whole amounts lose their decimals, because a fee is almost always whole and
    "five hundred point zero zero" is not how a price is said. Fractional amounts
    keep two places, since at that point the paise matter.

    >>> format_money(500.0)
    '500 rupees'
    >>> format_money(499.5)
    '499.50 rupees'
    """
    if float(amount).is_integer():
        return f"{int(amount)} {CURRENCY_WORD}"
    return f"{amount:.2f} {CURRENCY_WORD}"


__all__ = ["CURRENCY_WORD", "format_money"]
