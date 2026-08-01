"""Mandate status derivation — the load-bearing logic behind the dashboard.

Pure functions over ledger events with an injected clock, so every edge is
testable: blocked-but-still-active, expired-unconsumed, consumed, multi-attempt.

The distinction these tests defend: a BLOCKED or DECLINED attempt does NOT consume
a mandate. Only an APPROVED charge does. Collapsing mandate status and attempt
outcome into one field would misrepresent how the gate works.
"""

from datetime import datetime, timedelta, timezone

import pytest

from backend.ledger.chain import append_event, new_ledger
from backend.ledger.lifecycle import (
    ACTIVE,
    BLOCKED,
    COMPLETED,
    CONSUMED,
    DECLINED,
    DRAFT,
    EXPIRED,
    NONE,
    derive_attempts,
    derive_last_outcome,
    items_summary,
    project_all,
    project_mandate,
    summary_counts,
    window_expires_at,
)

HASH = "a" * 64
CREATED = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
INSIDE = CREATED + timedelta(minutes=5)
OUTSIDE = CREATED + timedelta(minutes=16)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def mandate_dict(confirmed=True, minutes=15, mandate_hash=HASH, mandate_id="m1"):
    return {
        "mandate_id": mandate_id,
        "created_at": iso(CREATED),
        "confirmed_at": iso(CREATED) if confirmed else None,
        "status": "confirmed" if confirmed else "draft",
        "user": {"user_id": "u1", "user_email": "shiv@example.com"},
        "intent_text": "Buy house blend and filters under $30.",
        "constraints": {
            "merchant": {"name": "Beanline Coffee", "url": "https://beanline.example.com"},
            "items": [
                {"product_id": "BL-HOUSE-12", "description": "House Blend",
                 "max_unit_price": "14.00", "quantity": 1},
                {"product_id": "BL-FILTER-100", "description": "Filters",
                 "max_unit_price": "8.00", "quantity": 2},
            ],
            "price_ceiling_total": "30.00",
            "currency": "USD",
            "effective_minutes": minutes,
        },
        "mandate_hash": mandate_hash,
    }


def build(*, confirmed=True, minutes=15, stages=(), attempt_start=1,
          mandate_hash=HASH, mandate_id="m1"):
    """Build a ledger through the named stages, in causal order.

    The mandate hash is fixed up front: a chained ledger cannot be retro-edited
    without invalidating itself, which is the whole point of the chain.
    """
    ledger = new_ledger(mandate_hash)
    ts = iso(CREATED)
    append_event(ledger, "MANDATE_CREATED",
                 {"mandate": mandate_dict(confirmed, minutes, mandate_hash, mandate_id)},
                 ts=ts, mandate_id=mandate_id)
    if confirmed:
        append_event(ledger, "MANDATE_CONFIRMED", {"confirmed_at": ts}, ts=ts, mandate_id="m1")

    attempt = attempt_start
    for stage in stages:
        if stage == "proposal":
            append_event(ledger, "AGENT_PROPOSAL", {"proposal": {"proposal_id": "p1"}},
                         ts=ts, mandate_id="m1")
        elif stage == "gate_pass":
            append_event(ledger, "GATE_VERDICT", {"verdict": "PASS", "results": [],
                         "failed_rule_ids": []}, ts=ts, mandate_id="m1")
        elif stage == "gate_fail":
            append_event(ledger, "GATE_VERDICT", {"verdict": "FAIL", "results": [],
                         "failed_rule_ids": ["R3", "R5"]}, ts=ts, mandate_id="m1")
        elif stage == "blocked":
            append_event(ledger, "GATE_BLOCKED", {"failed_rule_ids": ["R3", "R5"],
                         "reason": "no payment session was created"}, ts=ts, mandate_id="m1")
        elif stage == "session":
            append_event(ledger, "SESSION_CREATED", {
                "session_id": f"ses_{attempt:02d}",
                "order_id": f"ord_{attempt:02d}",
                "external_order_ref": f"{HASH}.{attempt:02d}",
                "attempt": attempt,
            }, ts=ts, mandate_id="m1")
            attempt += 1
        elif stage == "checkout":
            append_event(ledger, "CHECKOUT_EXECUTED", {"order_number": "BL-0001"},
                         ts=ts, mandate_id="m1")
        elif stage == "approved":
            append_event(ledger, "STATUS_REPORTED", {"txn_status": "APPROVED",
                         "visa_confirmation": "SUCCESS"}, ts=ts, mandate_id="m1")
        elif stage == "declined":
            append_event(ledger, "STATUS_REPORTED", {"txn_status": "DECLINED",
                         "visa_confirmation": "SUCCESS"}, ts=ts, mandate_id="m1")
        elif stage == "consumed":
            append_event(ledger, "MANDATE_CONSUMED", {"order_number": "BL-0001"},
                         ts=ts, mandate_id="m1")
        elif stage == "expired_event":
            append_event(ledger, "MANDATE_EXPIRED", {}, ts=ts, mandate_id="m1")
        else:
            raise AssertionError(f"unknown stage {stage}")
    return ledger


def project(ledger, now):
    return project_mandate(HASH, [(ledger, "/tmp/x.ledger.json")], now)


# --- window -----------------------------------------------------------------


def test_window_expiry_is_created_at_plus_effective_minutes():
    assert window_expires_at(mandate_dict()) == CREATED + timedelta(minutes=15)


def test_window_expiry_is_none_without_a_usable_mandate():
    assert window_expires_at(None) is None
    assert window_expires_at({"created_at": iso(CREATED)}) is None


# --- status derivation ------------------------------------------------------


def test_confirmed_and_in_window_is_active():
    assert project(build(), INSIDE)["status"] == ACTIVE


def test_window_elapsed_without_consumption_is_expired():
    assert project(build(), OUTSIDE)["status"] == EXPIRED


def test_expiry_boundary_is_exclusive_matching_r6():
    ledger = build()
    expiry = CREATED + timedelta(minutes=15)

    assert project(ledger, expiry - timedelta(seconds=1))["status"] == ACTIVE
    assert project(ledger, expiry)["status"] == EXPIRED


def test_consumed_beats_an_elapsed_window():
    """A spent mandate stays CONSUMED; it does not decay into EXPIRED."""
    ledger = build(stages=("proposal", "gate_pass", "session", "checkout",
                           "approved", "consumed"))

    assert project(ledger, OUTSIDE)["status"] == CONSUMED


def test_an_explicit_expiry_event_beats_the_clock():
    assert project(build(stages=("expired_event",)), INSIDE)["status"] == EXPIRED


def test_an_unconfirmed_mandate_is_never_active():
    row = project(build(confirmed=False), INSIDE)

    assert row["status"] == DRAFT


# --- the distinction that matters -------------------------------------------


def test_a_blocked_attempt_leaves_the_mandate_active():
    """The gate refused a cart; it did not spend the user's authorization."""
    row = project(build(stages=("proposal", "gate_fail", "blocked")), INSIDE)

    assert row["status"] == ACTIVE
    assert row["last_outcome"] == BLOCKED
    assert row["attempt_count"] == 0  # no session was ever created


def test_a_declined_attempt_leaves_the_mandate_active():
    """A one-time mandate is consumed only on APPROVED (Prava's rule)."""
    row = project(
        build(stages=("proposal", "gate_pass", "session", "declined")), INSIDE
    )

    assert row["status"] == ACTIVE
    assert row["last_outcome"] == DECLINED
    assert row["attempt_count"] == 1


def test_a_completed_attempt_consumes_the_mandate():
    row = project(
        build(stages=("proposal", "gate_pass", "session", "checkout", "approved",
                      "consumed")),
        INSIDE,
    )

    assert row["status"] == CONSUMED
    assert row["last_outcome"] == COMPLETED


def test_a_fresh_mandate_has_no_outcome():
    row = project(build(), INSIDE)

    assert row["last_outcome"] == NONE
    assert row["attempt_count"] == 0


# --- multi-attempt ----------------------------------------------------------


def test_multi_attempt_records_each_external_order_ref_suffix():
    ledger = build(stages=("proposal", "gate_pass", "session", "declined",
                           "proposal", "gate_pass", "session", "checkout",
                           "approved", "consumed"))

    row = project(ledger, INSIDE)

    assert row["attempt_count"] == 2
    assert [a["attempt"] for a in row["attempts"]] == [1, 2]
    assert [a["external_order_ref"] for a in row["attempts"]] == [
        f"{HASH}.01",
        f"{HASH}.02",
    ]
    assert [a["outcome"] for a in row["attempts"]] == [DECLINED, COMPLETED]
    assert row["status"] == CONSUMED
    assert row["last_outcome"] == COMPLETED


def test_last_outcome_reflects_the_most_recent_try_not_the_first():
    ledger = build(stages=("proposal", "gate_pass", "session", "declined",
                           "proposal", "gate_fail", "blocked"))

    row = project(ledger, INSIDE)

    assert row["last_outcome"] == BLOCKED
    assert row["status"] == ACTIVE  # neither a decline nor a block spends it


def test_attempts_carry_the_order_number_of_their_own_checkout():
    ledger = build(stages=("proposal", "gate_pass", "session", "checkout", "approved"))

    attempts = derive_attempts(ledger["events"])

    assert attempts[0]["order_number"] == "BL-0001"
    assert attempts[0]["visa_confirmation"] == "SUCCESS"


def test_derive_last_outcome_on_an_empty_stream_is_none():
    assert derive_last_outcome([], []) == NONE


# --- projection shape -------------------------------------------------------


def test_projection_copies_constraints_rather_than_recomputing_them():
    row = project(build(), INSIDE)

    assert row["price_ceiling_total"] == "30.00"
    assert row["currency"] == "USD"
    assert row["merchant"]["name"] == "Beanline Coffee"
    assert row["items_summary"] == "BL-HOUSE-12 ×1, BL-FILTER-100 ×2"
    assert row["hash_prefix"] == HASH[:12]


def test_projection_reports_chain_validity_per_ledger():
    ledger = build()
    row = project(ledger, INSIDE)
    assert row["ledgers"][0]["chain_valid"]

    ledger["events"][0]["payload"]["mandate"]["intent_text"] = "tampered"
    assert not project(ledger, INSIDE)["ledgers"][0]["chain_valid"]


def test_items_summary_is_empty_without_a_mandate():
    assert items_summary(None) == ""


# --- aggregation ------------------------------------------------------------


def test_project_all_groups_by_mandate_hash():
    first = build()
    second = new_ledger("b" * 64)
    append_event(second, "MANDATE_CREATED",
                 {"mandate": dict(mandate_dict(), mandate_id="m2", mandate_hash="b" * 64)},
                 ts=iso(CREATED + timedelta(minutes=1)), mandate_id="m2")

    rows = project_all([(first, "/tmp/a.ledger.json"), (second, "/tmp/b.ledger.json")],
                       INSIDE)

    assert len(rows) == 2
    assert {row["mandate_hash"] for row in rows} == {HASH, "b" * 64}


def test_two_ledgers_for_one_mandate_collapse_into_one_row():
    """A retry writes a second chain anchored to the same mandate hash."""
    first = build(stages=("proposal", "gate_pass", "session", "declined"))
    second = build(stages=("proposal", "gate_pass", "session", "checkout",
                           "approved", "consumed"), attempt_start=2)  # same hash

    rows = project_all([(first, "/tmp/a.ledger.json"), (second, "/tmp/b.ledger.json")],
                       INSIDE)

    assert len(rows) == 1
    assert rows[0]["attempt_count"] == 2
    assert rows[0]["status"] == CONSUMED
    assert len(rows[0]["ledgers"]) == 2


def test_summary_counts_across_statuses():
    active = build(stages=("proposal", "gate_fail", "blocked"))
    consumed = build(stages=("proposal", "gate_pass", "session", "checkout",
                             "approved", "consumed"),
                     mandate_hash="c" * 64, mandate_id="m3")

    rows = project_all(
        [(active, "/tmp/a.ledger.json"), (consumed, "/tmp/c.ledger.json")], INSIDE
    )
    counts = summary_counts(rows)

    assert counts["total"] == 2
    assert counts[ACTIVE] == 1
    assert counts[CONSUMED] == 1
    assert counts["blocked_attempts"] == 1
    assert counts["chain_broken"] == 0


def test_summary_counts_flags_a_broken_chain():
    ledger = build()
    ledger["events"][0]["payload"]["mandate"]["intent_text"] = "tampered"

    counts = summary_counts(project_all([(ledger, "/tmp/a.ledger.json")], INSIDE))

    assert counts["chain_broken"] == 1
