"""Verification gate rules R1-R7 — SPEC §3.

Pure functions. Each takes the mandate, the proposal, and an explicit `now`, and
returns `{rule_id, name, pass, expected, actual}`. No clock reads, no network, no
mutation of the inputs, no LLM anywhere near them. Same inputs, same verdict,
forever -- which is what makes a FAIL defensible when someone contests it.

Where a rule cannot verify something (an unmatched product has no cap to check
against), it FAILS rather than skipping. Unverifiable is not the same as fine.
"""

from backend.compiler.mandate import expires_at, parse_iso
from backend.money import parse as parse_money, to_string, total_of
from backend.normalize import comparison_host, visa_safe_name

DEAD_STATUSES = {"consumed", "expired", "revoked"}


def _result(rule_id, name, passed, expected, actual):
    return {
        "rule_id": rule_id,
        "name": name,
        "pass": bool(passed),
        "expected": expected,
        "actual": actual,
    }


def r1_merchant_match(mandate, proposal, now=None):
    """Same merchant host, and same name once reduced to the Visa-safe character set."""
    mandate_merchant = mandate["constraints"]["merchant"]
    expected_host = comparison_host(mandate_merchant["url"])
    actual_host = comparison_host(proposal["merchant"]["url"])
    expected_name = visa_safe_name(mandate_merchant["name"])
    actual_name = visa_safe_name(proposal["merchant"]["name"])

    return _result(
        "R1",
        "merchant_match",
        expected_host == actual_host and expected_name == actual_name,
        f"host={expected_host} name={expected_name}",
        f"host={actual_host} name={actual_name}",
    )


def r2_item_identity(mandate, proposal, now=None):
    """Exact multiset match both ways: no substitutions, no extras, no omissions."""
    expected = sorted(item["product_id"] for item in mandate["constraints"]["items"])
    actual = sorted(item["product_id"] for item in proposal["line_items"])

    return _result("R2", "item_identity", expected == actual, expected, actual)


def r3_unit_price_cap(mandate, proposal, now=None):
    """Every line's unit price is at or under the cap the user approved for that product."""
    caps = {
        item["product_id"]: item["max_unit_price"]
        for item in mandate["constraints"]["items"]
    }
    expected, actual, passed = [], [], True

    for line in proposal["line_items"]:
        product_id = line["product_id"]
        cap = caps.get(product_id)
        actual.append(f"{product_id}={line['unit_price']}")
        if cap is None:
            expected.append(f"{product_id}=<not in mandate>")
            passed = False  # no cap to verify against -- unverifiable, so fail
            continue
        expected.append(f"{product_id}<={cap}")
        if parse_money(line["unit_price"]) > parse_money(cap):
            passed = False

    return _result("R3", "unit_price_cap", passed, expected, actual)


def r4_quantity_exact(mandate, proposal, now=None):
    """Quantities match the mandate exactly -- v1 has no partial or bulk allowance."""
    wanted = {
        item["product_id"]: item["quantity"] for item in mandate["constraints"]["items"]
    }
    got = {}
    passed = True

    for line in proposal["line_items"]:
        product_id = line["product_id"]
        # A duplicated product_id would collapse here; R2 has already failed it.
        got[product_id] = got.get(product_id, 0) + line["quantity"]

    for product_id in sorted(set(wanted) | set(got)):
        if wanted.get(product_id) != got.get(product_id):
            passed = False

    return _result(
        "R4",
        "quantity_exact",
        passed,
        {k: wanted[k] for k in sorted(wanted)},
        {k: got[k] for k in sorted(got)},
    )


def r5_total_ceiling(mandate, proposal, now=None):
    """Total is under the ceiling AND equals the sum of the lines.

    The second half is the one Prava does not do for us: `create-session` allows
    `total_amount` to exceed the line sum for tax and fees. Padded fees are a
    primary drift vector, so internal consistency is checked here.
    """
    ceiling = mandate["constraints"]["price_ceiling_total"]
    proposed = proposal["proposed_total"]
    line_sum = to_string(total_of(proposal["line_items"]))

    under_ceiling = parse_money(proposed) <= parse_money(ceiling)
    consistent = parse_money(proposed) == parse_money(line_sum)

    return _result(
        "R5",
        "total_ceiling",
        under_ceiling and consistent,
        f"total<={ceiling} and total==sum_of_lines({line_sum})",
        f"total={proposed} sum_of_lines={line_sum}",
    )


def r6_window_valid(mandate, proposal, now):
    """Confirmed, and still inside its effective window. Expiry is exclusive."""
    expiry = expires_at(mandate)
    confirmed = mandate["status"] == "confirmed"
    in_window = now < expiry

    return _result(
        "R6",
        "window_valid",
        confirmed and in_window,
        f"status=confirmed and now<{expiry.isoformat()}",
        f"status={mandate['status']} now={now.isoformat()}",
    )


def r7_single_use(mandate, proposal, now=None):
    """A mandate is spent once. Revoked and expired are equally terminal."""
    status = mandate["status"]

    return _result(
        "R7",
        "single_use",
        status not in DEAD_STATUSES,
        f"status not in {sorted(DEAD_STATUSES)}",
        f"status={status}",
    )


# Order is the order the UI and the ledger render them in.
RULES = (
    r1_merchant_match,
    r2_item_identity,
    r3_unit_price_cap,
    r4_quantity_exact,
    r5_total_ceiling,
    r6_window_valid,
    r7_single_use,
)
