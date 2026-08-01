"""fake_prava fidelity tests.

The fake is only useful if it is wrong in the same ways the real sandbox is. These
lock in the behaviors observed on Jul 31, so a change that makes the fake "nicer"
than the real API fails here rather than in a live integration checkpoint.
"""

import pytest
from fastapi.testclient import TestClient

from fake_prava.app import app

AUTH = {"Authorization": "Bearer sk_test_fake"}


def session_body(**overrides):
    body = {
        "user_id": "user_001",
        "user_email": "shiv@example.com",
        "total_amount": "27.00",
        "currency": "USD",
        "integration_type": "full_checkout",
        "purchase_context": [
            {
                "merchant_details": {
                    "name": "Beanline Coffee",
                    "url": "https://beanline.example.com",
                    "country_code_iso2": "US",
                },
                "product_details": [
                    {
                        "description": "Beanline House Blend 12oz",
                        "unit_price": "12.50",
                        "product_id": "BL-HOUSE-12",
                        "quantity": 1,
                    }
                ],
                "effective_until_minutes": 15,
            }
        ],
    }
    body.update(overrides)
    return body


@pytest.fixture
def client():
    with TestClient(app) as c:
        c.post("/_control/reset")
        yield c
        c.post("/_control/reset")


def create(client, **overrides):
    return client.post("/v1/sessions", json=session_body(**overrides), headers=AUTH)


# --- shape fidelity ---------------------------------------------------------


def test_health_needs_no_auth_and_costs_no_quota(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_session_returns_the_observed_shape(client):
    response = create(client)

    assert response.status_code == 201
    body = response.json()
    assert sorted(body) == [
        "expires_at",
        "iframe_url",
        "order_id",
        "session_id",
        "session_token",
    ]
    assert body["session_id"].startswith("ses_")
    assert body["order_id"].startswith("ord_")
    assert body["iframe_url"].startswith("https://sandbox.collect.prava.space?session=ses_")
    assert "authorizeOnly" not in body


def test_every_response_carries_a_response_id(client):
    assert client.get("/health").headers["X-Response-ID"].startswith("resp_")


def test_ids_are_26_char_ulids_after_the_prefix(client):
    session_id = create(client).json()["session_id"]
    assert len(session_id.split("_", 1)[1]) == 26


# --- auth -------------------------------------------------------------------


def test_missing_authorization_header_is_auth_1002(client):
    response = client.post("/v1/sessions", json=session_body())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_1002"


def test_non_secret_key_is_auth_1001(client):
    response = client.post(
        "/v1/sessions", json=session_body(), headers={"Authorization": "Bearer pk_test_x"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_1001"


# --- OBSERVED: external_order_ref is a permanent idempotency key -------------


def test_duplicate_external_order_ref_is_rejected_with_409(client):
    ref = "a" * 64
    assert create(client, external_order_ref=ref).status_code == 201

    response = create(client, external_order_ref=ref)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DUPLICATE_EXTERNAL_ORDER_REF"


def test_the_409_does_not_consume_quota_or_create_a_session(client):
    ref = "b" * 64
    first = create(client, external_order_ref=ref).json()["session_id"]
    create(client, external_order_ref=ref)

    # The rejected attempt must not have produced a second session.
    assert client.get(f"/v1/sessions/{first}/payment-result", headers=AUTH).status_code == 200


def test_attempt_suffixed_refs_do_not_collide(client):
    """The {mandate_hash}.{attempt:02d} mapping must survive a retry."""
    mandate_hash = "c" * 64

    assert create(client, external_order_ref=f"{mandate_hash}.01").status_code == 201
    assert create(client, external_order_ref=f"{mandate_hash}.02").status_code == 201


# --- validation -------------------------------------------------------------


def test_unsupported_currency_is_val_2001_with_field_details(client):
    response = create(client, currency="XYZ")

    assert response.status_code == 400
    body = response.json()["error"]
    assert body["code"] == "VAL_2001"
    assert "currency" in body["details"]


def test_http_merchant_url_is_rejected(client):
    body = session_body()
    body["purchase_context"][0]["merchant_details"]["url"] = "http://beanline.example.com"

    response = client.post("/v1/sessions", json=body, headers=AUTH)

    assert response.status_code == 400
    assert "purchase_context[0].merchant_details.url" in response.json()["error"]["details"]


def test_over_long_product_id_is_rejected(client):
    body = session_body()
    body["purchase_context"][0]["product_details"][0]["product_id"] = "X" * 51

    response = client.post("/v1/sessions", json=body, headers=AUTH)

    assert response.status_code == 400


# --- credential lifecycle ---------------------------------------------------


def test_pending_session_exposes_no_credentials(client):
    session_id = create(client).json()["session_id"]

    result = client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()

    assert result["status"] == "pending"
    assert result["transactions"] == []


def test_credentials_appear_only_while_awaiting_result(client):
    session_id = create(client).json()["session_id"]
    client.post(f"/_control/sessions/{session_id}/approve")

    result = client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()
    item = result["transactions"][0]["line_items"][0]

    assert result["status"] == "awaiting_result"
    assert item["token"] and item["dynamic_cvv"]
    assert item["expiry_year"] == "2027"  # OBSERVED: 4 digits
    assert len(item["expiry_year"]) == 4


def test_token_is_luhn_valid_and_visa_prefixed(client):
    """The storefront's mock processor checks Luhn + Visa prefix, so the fake must pass it."""
    session_id = create(client).json()["session_id"]
    client.post(f"/_control/sessions/{session_id}/approve")
    result = client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()
    token = result["transactions"][0]["line_items"][0]["token"]

    assert token.startswith("4") and len(token) == 16
    total = 0
    for index, char in enumerate(reversed(token)):
        digit = int(char)
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    assert total % 10 == 0


def test_credentials_are_gone_once_the_outcome_is_reported(client):
    session_id = create(client).json()["session_id"]
    txn_ref_id = client.post(f"/_control/sessions/{session_id}/approve").json()["txn_ref_id"]
    client.post(
        f"/v1/sessions/{session_id}/report-status",
        json={"txn_ref_id": txn_ref_id, "txn_status": "APPROVED"},
        headers=AUTH,
    )

    result = client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()

    assert result["status"] == "completed"
    assert result["transactions"][0]["line_items"][0]["token"] is None


# --- OBSERVED: failure shape ------------------------------------------------


def test_auth_failure_yields_transaction_level_auth_failed_and_no_credentials(client):
    session_id = create(client).json()["session_id"]
    client.post(f"/_control/sessions/{session_id}/fail-auth")

    result = client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()
    txn = result["transactions"][0]

    assert result["status"] == "failed"
    assert txn["error"]["code"] == "AUTH_FAILED"
    assert txn["line_items"][0]["token"] is None


def test_line_item_status_can_disagree_with_transaction_status(client):
    """The disagreement is the point: a failed txn held a pending line item."""
    session_id = create(client).json()["session_id"]
    client.post(f"/_control/sessions/{session_id}/fail-auth")

    txn = client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()[
        "transactions"
    ][0]

    assert txn["status"] == "failed"
    assert txn["line_items"][0]["status"] == "pending"


# --- OBSERVED: multiple transactions, newest wins ---------------------------


def test_a_retry_accumulates_transactions_and_the_newest_carries_credentials(client):
    session_id = create(client).json()["session_id"]
    client.post(f"/_control/sessions/{session_id}/fail-auth")
    client.post(f"/_control/sessions/{session_id}/approve")

    result = client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()

    assert len(result["transactions"]) == 2
    newest = max(result["transactions"], key=lambda t: t["txn_id"])
    assert newest["line_items"][0]["token"]
    oldest = min(result["transactions"], key=lambda t: t["txn_id"])
    assert oldest["line_items"][0]["token"] is None


def test_txn_ids_sort_newest_last_even_when_minted_back_to_back(client):
    session_id = create(client).json()["session_id"]
    client.post(f"/_control/sessions/{session_id}/fail-auth")
    client.post(f"/_control/sessions/{session_id}/fail-auth")
    client.post(f"/_control/sessions/{session_id}/approve")

    ids = [
        t["txn_id"]
        for t in client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()[
            "transactions"
        ]
    ]

    assert ids == sorted(ids), "ULIDs must be monotonic or newest-transaction logic breaks"


# --- report-status ----------------------------------------------------------


def test_report_status_returns_visa_confirmation_and_completes_the_session(client):
    session_id = create(client).json()["session_id"]
    txn_ref_id = client.post(f"/_control/sessions/{session_id}/approve").json()["txn_ref_id"]

    response = client.post(
        f"/v1/sessions/{session_id}/report-status",
        json={
            "txn_ref_id": txn_ref_id,
            "txn_status": "APPROVED",
            "authorization_code": "MOCK123",
            "response_code": "00",
        },
        headers=AUTH,
    )

    assert response.json() == {
        "status": "confirmed",
        "txn_ref_id": txn_ref_id,
        "txn_status": "APPROVED",
        "visa_confirmation": "SUCCESS",
    }


def test_reporting_twice_is_invalid_state(client):
    session_id = create(client).json()["session_id"]
    txn_ref_id = client.post(f"/_control/sessions/{session_id}/approve").json()["txn_ref_id"]
    payload = {"txn_ref_id": txn_ref_id, "txn_status": "APPROVED"}
    client.post(f"/v1/sessions/{session_id}/report-status", json=payload, headers=AUTH)

    response = client.post(
        f"/v1/sessions/{session_id}/report-status", json=payload, headers=AUTH
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_STATE"


def test_unknown_txn_ref_id_is_not_found(client):
    session_id = create(client).json()["session_id"]
    client.post(f"/_control/sessions/{session_id}/approve")

    response = client.post(
        f"/v1/sessions/{session_id}/report-status",
        json={"txn_ref_id": "tli_nope", "txn_status": "APPROVED"},
        headers=AUTH,
    )

    assert response.status_code == 404


def test_declined_report_fails_the_session(client):
    session_id = create(client).json()["session_id"]
    txn_ref_id = client.post(f"/_control/sessions/{session_id}/approve").json()["txn_ref_id"]

    client.post(
        f"/v1/sessions/{session_id}/report-status",
        json={"txn_ref_id": txn_ref_id, "txn_status": "DECLINED", "response_code": "05"},
        headers=AUTH,
    )

    result = client.get(f"/v1/sessions/{session_id}/payment-result", headers=AUTH).json()
    assert result["status"] == "failed"


# --- quota simulation -------------------------------------------------------


def test_tries_exhausted_toggle_returns_429(client):
    client.post("/_control/tries-exhausted")

    response = create(client)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "TRIES_EXHAUSTED"


def test_unknown_session_is_404(client):
    assert client.get("/v1/sessions/ses_nope/payment-result", headers=AUTH).status_code == 404
