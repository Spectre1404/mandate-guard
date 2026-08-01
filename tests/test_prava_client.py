"""Prava client, tested against fake_prava — SPEC §5.

Runs entirely against the local fake, which reproduces the behaviors the real
sandbox actually exhibited. No sandbox quota is spent by this file.

The four things worth proving:
  * the session payload is built ONLY from a gate-verified proposal
  * external_order_ref is attempt-suffixed, so a retry after failure is possible
  * the poller is bounded by the session's own expires_at, not a fixed timeout
  * credentials come from the NEWEST transaction, and never reach the ledger
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.gate.verdict import evaluate
from backend.ledger.chain import new_ledger, verify_chain
from backend.prava_client.client import (
    PravaClient,
    PravaError,
    PollTimeout,
    build_session_payload,
)
from fake_prava.app import app

MANDATE_HASH = "d" * 64


@pytest.fixture
def transport():
    with TestClient(app) as c:
        c.post("/_control/reset")
        yield c
        c.post("/_control/reset")


@pytest.fixture
def client(transport):
    return PravaClient(
        base_url="", secret_key="sk_test_fake", session=transport, sleep=lambda _: None
    )


@pytest.fixture
def verified(mandate, proposal, now):
    """A proposal that actually passed the gate -- the only thing allowed to buy."""
    mandate = dict(mandate, mandate_hash=MANDATE_HASH)
    verdict = evaluate(mandate, proposal, now=now)
    assert verdict["verdict"] == "PASS"
    return mandate, proposal


# --- payload mapping --------------------------------------------------------


def test_payload_is_built_from_the_mandate_and_verified_proposal(verified):
    mandate, proposal = verified

    payload = build_session_payload(mandate, proposal, attempt=1)

    assert payload["user_id"] == mandate["user"]["user_id"]
    assert payload["user_email"] == mandate["user"]["user_email"]
    assert payload["total_amount"] == proposal["proposed_total"]
    assert payload["currency"] == mandate["constraints"]["currency"]
    assert payload["integration_type"] == "full_checkout"

    context = payload["purchase_context"][0]
    assert context["merchant_details"] == {
        "name": "Beanline Coffee",
        "url": "https://beanline.example.com",
        "country_code_iso2": "US",
    }
    assert context["effective_until_minutes"] == 15
    assert [p["product_id"] for p in context["product_details"]] == [
        "BL-HOUSE-12",
        "BL-FILTER-100",
    ]
    assert context["product_details"][1]["quantity"] == 2


def test_callback_url_is_omitted_because_we_poll(verified):
    mandate, proposal = verified

    assert "callback_url" not in build_session_payload(mandate, proposal, attempt=1)


def test_external_order_ref_is_the_hash_plus_a_zero_padded_attempt(verified):
    mandate, proposal = verified

    first = build_session_payload(mandate, proposal, attempt=1)["external_order_ref"]
    tenth = build_session_payload(mandate, proposal, attempt=10)["external_order_ref"]

    assert first == f"{MANDATE_HASH}.01"
    assert tenth == f"{MANDATE_HASH}.10"
    assert len(first) <= 255


def test_payload_amounts_are_the_proposals_not_the_mandates_caps(verified):
    """total_amount is the verified total, which becomes the authorized cap."""
    mandate, proposal = verified

    payload = build_session_payload(mandate, proposal, attempt=1)

    assert payload["total_amount"] == "27.00"  # proposed, not the 30.00 ceiling
    assert payload["purchase_context"][0]["product_details"][0]["unit_price"] == "12.50"


def test_a_failed_proposal_can_never_build_a_payload(mandate, proposal, mutate, now):
    """Session payloads are built ONLY from gate-verified proposals."""
    drifted = mutate(proposal, lambda p: p["line_items"][0].update(unit_price="99.00"))

    with pytest.raises(PravaError):
        build_session_payload(mandate, drifted, attempt=1, require_verdict=evaluate(
            mandate, drifted, now=now
        ))


# --- session creation -------------------------------------------------------


def test_create_session_returns_the_observed_fields(client, verified):
    mandate, proposal = verified

    session = client.create_session(mandate, proposal, attempt=1)

    assert session["session_id"].startswith("ses_")
    assert session["order_id"].startswith("ord_")
    assert session["iframe_url"].startswith("https://sandbox.collect.prava.space")


def test_duplicate_external_order_ref_raises_with_the_409_code(client, verified):
    mandate, proposal = verified
    client.create_session(mandate, proposal, attempt=1)

    with pytest.raises(PravaError) as exc:
        client.create_session(mandate, proposal, attempt=1)

    assert exc.value.code == "DUPLICATE_EXTERNAL_ORDER_REF"
    assert exc.value.status == 409


def test_bumping_the_attempt_lets_a_retry_succeed(client, verified):
    """The whole reason for the attempt suffix: a failed run must be retryable."""
    mandate, proposal = verified
    client.create_session(mandate, proposal, attempt=1)

    second = client.create_session(mandate, proposal, attempt=2)

    assert second["session_id"].startswith("ses_")


def test_tries_exhausted_is_surfaced_loudly(client, verified, transport):
    mandate, proposal = verified
    transport.post("/_control/tries-exhausted")

    with pytest.raises(PravaError) as exc:
        client.create_session(mandate, proposal, attempt=1)

    assert exc.value.code == "TRIES_EXHAUSTED"
    assert exc.value.is_quota_exhausted


# --- polling ----------------------------------------------------------------


def test_poll_returns_once_credentials_are_available(client, verified, transport):
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    transport.post(f"/_control/sessions/{session['session_id']}/approve")

    result = client.poll_until_credentials(session)

    assert result["status"] == "awaiting_result"


def test_poll_is_bounded_by_expires_at_not_a_fixed_timeout(client, verified):
    """SPEC §5: the session's own expiry is the only real deadline."""
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    # Comfortably past expiry + the 10s grace, so the deadline has already passed
    # and the poller gives up after one look rather than spinning.
    session = dict(
        session,
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace(
            "+00:00", "Z"
        ),
    )

    with pytest.raises(PollTimeout):
        client.poll_until_credentials(session)


def test_poll_records_every_status_transition(client, verified, transport):
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    seen = []

    def approve_after_first_poll(status):
        seen.append(status)
        if status == "pending":
            transport.post(f"/_control/sessions/{session['session_id']}/approve")

    client.poll_until_credentials(session, on_status=approve_after_first_poll)

    assert seen[0] == "pending"
    assert seen[-1] == "awaiting_result"


def test_unknown_status_does_not_raise(client, verified, monkeypatch):
    """The docs disagree on the enum; an unknown status is transient, not fatal."""
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    responses = iter(
        [
            {"status": "processing", "transactions": []},
            {"status": "reticulating_splines", "transactions": []},
            {
                "status": "awaiting_result",
                "transactions": [
                    {
                        "txn_id": "txn_1",
                        "status": "awaiting_result",
                        "line_items": [
                            {
                                "txn_ref_id": "tli_1",
                                # Only needs to be truthy; no PAN-shaped literal required.
                                "token": "token-stub-9563",
                                "dynamic_cvv": "123",
                                "expiry_month": "12",
                                "expiry_year": "2027",
                                "total_amount": "27.00",
                            }
                        ],
                    }
                ],
            },
        ]
    )
    monkeypatch.setattr(client, "get_payment_result", lambda _sid: next(responses))

    seen = []
    result = client.poll_until_credentials(session, on_status=seen.append)

    assert seen == ["processing", "reticulating_splines", "awaiting_result"]
    assert result["status"] == "awaiting_result"


# --- credential extraction --------------------------------------------------


def test_credentials_come_from_the_newest_transaction(client, verified, transport):
    """A retried session carries a dead transaction first; the newest one wins."""
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    session_id = session["session_id"]
    transport.post(f"/_control/sessions/{session_id}/fail-auth")
    approved = transport.post(f"/_control/sessions/{session_id}/approve").json()

    result = client.get_payment_result(session_id)
    credentials = client.extract_credentials(result)

    assert credentials["txn_ref_id"] == approved["txn_ref_id"]
    assert credentials["token_last4"] == credentials["_token"][-4:]


def test_the_newest_of_two_credentialed_transactions_wins(client, verified, transport):
    """Ordering only bites when more than one transaction carries a token.

    The failed-then-approved case above passes even with the sort reversed, because
    a failed transaction has no token to pick. This is the case that actually pins
    newest-first: two live transactions, and the stale one must lose.
    """
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    session_id = session["session_id"]
    stale = transport.post(f"/_control/sessions/{session_id}/approve").json()["txn_ref_id"]
    newest = transport.post(f"/_control/sessions/{session_id}/approve").json()["txn_ref_id"]

    credentials = client.extract_credentials(client.get_payment_result(session_id))

    assert credentials["txn_ref_id"] == newest
    assert credentials["txn_ref_id"] != stale


def test_extract_credentials_returns_none_when_there_are_none(client, verified, transport):
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    transport.post(f"/_control/sessions/{session['session_id']}/fail-auth")

    result = client.get_payment_result(session["session_id"])

    assert client.extract_credentials(result) is None


def test_ledger_payload_for_credentials_carries_only_masked_fields(client, verified, transport):
    """SPEC §12 -- the full token and CVV must never reach the ledger."""
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    transport.post(f"/_control/sessions/{session['session_id']}/approve")
    credentials = client.extract_credentials(client.get_payment_result(session["session_id"]))

    payload = client.credentials_ledger_payload(credentials)

    assert set(payload) == {"txn_ref_id", "token_last4", "expiry_month", "expiry_year"}
    assert len(payload["token_last4"]) == 4

    ledger = new_ledger(MANDATE_HASH)
    from backend.ledger.chain import append_event

    append_event(ledger, "CREDENTIALS_RECEIVED", payload, ts="2026-08-01T12:00:00Z")
    assert verify_chain(ledger)["valid"]


# --- report status ----------------------------------------------------------


def test_report_approved_returns_visa_confirmation(client, verified, transport):
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    txn_ref_id = transport.post(
        f"/_control/sessions/{session['session_id']}/approve"
    ).json()["txn_ref_id"]

    report = client.report_status(
        session["session_id"], txn_ref_id, "APPROVED", authorization_code="BL123", response_code="00"
    )

    assert report["visa_confirmation"] == "SUCCESS"
    assert report["txn_status"] == "APPROVED"


def test_response_code_longer_than_two_chars_is_refused_before_the_call(client):
    """Prava caps response_code at 2; catching it here beats a 400 round trip."""
    with pytest.raises(PravaError):
        client.report_status("ses_x", "tli_x", "APPROVED", response_code="000")


def test_invalid_txn_status_is_refused_before_the_call(client):
    with pytest.raises(PravaError):
        client.report_status("ses_x", "tli_x", "MAYBE")


def test_reporting_completes_the_session(client, verified, transport):
    mandate, proposal = verified
    session = client.create_session(mandate, proposal, attempt=1)
    txn_ref_id = transport.post(
        f"/_control/sessions/{session['session_id']}/approve"
    ).json()["txn_ref_id"]
    client.report_status(session["session_id"], txn_ref_id, "APPROVED")

    final = client.poll_until_terminal(session)

    assert final["status"] == "completed"


# --- errors -----------------------------------------------------------------


def test_missing_auth_raises_auth_1002():
    with TestClient(app) as transport:
        client = PravaClient(base_url="", secret_key="", session=transport, sleep=lambda _: None)
        with pytest.raises(PravaError) as exc:
            client.get_payment_result("ses_anything")
    assert exc.value.code == "AUTH_1002"


def test_unknown_session_raises_not_found(client):
    with pytest.raises(PravaError) as exc:
        client.get_payment_result("ses_nope")

    assert exc.value.code == "NOT_FOUND"
    assert exc.value.status == 404


def test_errors_carry_the_response_id_for_support(client):
    with pytest.raises(PravaError) as exc:
        client.get_payment_result("ses_nope")

    assert exc.value.response_id
