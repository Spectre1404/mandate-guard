"""Evidence ledger — SPEC §4.

Append-only, hash-chained. Genesis `prev_hash` is the mandate_hash, so the chain
is anchored to the thing the user actually confirmed. `event_hash` covers the
canonical event minus the hash field, concatenated with `prev_hash`.

The verify-chain function is a demo moment and a dispute artifact, so it has to
catch every way a chain can be wrong: edited payload, edited hash, reordering,
deletion, insertion, and a wrong anchor.
"""

import pytest

from backend.ledger.chain import (
    EVENT_TYPES,
    LedgerError,
    append_event,
    compute_event_hash,
    new_ledger,
    verify_chain,
)

MANDATE_HASH = "a" * 64


@pytest.fixture
def ledger():
    return new_ledger(MANDATE_HASH)


@pytest.fixture
def chain(ledger):
    append_event(ledger, "MANDATE_CREATED", {"mandate_id": "m1"}, ts="2026-08-01T12:00:00Z")
    append_event(ledger, "MANDATE_CONFIRMED", {"at": "12:01"}, ts="2026-08-01T12:01:00Z")
    append_event(ledger, "AGENT_PROPOSAL", {"proposal_id": "p1"}, ts="2026-08-01T12:02:00Z")
    return ledger


# --- structure --------------------------------------------------------------


def test_genesis_prev_hash_is_the_mandate_hash(ledger):
    event = append_event(ledger, "MANDATE_CREATED", {}, ts="2026-08-01T12:00:00Z")

    assert event["prev_hash"] == MANDATE_HASH


def test_each_event_links_to_the_previous_event_hash(chain):
    events = chain["events"]

    assert events[1]["prev_hash"] == events[0]["event_hash"]
    assert events[2]["prev_hash"] == events[1]["event_hash"]


def test_event_has_the_documented_shape(chain):
    event = chain["events"][0]

    assert set(event) == {
        "event_id",
        "ts",
        "mandate_id",
        "type",
        "payload",
        "prev_hash",
        "event_hash",
    }
    assert len(event["event_hash"]) == 64


def test_event_hash_excludes_itself_but_covers_prev_hash(chain):
    event = chain["events"][1]

    assert compute_event_hash(event, event["prev_hash"]) == event["event_hash"]

    # Same event, different anchor -> different hash. This is what makes it a chain.
    assert compute_event_hash(event, "f" * 64) != event["event_hash"]


def test_unknown_event_type_is_rejected(ledger):
    """The taxonomy is closed -- a typo'd type must not enter the evidence record."""
    with pytest.raises(LedgerError):
        append_event(ledger, "MANDATE_VIBES", {}, ts="2026-08-01T12:00:00Z")


def test_every_spec_event_type_is_accepted(ledger):
    for index, event_type in enumerate(EVENT_TYPES):
        append_event(ledger, event_type, {}, ts=f"2026-08-01T12:{index:02d}:00Z")

    assert verify_chain(ledger)["valid"]


def test_payload_with_a_float_is_rejected(ledger):
    """Money is decimal strings; a float would also make the hash non-deterministic."""
    with pytest.raises(LedgerError):
        append_event(ledger, "AGENT_PROPOSAL", {"total": 27.0}, ts="2026-08-01T12:00:00Z")


# --- verification -----------------------------------------------------------


def test_an_untampered_chain_verifies(chain):
    result = verify_chain(chain)

    assert result["valid"]
    assert all(link["valid"] for link in result["links"])
    assert len(result["links"]) == 3


def test_verify_reports_per_link_so_the_ui_can_render_green_and_red(chain):
    """A payload edit localizes to exactly the altered event.

    Later links stay green because they correctly point at the *stored* hash of
    the tampered event, which the edit did not touch. That is the behavior we
    want for evidence: the report names which event was altered rather than
    smearing suspicion across everything after it. Chain-level `valid` is still
    False, so nothing is glossed over.
    """
    chain["events"][1]["payload"]["at"] = "tampered"

    result = verify_chain(chain)

    assert not result["valid"]
    assert [link["valid"] for link in result["links"]] == [True, False, True]
    assert result["links"][1]["reason"] == "event_hash mismatch"


def test_edited_payload_breaks_the_chain(chain):
    chain["events"][0]["payload"]["mandate_id"] = "m2"

    assert not verify_chain(chain)["valid"]


def test_edited_event_hash_breaks_the_chain(chain):
    chain["events"][0]["event_hash"] = "b" * 64

    assert not verify_chain(chain)["valid"]


def test_recomputing_a_tampered_event_hash_still_breaks_the_next_link(chain):
    """The classic forgery attempt: edit the payload, then fix up its own hash."""
    chain["events"][0]["payload"]["mandate_id"] = "m2"
    chain["events"][0]["event_hash"] = compute_event_hash(
        chain["events"][0], chain["events"][0]["prev_hash"]
    )

    result = verify_chain(chain)

    assert not result["valid"]
    assert result["links"][0]["valid"]  # the forged event is internally consistent
    assert not result["links"][1]["valid"]  # but the next link no longer points at it
    assert result["links"][1]["reason"] == "prev_hash does not match the preceding event"


def test_deleting_an_event_breaks_the_chain(chain):
    del chain["events"][1]

    assert not verify_chain(chain)["valid"]


def test_reordering_events_breaks_the_chain(chain):
    chain["events"][0], chain["events"][1] = chain["events"][1], chain["events"][0]

    assert not verify_chain(chain)["valid"]


def test_inserting_a_forged_event_breaks_the_chain(chain):
    forged = dict(chain["events"][2])
    forged["payload"] = {"proposal_id": "forged"}
    chain["events"].insert(2, forged)

    assert not verify_chain(chain)["valid"]


def test_wrong_anchor_breaks_the_genesis_link(chain):
    """A chain re-anchored to a different mandate is not evidence about this one."""
    chain["mandate_hash"] = "c" * 64

    result = verify_chain(chain)

    assert not result["valid"]
    assert result["links"][0]["reason"] == "genesis prev_hash is not the mandate_hash"


def test_empty_ledger_is_trivially_valid(ledger):
    result = verify_chain(ledger)

    assert result["valid"]
    assert result["links"] == []


# --- append-only ------------------------------------------------------------


def test_append_returns_the_stored_event_not_a_live_reference(ledger):
    payload = {"mandate_id": "m1"}
    event = append_event(ledger, "MANDATE_CREATED", payload, ts="2026-08-01T12:00:00Z")

    payload["mandate_id"] = "mutated after the fact"

    assert ledger["events"][0]["payload"]["mandate_id"] == "m1"
    assert event["payload"]["mandate_id"] == "m1"


def test_credential_events_must_not_carry_a_full_token(ledger):
    """SPEC §12 is a hard rule, so the ledger refuses rather than trusts the caller."""
    with pytest.raises(LedgerError):
        append_event(
            ledger,
            "CREDENTIALS_RECEIVED",
            {"token": "4111111111111111", "txn_ref_id": "tli_1"},
            ts="2026-08-01T12:00:00Z",
        )

    with pytest.raises(LedgerError):
        append_event(
            ledger,
            "CREDENTIALS_RECEIVED",
            {"dynamic_cvv": "123", "txn_ref_id": "tli_1"},
            ts="2026-08-01T12:00:00Z",
        )


def test_masked_credential_event_is_accepted(ledger):
    event = append_event(
        ledger,
        "CREDENTIALS_RECEIVED",
        {
            "txn_ref_id": "tli_1",
            "token_last4": "9563",
            "expiry_month": "12",
            "expiry_year": "2027",
        },
        ts="2026-08-01T12:00:00Z",
    )

    assert event["payload"]["token_last4"] == "9563"
