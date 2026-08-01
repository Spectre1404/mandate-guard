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
from backend.origins import declared_origin


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


def e2_storefront_host_matches_declared_origin(observation, mandate, origin_map=None):
    """We must still be on the merchant the user named.

    Compared against the *declared origin* for that merchant when one exists (see
    backend/origins.py), otherwise against the mandate's canonical URL. Either
    way a page on some other host fails: the mapping redirects the comparison, it
    does not relax it.
    """
    merchant = mandate["constraints"]["merchant"]
    canonical_url = merchant["url"]
    declared = declared_origin(merchant["name"], origin_map)

    expected = comparison_host(declared or canonical_url)
    actual = comparison_host(observation["url"])

    return _result(
        "E2",
        "storefront_host_matches_declared_origin",
        expected == actual,
        expected,
        actual,
    )


def origin_disclosure(observation, mandate, origin_map=None):
    """All three values, for the ledger -- the mapping is disclosed in-evidence."""
    merchant = mandate["constraints"]["merchant"]
    declared = declared_origin(merchant["name"], origin_map)
    return {
        "canonical_merchant_url": merchant["url"],
        "declared_origin": declared,
        "observed_host": comparison_host(observation["url"]),
    }


def e3_page_items_match_proposal(observation, proposal):
    """The cart on screen must be the cart the gate verified."""
    expected = _fingerprint(proposal["line_items"])
    actual = _fingerprint(observation["line_items"])
    return _result("E3", "page_items_match_proposal", expected == actual, expected, actual)


def precheck(observation, proposal, mandate, session_total, origin_map=None):
    """Run E1-E3. Reports all three; never short-circuits."""
    results = [
        e1_page_total_matches_session(observation, session_total),
        e2_storefront_host_matches_declared_origin(observation, mandate, origin_map),
        e3_page_items_match_proposal(observation, proposal),
    ]
    passed = all(result["pass"] for result in results)

    return {
        "verdict": "PASS" if passed else "FAIL",
        "results": results,
        "failed_rule_ids": [r["rule_id"] for r in results if not r["pass"]],
        "origin_disclosure": origin_disclosure(observation, mandate, origin_map),
    }
