"""Beanline's mock card processor — SPEC §6.

Deliberately boring and deterministic: Luhn + Visa prefix + expiry + CVV shape.
It approves anything structurally valid, because the interesting failure modes in
this project live in the gate and the pre-check, not here.

Returns an `authorization_code` and a `response_code` ("00" approved, "05"
declined) which the executor forwards to Prava's report-status verbatim. Note the
2-character cap on `response_code` -- Prava rejects longer values.
"""

import re
from datetime import datetime, timezone

EXPIRY_RE = re.compile(r"^(0[1-9]|1[0-2])\s*/\s*(\d{2}|\d{4})$")
CVV_RE = re.compile(r"^\d{3,4}$")

APPROVED = "00"
DECLINED = "05"


def luhn_ok(number):
    if not number.isdigit() or not 12 <= len(number) <= 19:
        return False
    total = 0
    for index, char in enumerate(reversed(number)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def authorize(card_number, expiry, cvv, name, now=None):
    """Validate the card and return a processor decision.

    Returns {approved, response_code, authorization_code, reason}.
    """
    now = now or datetime.now(timezone.utc)
    number = re.sub(r"[\s-]", "", card_number or "")

    if not name or not name.strip():
        return _declined("cardholder name is required")
    if not number.startswith("4"):
        return _declined("only Visa cards are accepted at this merchant")
    if not luhn_ok(number):
        return _declined("card number failed Luhn check")
    if not CVV_RE.match(cvv or ""):
        return _declined("CVV must be 3 or 4 digits")

    match = EXPIRY_RE.match((expiry or "").strip())
    if not match:
        return _declined("expiry must be MM/YY or MM/YYYY")

    month = int(match.group(1))
    year = int(match.group(2))
    if year < 100:
        year += 2000
    # Expiry means end of that month.
    if (year, month) < (now.year, now.month):
        return _declined("card has expired")

    return {
        "approved": True,
        "response_code": APPROVED,
        # Derived from the card's last 4 so it is stable per run without a clock or RNG.
        "authorization_code": f"BL{number[-4:]}{month:02d}",
        "reason": None,
    }


def _declined(reason):
    return {
        "approved": False,
        "response_code": DECLINED,
        "authorization_code": None,
        "reason": reason,
    }
