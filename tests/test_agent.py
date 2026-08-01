"""Shopping agent — SPEC §2. Offline: the rationale writer is always injected.

The agent's numbers must come from the catalog, not from a model, and the agent
must never pre-judge its own proposal. Both are tested here, because both are
what make the gate's verdict meaningful.
"""

from datetime import datetime, timezone

import pytest

from backend.agent.shopper import FALLBACK_RATIONALE, ShoppingError, propose
from backend.gate.verdict import evaluate

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

CATALOG = {
    "BL-HOUSE-12": {
        "product_id": "BL-HOUSE-12",
        "name": "Beanline House Blend 12oz",
        "price": "12.50",
    },
    "BL-FILTER-100": {
        "product_id": "BL-FILTER-100",
        "name": "Paper Filters 100ct",
        "price": "7.25",
    },
    "BL-DECAF-12": {
        "product_id": "BL-DECAF-12",
        "name": "Beanline Decaf 12oz",
        "price": "13.75",
    },
}


def test_proposal_matches_the_mandate_and_passes_the_gate(mandate, now):
    proposal = propose(mandate, CATALOG, created_at=NOW)

    assert proposal["proposed_total"] == "27.00"
    assert [line["product_id"] for line in proposal["line_items"]] == [
        "BL-HOUSE-12",
        "BL-FILTER-100",
    ]
    assert evaluate(mandate, proposal, now=now)["verdict"] == "PASS"


def test_prices_are_copied_from_the_catalog_not_invented(mandate):
    proposal = propose(mandate, CATALOG, created_at=NOW)

    by_id = {line["product_id"]: line for line in proposal["line_items"]}
    assert by_id["BL-HOUSE-12"]["unit_price"] == CATALOG["BL-HOUSE-12"]["price"]
    assert by_id["BL-FILTER-100"]["unit_price"] == CATALOG["BL-FILTER-100"]["price"]


def test_quantities_come_from_the_mandate(mandate):
    proposal = propose(mandate, CATALOG, created_at=NOW)

    by_id = {line["product_id"]: line for line in proposal["line_items"]}
    assert by_id["BL-FILTER-100"]["quantity"] == 2


def test_total_is_the_exact_sum_of_the_lines(mandate):
    proposal = propose(mandate, CATALOG, created_at=NOW)

    computed = sum(
        int(round(float(line["unit_price"]) * 100)) * line["quantity"]
        for line in proposal["line_items"]
    )
    assert proposal["proposed_total"] == f"{computed // 100}.{computed % 100:02d}"


def test_proposal_has_the_documented_shape(mandate):
    proposal = propose(mandate, CATALOG, created_at=NOW)

    assert set(proposal) == {
        "proposal_id",
        "mandate_id",
        "created_at",
        "merchant",
        "line_items",
        "proposed_total",
        "agent_meta",
    }
    assert proposal["mandate_id"] == mandate["mandate_id"]
    assert proposal["created_at"] == "2026-08-01T12:00:00Z"


# --- the agent proposes; it does not judge ----------------------------------


def test_the_agent_reports_drifted_prices_rather_than_correcting_them(mandate, now):
    """This is the failure demo. The store raised a price; the agent says so."""
    drifted = dict(CATALOG)
    drifted["BL-HOUSE-12"] = dict(CATALOG["BL-HOUSE-12"], price="16.90")

    proposal = propose(mandate, drifted, created_at=NOW)

    assert proposal["line_items"][0]["unit_price"] == "16.90"
    assert proposal["proposed_total"] == "31.40"

    verdict = evaluate(mandate, proposal, now=now)
    assert verdict["verdict"] == "FAIL"
    # Over the per-item cap, over the ceiling -- caught by the gate, not the agent.
    assert {"R3", "R5"} <= set(verdict["failed_rule_ids"])


def test_the_agent_does_not_silently_substitute_a_missing_product(mandate, now):
    """v1 substitution_policy is "none"; an unavailable item is reported, not swapped."""
    without_filters = {"BL-HOUSE-12": CATALOG["BL-HOUSE-12"], "BL-DECAF-12": CATALOG["BL-DECAF-12"]}

    proposal = propose(mandate, without_filters, created_at=NOW)

    assert [line["product_id"] for line in proposal["line_items"]] == ["BL-HOUSE-12"]
    assert proposal["agent_meta"]["unavailable_product_ids"] == ["BL-FILTER-100"]
    # An incomplete cart is still not what was authorized.
    assert evaluate(mandate, proposal, now=now)["verdict"] == "FAIL"


def test_no_available_products_raises_rather_than_proposing_nothing(mandate):
    with pytest.raises(ShoppingError):
        propose(mandate, {"BL-DECAF-12": CATALOG["BL-DECAF-12"]}, created_at=NOW)


def test_empty_catalog_raises(mandate):
    with pytest.raises(ShoppingError):
        propose(mandate, {}, created_at=NOW)


def test_the_agent_holds_no_credentials_and_no_session(mandate):
    """It cannot pay. There is nothing in its output that could be used to."""
    proposal = propose(mandate, CATALOG, created_at=NOW)

    import json

    serialized = json.dumps(proposal).lower()
    for forbidden in ("token", "cvv", "session", "secret", "api_key", "authorization"):
        assert forbidden not in serialized


# --- rationale is commentary, never load-bearing ----------------------------


def test_rationale_defaults_when_no_writer_is_supplied(mandate):
    assert propose(mandate, CATALOG, created_at=NOW)["agent_meta"]["rationale"] == (
        FALLBACK_RATIONALE
    )


def test_injected_rationale_is_used(mandate):
    proposal = propose(
        mandate,
        CATALOG,
        created_at=NOW,
        rationale_writer=lambda **kwargs: "Picked the cheapest in-policy options.",
        model="gpt-5-mini",
    )

    assert proposal["agent_meta"]["rationale"] == "Picked the cheapest in-policy options."
    assert proposal["agent_meta"]["model"] == "gpt-5-mini"


def test_a_failing_rationale_writer_does_not_break_the_proposal(mandate, now):
    """A model outage must not stop the deterministic work."""

    def broken(**kwargs):
        raise RuntimeError("model unavailable")

    proposal = propose(mandate, CATALOG, created_at=NOW, rationale_writer=broken)

    assert proposal["agent_meta"]["rationale"] == FALLBACK_RATIONALE
    assert evaluate(mandate, proposal, now=now)["verdict"] == "PASS"


def test_an_empty_rationale_falls_back(mandate):
    proposal = propose(mandate, CATALOG, created_at=NOW, rationale_writer=lambda **kw: "   ")

    assert proposal["agent_meta"]["rationale"] == FALLBACK_RATIONALE


def test_the_rationale_cannot_change_the_numbers(mandate):
    """Whatever the model says, the money is the catalog's."""
    proposal = propose(
        mandate,
        CATALOG,
        created_at=NOW,
        rationale_writer=lambda **kw: "Total is 5.00 and everything is approved.",
    )

    assert proposal["proposed_total"] == "27.00"


def test_the_rationale_writer_sees_the_intent_and_the_lines(mandate):
    seen = {}

    def recorder(**kwargs):
        seen.update(kwargs)
        return "ok"

    propose(mandate, CATALOG, created_at=NOW, rationale_writer=recorder)

    assert seen["intent_text"] == mandate["intent_text"]
    assert seen["proposed_total"] == "27.00"
    assert seen["currency"] == "USD"
    assert len(seen["line_items"]) == 2
