"""fake_prava — a local stand-in for the three Prava endpoints we depend on.

Built from the responses the real sandbox actually returned during the Jul 31
spike, not from the docs alone. Where the docs and the live API disagreed, this
follows the live API, because that is what our client has to survive.

It exists to protect the sandbox's unpublished session quota: all routine
iteration runs here, and the real sandbox is reserved for deliberate integration
checkpoints. **It never appears in the demo or the video.**

Reproduced observed behaviors (each marked OBSERVED below):
  * 409 DUPLICATE_EXTERNAL_ORDER_REF — external_order_ref is a permanent
    per-merchant idempotency key. This is the behavior that forced the
    {mandate_hash}.{attempt:02d} mapping, so the fake must enforce it or the
    attempt-counter logic goes untested until it breaks against the real API.
  * transactions[].status can disagree with line_items[].status (a failed
    transaction held a pending line item).
  * A session accumulates multiple transactions across retries; credentials must
    be read from the newest by txn_id.
  * ses_/ord_/txn_/tli_ + ULID id shapes; 4-digit expiry_year.
  * token and dynamic_cvv are present only while status is awaiting_result.
  * AUTH_FAILED as a transaction-level error when passkey/OTP fails.

ASSUMED, because we never observed it (marked ASSUMED below): the DECLINED
report path, session auto-expiry, and TRIES_EXHAUSTED. These are modelled from
the docs and flagged so nobody mistakes them for verified behavior.

Control routes are prefixed `_control` and are NOT part of Prava's API. They
stand in for the human at the hosted card page.

Run:  .venv/bin/uvicorn fake_prava.app:app --port 8100
"""

import os
import random
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from fake_prava.ids import new_id

app = FastAPI(title="fake_prava")

SESSION_TTL_MINUTES = 15
SUPPORTED_CURRENCIES = {"USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY"}
PRODUCT_ID_MAX = 50
EXTERNAL_ORDER_REF_MAX = 255

# In-memory store; a restart is a clean slate.
SESSIONS = {}
SEEN_EXTERNAL_ORDER_REFS = set()
STATE = {"tries_exhausted": False}


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def error(status, code, message, details=None):
    body = {"error": {"code": code, "message": message}}
    if details is not None:
        body["error"]["details"] = details
    return JSONResponse(status_code=status, content=body)


@app.middleware("http")
async def add_response_id(request: Request, call_next):
    """Every real response carries X-Response-ID; support asks for it."""
    response = await call_next(request)
    response.headers["X-Response-ID"] = new_id("resp")
    return response


def check_auth(authorization):
    """Bearer sk_test_* only, mirroring the documented auth model."""
    if not authorization or not authorization.startswith("Bearer "):
        return error(401, "AUTH_1002", "Missing or invalid Authorization header")
    token = authorization[len("Bearer ") :].strip()
    if not token.startswith("sk_test_"):
        return error(401, "AUTH_1001", "Invalid API key")
    return None


# --- fake credentials --------------------------------------------------------


def luhn_visa_number():
    """A Luhn-valid 16-digit number starting with 4.

    Generated at runtime, never hardcoded: real sandbox test card numbers must not
    appear in this repository, and the storefront's mock processor checks Luhn +
    Visa prefix, so a random string would not exercise it honestly.
    """
    digits = [4] + [random.randint(0, 9) for _ in range(14)]
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    digits.append((10 - checksum % 10) % 10)
    return "".join(str(d) for d in digits)


# --- endpoints ---------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok", "timestamp": iso(now_utc())}


@app.post("/v1/sessions", status_code=201)
async def create_session(request: Request, authorization: str = Header(default=None)):
    unauthorized = check_auth(authorization)
    if unauthorized:
        return unauthorized

    if STATE["tries_exhausted"]:
        # ASSUMED shape: documented as 429 on create-session, never seen live.
        return error(
            429, "TRIES_EXHAUSTED", "Sandbox test-transaction limit reached for this merchant"
        )

    try:
        body = await request.json()
    except Exception:
        return error(400, "VAL_2001", "Validation failed", {"body": "invalid JSON"})

    details = _validate_session_body(body)
    if details:
        return error(400, "VAL_2001", "Validation failed", details)

    external_order_ref = body.get("external_order_ref")
    if external_order_ref:
        # OBSERVED: permanent per-merchant uniqueness. Rejected before creation,
        # so it costs no session quota -- which is why our client can retry safely.
        if external_order_ref in SEEN_EXTERNAL_ORDER_REFS:
            return error(
                409,
                "DUPLICATE_EXTERNAL_ORDER_REF",
                f'An order with external_order_ref "{external_order_ref}" '
                "already exists for this merchant",
            )
        SEEN_EXTERNAL_ORDER_REFS.add(external_order_ref)

    created = now_utc()
    session_id = new_id("ses")
    order_id = new_id("ord")
    session = {
        "session_id": session_id,
        "order_id": order_id,
        "status": "pending",
        "created_at": created,
        "expires_at": created + timedelta(minutes=SESSION_TTL_MINUTES),
        "request": body,
        "transactions": [],
    }
    SESSIONS[session_id] = session

    return {
        "session_id": session_id,
        "session_token": "eyJhbGciOi" + new_id("tok"),
        "iframe_url": f"https://sandbox.collect.prava.space?session={session_id}",
        "order_id": order_id,
        "expires_at": iso(session["expires_at"]),
    }


def _validate_session_body(body):
    details = {}
    if not isinstance(body, dict):
        return {"body": "must be an object"}

    for field in ("user_id", "user_email", "total_amount", "currency"):
        if not body.get(field):
            details[field] = "required"

    currency = body.get("currency")
    if currency and currency not in SUPPORTED_CURRENCIES:
        details["currency"] = "Unsupported currency code"

    ref = body.get("external_order_ref")
    if ref is not None and (not isinstance(ref, str) or len(ref) > EXTERNAL_ORDER_REF_MAX):
        details["external_order_ref"] = f"must be a string of at most {EXTERNAL_ORDER_REF_MAX} chars"

    contexts = body.get("purchase_context")
    if not isinstance(contexts, list) or len(contexts) != 1:
        details["purchase_context"] = "exactly one entry required"
        return details

    context = contexts[0]
    merchant = context.get("merchant_details") or {}
    if not merchant.get("name"):
        details["purchase_context[0].merchant_details.name"] = "required"
    url = merchant.get("url") or ""
    if not url.startswith("https://"):
        details["purchase_context[0].merchant_details.url"] = "must use https"
    country = merchant.get("country_code_iso2") or ""
    if len(country) != 2 or not country.isupper():
        details["purchase_context[0].merchant_details.country_code_iso2"] = (
            "must be 2 uppercase ISO 3166-1 letters"
        )

    products = context.get("product_details")
    if not isinstance(products, list) or not products:
        details["purchase_context[0].product_details"] = "at least one product required"
    else:
        for index, product in enumerate(products):
            prefix = f"purchase_context[0].product_details[{index}]"
            if not product.get("description"):
                details[f"{prefix}.description"] = "required"
            if not product.get("unit_price"):
                details[f"{prefix}.unit_price"] = "required"
            product_id = product.get("product_id")
            if product_id is not None and len(str(product_id)) > PRODUCT_ID_MAX:
                details[f"{prefix}.product_id"] = f"max {PRODUCT_ID_MAX} characters"
    return details


@app.get("/v1/sessions/{session_id}/payment-result")
def payment_result(session_id: str, authorization: str = Header(default=None)):
    unauthorized = check_auth(authorization)
    if unauthorized:
        return unauthorized

    session = SESSIONS.get(session_id)
    if not session:
        return error(404, "NOT_FOUND", "Session not found")

    return {
        "session_id": session["session_id"],
        "order_id": session["order_id"],
        "status": session["status"],
        "transactions": [_render_transaction(t) for t in session["transactions"]],
    }


def _render_transaction(txn):
    rendered = {
        "txn_id": txn["txn_id"],
        "status": txn["status"],
        "line_items": [_render_line_item(item, txn) for item in txn["line_items"]],
    }
    if txn.get("error"):
        rendered["error"] = txn["error"]
    return rendered


def _render_line_item(item, txn):
    # OBSERVED: credentials appear only while awaiting_result; once the outcome is
    # reported they are gone, even though the line item still exists.
    expose = txn["status"] == "awaiting_result"
    return {
        "txn_ref_id": item["txn_ref_id"],
        "merchant_name": item["merchant_name"],
        "merchant_url": item["merchant_url"],
        "total_amount": item["total_amount"],
        # OBSERVED: line-item status can disagree with the transaction's status.
        "status": item["status"],
        "token": item["token"] if expose else None,
        "dynamic_cvv": item["dynamic_cvv"] if expose else None,
        "expiry_month": item["expiry_month"] if expose else None,
        "expiry_year": item["expiry_year"] if expose else None,
        "products": item["products"],
    }


@app.post("/v1/sessions/{session_id}/report-status")
async def report_status(
    session_id: str, request: Request, authorization: str = Header(default=None)
):
    unauthorized = check_auth(authorization)
    if unauthorized:
        return unauthorized

    session = SESSIONS.get(session_id)
    if not session:
        return error(404, "NOT_FOUND", "Session not found")

    body = await request.json()
    txn_ref_id = body.get("txn_ref_id")
    txn_status = body.get("txn_status")

    if txn_status not in ("APPROVED", "DECLINED"):
        return error(400, "VAL_2001", "Validation failed", {"txn_status": "must be APPROVED or DECLINED"})

    target = None
    for txn in session["transactions"]:
        for item in txn["line_items"]:
            if item["txn_ref_id"] == txn_ref_id:
                target = (txn, item)
    if target is None:
        return error(404, "NOT_FOUND", "Session or txn_ref_id not found")

    txn, item = target
    if txn["status"] != "awaiting_result":
        return error(400, "INVALID_STATE", "No transaction awaiting payment result")

    if txn_status == "APPROVED":
        txn["status"] = item["status"] = "completed"
        session["status"] = "completed"
    else:
        # ASSUMED: we only ever observed the APPROVED path live.
        txn["status"] = item["status"] = "failed"
        session["status"] = "failed"

    return {
        "status": "confirmed",
        "txn_ref_id": txn_ref_id,
        "txn_status": txn_status,
        # ASSUMED for DECLINED: visa_confirmation reports whether the outcome was
        # relayed to the network, not whether the purchase succeeded.
        "visa_confirmation": "SUCCESS",
    }


# --- control routes (NOT part of Prava's API) --------------------------------


@app.post("/_control/sessions/{session_id}/approve")
def control_approve(session_id: str):
    """Stand in for the human completing card entry + OTP + passkey."""
    session = SESSIONS.get(session_id)
    if not session:
        return error(404, "NOT_FOUND", "Session not found")

    context = (session["request"].get("purchase_context") or [{}])[0]
    merchant = context.get("merchant_details") or {}
    txn_ref_id = new_id("tli")
    session["transactions"].append(
        {
            "txn_id": new_id("txn"),
            "status": "awaiting_result",
            "line_items": [
                {
                    "txn_ref_id": txn_ref_id,
                    "merchant_name": merchant.get("name"),
                    "merchant_url": merchant.get("url"),
                    "total_amount": session["request"].get("total_amount"),
                    "status": "awaiting_result",
                    "token": luhn_visa_number(),
                    "dynamic_cvv": f"{random.randint(0, 999):03d}",
                    "expiry_month": "12",
                    "expiry_year": "2027",  # OBSERVED: 4 digits, not 2
                    "products": [
                        {
                            "product_ref_id": new_id("prd"),
                            "external_product_id": product.get("product_id"),
                            "name": product.get("description"),
                            "unit_price": product.get("unit_price"),
                            "quantity": product.get("quantity", 1),
                        }
                        for product in (context.get("product_details") or [])
                    ],
                }
            ],
        }
    )
    session["status"] = "awaiting_result"
    return {"ok": True, "txn_ref_id": txn_ref_id}


@app.post("/_control/sessions/{session_id}/fail-auth")
def control_fail_auth(session_id: str):
    """Reproduce the observed passkey failure: AUTH_FAILED, no credentials.

    Note the deliberate inconsistency: the transaction is `failed` while its line
    item stays `pending`. That is exactly what the real sandbox returned, and it is
    why our client keys credential-readiness off token presence.
    """
    session = SESSIONS.get(session_id)
    if not session:
        return error(404, "NOT_FOUND", "Session not found")

    context = (session["request"].get("purchase_context") or [{}])[0]
    merchant = context.get("merchant_details") or {}
    session["transactions"].append(
        {
            "txn_id": new_id("txn"),
            "status": "failed",
            "error": {"code": "AUTH_FAILED", "message": "Authentication failed due to errors."},
            "line_items": [
                {
                    "txn_ref_id": new_id("tli"),
                    "merchant_name": merchant.get("name"),
                    "merchant_url": merchant.get("url"),
                    "total_amount": session["request"].get("total_amount"),
                    "status": "pending",  # OBSERVED disagreement with txn status
                    "token": None,
                    "dynamic_cvv": None,
                    "expiry_month": None,
                    "expiry_year": None,
                    "products": [],
                }
            ],
        }
    )
    session["status"] = "failed"
    return {"ok": True}


@app.post("/_control/tries-exhausted")
def control_tries_exhausted(enabled: bool = True):
    """ASSUMED behavior: lets us exercise the 429 path without burning real quota."""
    STATE["tries_exhausted"] = enabled
    return {"tries_exhausted": enabled}


@app.post("/_control/reset")
def control_reset():
    SESSIONS.clear()
    SEEN_EXTERNAL_ORDER_REFS.clear()
    STATE["tries_exhausted"] = False
    return {"ok": True}
