"""Executor pre-check E1-E3 — SPEC §3.

The second gate, run against the rendered storefront page immediately before any
card credential is typed. Pure functions over a page observation, so the scraping
layer can be swapped for Playwright without touching the verification logic.
"""

import pytest

from backend.executor.precheck import precheck


@pytest.fixture
def observation():
    """What the executor reads off the rendered checkout page."""
    return {
        "url": "https://beanline.example.com/checkout",
        "page_total": "27.00",
        "line_items": [
            {"product_id": "BL-HOUSE-12", "unit_price": "12.50", "quantity": 1},
            {"product_id": "BL-FILTER-100", "unit_price": "7.25", "quantity": 2},
        ],
    }


def failing_ids(result):
    return {r["rule_id"] for r in result["results"] if not r["pass"]}


def test_matching_page_passes_all_three(mandate, proposal, observation):
    result = precheck(observation, proposal, mandate, session_total="27.00")

    assert result["verdict"] == "PASS"
    assert failing_ids(result) == set()
    assert [r["rule_id"] for r in result["results"]] == ["E1", "E2", "E3"]


def test_e1_page_total_differs_from_session_total(mandate, proposal, observation, mutate):
    """The drift that matters most: the page charges more than Prava authorized."""
    drifted = mutate(observation, lambda o: o.update(page_total="29.00"))

    result = precheck(drifted, proposal, mandate, session_total="27.00")

    assert result["verdict"] == "FAIL"
    assert "E1" in failing_ids(result)


def test_e2_storefront_host_differs_from_mandate_merchant(
    mandate, proposal, observation, mutate
):
    redirected = mutate(observation, lambda o: o.update(url="https://evil.test/checkout"))

    result = precheck(redirected, proposal, mandate, session_total="27.00")

    assert result["verdict"] == "FAIL"
    assert "E2" in failing_ids(result)


def test_e3_page_line_items_differ_from_verified_proposal(
    mandate, proposal, observation, mutate
):
    swapped = mutate(
        observation, lambda o: o["line_items"][0].update(product_id="BL-DECAF-12")
    )

    result = precheck(swapped, proposal, mandate, session_total="27.00")

    assert result["verdict"] == "FAIL"
    assert "E3" in failing_ids(result)


def test_e3_catches_a_quantity_change_on_the_page(mandate, proposal, observation, mutate):
    inflated = mutate(observation, lambda o: o["line_items"][1].update(quantity=3))

    result = precheck(inflated, proposal, mandate, session_total="27.00")

    assert "E3" in failing_ids(result)


def test_e3_catches_an_extra_item_added_at_checkout(mandate, proposal, observation, mutate):
    upsold = mutate(
        observation,
        lambda o: o["line_items"].append(
            {"product_id": "BL-MUG", "unit_price": "9.00", "quantity": 1}
        ),
    )

    result = precheck(upsold, proposal, mandate, session_total="27.00")

    assert "E3" in failing_ids(result)


def test_precheck_reports_all_failures_not_just_the_first(
    mandate, proposal, observation, mutate
):
    broken = mutate(observation, lambda o: o.update(url="https://evil.test/checkout"))
    broken = mutate(broken, lambda o: o.update(page_total="99.00"))

    result = precheck(broken, proposal, mandate, session_total="27.00")

    assert {"E1", "E2"} <= failing_ids(result)
    assert len(result["results"]) == 3


def test_precheck_does_not_mutate_its_inputs(mandate, proposal, observation):
    import copy

    before = copy.deepcopy(observation)

    precheck(observation, proposal, mandate, session_total="27.00")

    assert observation == before
