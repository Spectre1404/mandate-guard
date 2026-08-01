"""Executor pre-check E1-E3 — SPEC §3, the second gate.

Runs against the rendered storefront checkout page in the instant before any card
credential is typed. The gate verified intent against the mandate; this verifies
that the page in front of us is still the thing that was verified.

Pure functions over a page observation dict, so the Playwright scraping layer can
change without touching the verification logic:

    {"url": ..., "page_total": "27.00",
     "line_items": [{"product_id": ..., "unit_price": ..., "quantity": ...}]}

Any failure aborts before card entry. Nothing is reported to Prava if no token was
used; if a token was already entered, the caller reports DECLINED per Prava's docs.
"""

from backend.money import parse as parse_money
from backend.normalize import comparison_host


def _result(rule_id, name, passed, expected, actual):
    return {
        "rule_id": rule_id,
        "name": name,
        "pass": bool(passed),
        "expected": expected,
        "actual": actual,
    }


def _fingerprint(line_items):
    """Order-insensitive comparable form of a cart."""
    return sorted(
        (item["product_id"], item["unit_price"], item["quantity"]) for item in line_items
    )


def e1_page_total_matches_session(observation, session_total):
    """The page must charge exactly what Prava authorized."""
    page_total = observation["page_total"]
    return _result(
        "E1",
        "page_total_matches_session",
        parse_money(page_total) == parse_money(session_total),
        session_total,
        page_total,
    )


def e2_storefront_host_matches_mandate(observation, mandate):
    """We must still be on the merchant the user named."""
    expected = comparison_host(mandate["constraints"]["merchant"]["url"])
    actual = comparison_host(observation["url"])
    return _result("E2", "storefront_host_matches_mandate", expected == actual, expected, actual)


def e3_page_items_match_proposal(observation, proposal):
    """The cart on screen must be the cart the gate verified."""
    expected = _fingerprint(proposal["line_items"])
    actual = _fingerprint(observation["line_items"])
    return _result("E3", "page_items_match_proposal", expected == actual, expected, actual)


def precheck(observation, proposal, mandate, session_total):
    """Run E1-E3. Reports all three; never short-circuits."""
    results = [
        e1_page_total_matches_session(observation, session_total),
        e2_storefront_host_matches_mandate(observation, mandate),
        e3_page_items_match_proposal(observation, proposal),
    ]
    passed = all(result["pass"] for result in results)

    return {
        "verdict": "PASS" if passed else "FAIL",
        "results": results,
        "failed_rule_ids": [r["rule_id"] for r in results if not r["pass"]],
    }
