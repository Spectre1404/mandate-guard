"""Verification gate — SPEC §3.

Every rule R1-R7 gets a passing case and at least one failing case, and the
minimum drift list from SPEC §3 is covered end to end. Rules are pure functions:
same inputs, same verdict, no clock or network reads except the `now` passed in.
"""

from datetime import timedelta

import pytest

from backend.gate.verdict import evaluate

RULE_NAMES = {
    "R1": "merchant_match",
    "R2": "item_identity",
    "R3": "unit_price_cap",
    "R4": "quantity_exact",
    "R5": "total_ceiling",
    "R6": "window_valid",
    "R7": "single_use",
}


def failing_rule_ids(verdict):
    return {r["rule_id"] for r in verdict["results"] if not r["pass"]}


# --- shape of the verdict ---------------------------------------------------


def test_verdict_reports_every_rule_with_the_documented_shape(mandate, proposal, now):
    verdict = evaluate(mandate, proposal, now=now)

    assert [r["rule_id"] for r in verdict["results"]] == list(RULE_NAMES)
    for result in verdict["results"]:
        assert set(result) == {"rule_id", "name", "pass", "expected", "actual"}
        assert result["name"] == RULE_NAMES[result["rule_id"]]
        assert isinstance(result["pass"], bool)


def test_gate_does_not_short_circuit_on_the_first_failure(mandate, proposal, mutate, now):
    """Two independent breakages must both surface -- the ledger and UI need all of them."""
    broken = mutate(proposal, lambda p: p["merchant"].update(url="https://evil.example.com"))
    broken = mutate(broken, lambda p: p["line_items"][0].update(unit_price="99.00"))

    verdict = evaluate(mandate, broken, now=now)

    assert verdict["verdict"] == "FAIL"
    assert {"R1", "R3"} <= failing_rule_ids(verdict)
    assert len(verdict["results"]) == len(RULE_NAMES)


# --- happy path -------------------------------------------------------------


def test_happy_path_passes_every_rule(mandate, proposal, now):
    verdict = evaluate(mandate, proposal, now=now)

    assert verdict["verdict"] == "PASS"
    assert failing_rule_ids(verdict) == set()


# --- R1 merchant_match ------------------------------------------------------


def test_merchant_swap_fails_r1(mandate, proposal, mutate, now):
    swapped = mutate(
        proposal,
        lambda p: p["merchant"].update(
            name="Rival Roasters", url="https://rival.example.com"
        ),
    )

    verdict = evaluate(mandate, swapped, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R1" in failing_rule_ids(verdict)


def test_r1_ignores_case_and_www_and_visa_unsafe_punctuation(mandate, proposal, mutate, now):
    """`H&M` sanitizes to `HM`; host compare is case-insensitive and www-insensitive."""
    relaxed = mutate(
        proposal,
        lambda p: p["merchant"].update(
            name="beanline coffee!", url="https://WWW.Beanline.Example.com/cart"
        ),
    )

    verdict = evaluate(mandate, relaxed, now=now)

    assert "R1" not in failing_rule_ids(verdict)


def test_r1_fails_on_lookalike_host(mandate, proposal, mutate, now):
    """A subdomain of an attacker domain must not pass as the merchant."""
    lookalike = mutate(
        proposal,
        lambda p: p["merchant"].update(url="https://beanline.example.com.evil.test"),
    )

    verdict = evaluate(mandate, lookalike, now=now)

    assert "R1" in failing_rule_ids(verdict)


# --- R2 item_identity -------------------------------------------------------


def test_substituted_product_id_fails_r2(mandate, proposal, mutate, now):
    substituted = mutate(
        proposal, lambda p: p["line_items"][0].update(product_id="BL-DECAF-12")
    )

    verdict = evaluate(mandate, substituted, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R2" in failing_rule_ids(verdict)


def test_extra_line_item_fails_r2(mandate, proposal, mutate, now):
    padded = mutate(
        proposal,
        lambda p: p["line_items"].append(
            {
                "product_id": "BL-MUG",
                "description": "Branded Mug",
                "unit_price": "9.00",
                "quantity": 1,
            }
        ),
    )

    verdict = evaluate(mandate, padded, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R2" in failing_rule_ids(verdict)


def test_dropped_line_item_fails_r2(mandate, proposal, mutate, now):
    """v1 is exact-match both ways: buying less than mandated is still not what was authorized."""
    short = mutate(proposal, lambda p: p["line_items"].pop())
    short = mutate(short, lambda p: p.update(proposed_total="12.50"))

    verdict = evaluate(mandate, short, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R2" in failing_rule_ids(verdict)


def test_duplicate_product_id_fails_r2(mandate, proposal, mutate, now):
    """The same mandated product listed twice is an extra line item, not a bigger order."""
    duped = mutate(
        proposal,
        lambda p: p["line_items"].append(dict(p["line_items"][0])),
    )
    duped = mutate(duped, lambda p: p.update(proposed_total="39.50"))

    verdict = evaluate(mandate, duped, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R2" in failing_rule_ids(verdict)


# --- R3 unit_price_cap ------------------------------------------------------


def test_price_drift_above_cap_fails_r3(mandate, proposal, mutate, now):
    drifted = mutate(proposal, lambda p: p["line_items"][0].update(unit_price="14.01"))
    drifted = mutate(drifted, lambda p: p.update(proposed_total="28.51"))

    verdict = evaluate(mandate, drifted, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R3" in failing_rule_ids(verdict)


def test_unit_price_exactly_at_cap_passes_r3(mandate, proposal, mutate, now):
    at_cap = mutate(proposal, lambda p: p["line_items"][0].update(unit_price="14.00"))
    at_cap = mutate(at_cap, lambda p: p.update(proposed_total="28.50"))

    verdict = evaluate(mandate, at_cap, now=now)

    assert "R3" not in failing_rule_ids(verdict)
    assert verdict["verdict"] == "PASS"


# --- R4 quantity_exact ------------------------------------------------------


def test_quantity_inflation_fails_r4(mandate, proposal, mutate, now):
    inflated = mutate(proposal, lambda p: p["line_items"][1].update(quantity=3))
    inflated = mutate(inflated, lambda p: p.update(proposed_total="34.25"))

    verdict = evaluate(mandate, inflated, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R4" in failing_rule_ids(verdict)


# --- R5 total_ceiling -------------------------------------------------------


def test_total_above_ceiling_fails_r5(mandate, proposal, mutate, now):
    """Each unit price is under its own cap, but the order total breaches the ceiling."""
    over = mutate(proposal, lambda p: p["line_items"][0].update(unit_price="14.00"))
    over = mutate(over, lambda p: p["line_items"][1].update(unit_price="8.00"))
    over = mutate(over, lambda p: p.update(proposed_total="30.00"))
    # 14.00 + (2 x 8.00) = 30.00, exactly at ceiling -- now nudge one cent over.
    over = mutate(over, lambda p: p["line_items"][0].update(unit_price="14.01"))
    over = mutate(over, lambda p: p.update(proposed_total="30.01"))

    verdict = evaluate(mandate, over, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R5" in failing_rule_ids(verdict)


def test_hidden_fee_mismatch_fails_r5(mandate, proposal, mutate, now):
    """Total exceeds the sum of the lines -- padded fees, the drift Prava will not catch."""
    padded = mutate(proposal, lambda p: p.update(proposed_total="29.00"))

    verdict = evaluate(mandate, padded, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R5" in failing_rule_ids(verdict)


def test_total_below_line_sum_also_fails_r5(mandate, proposal, mutate, now):
    """Consistency is bidirectional: an under-stated total is still an inconsistent order."""
    understated = mutate(proposal, lambda p: p.update(proposed_total="20.00"))

    verdict = evaluate(mandate, understated, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R5" in failing_rule_ids(verdict)


def test_total_exactly_at_ceiling_passes_r5(mandate, proposal, mutate, now):
    at_ceiling = mutate(proposal, lambda p: p["line_items"][0].update(unit_price="14.00"))
    at_ceiling = mutate(at_ceiling, lambda p: p["line_items"][1].update(unit_price="8.00"))
    at_ceiling = mutate(at_ceiling, lambda p: p.update(proposed_total="30.00"))

    verdict = evaluate(mandate, at_ceiling, now=now)

    assert verdict["verdict"] == "PASS"


# --- R6 window_valid --------------------------------------------------------


def test_expired_window_fails_r6(mandate, proposal, now):
    verdict = evaluate(mandate, proposal, now=now + timedelta(minutes=20))

    assert verdict["verdict"] == "FAIL"
    assert "R6" in failing_rule_ids(verdict)


def test_unconfirmed_mandate_fails_r6(mandate, proposal, mutate, now):
    draft = mutate(mandate, lambda m: m.update(status="draft", confirmed_at=None))

    verdict = evaluate(draft, proposal, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R6" in failing_rule_ids(verdict)


def test_window_boundary_is_exclusive(mandate, proposal, now):
    """created_at + effective_minutes is expiry, not the last valid instant."""
    expires_at = now + timedelta(minutes=14)  # created 1 minute before `now`

    assert "R6" not in failing_rule_ids(
        evaluate(mandate, proposal, now=expires_at - timedelta(seconds=1))
    )
    assert "R6" in failing_rule_ids(evaluate(mandate, proposal, now=expires_at))


# --- R7 single_use ----------------------------------------------------------


@pytest.mark.parametrize("dead_status", ["consumed", "revoked", "expired"])
def test_consumed_revoked_or_expired_mandate_fails_r7(
    mandate, proposal, mutate, now, dead_status
):
    spent = mutate(mandate, lambda m: m.update(status=dead_status))

    verdict = evaluate(spent, proposal, now=now)

    assert verdict["verdict"] == "FAIL"
    assert "R7" in failing_rule_ids(verdict)


# --- purity -----------------------------------------------------------------


def test_evaluate_does_not_mutate_its_inputs(mandate, proposal, now):
    import copy

    mandate_before = copy.deepcopy(mandate)
    proposal_before = copy.deepcopy(proposal)

    evaluate(mandate, proposal, now=now)

    assert mandate == mandate_before
    assert proposal == proposal_before


def test_evaluate_is_deterministic(mandate, proposal, now):
    assert evaluate(mandate, proposal, now=now) == evaluate(mandate, proposal, now=now)
