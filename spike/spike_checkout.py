"""Tier 0 spike: prove the Prava sandbox seam end to end with hardcoded values.

Follows the REST Checkout Walkthrough (docs.prava.space/guides/rest-checkout-walkthrough)
verbatim: create session -> human pays on hosted page -> poll payment-result ->
report-status -> re-poll to completed.

There is no merchant checkout in this spike. Between receiving credentials and
reporting APPROVED we do NOT charge anything -- the storefront and executor come
later (SPEC.md Tier 0.2). The authorization_code here is a mock, clearly labeled.
This script exists to prove the Prava seam, nothing more.

Security (SPEC.md §12): the full token and dynamic CVV live in local variables and
are never printed, written, or returned. Every response is passed through redact()
before it can reach stdout. Only the token's last 4 + expiry + txn_ref_id are shown.

Usage:
    python spike/spike_checkout.py                  # create a new session
    python spike/spike_checkout.py --session ses_x  # resume polling an existing one
                                                    # (protects the sandbox quota)
"""

import argparse
import copy
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

import requests

# --- hardcoded demo values (SPEC.md §5 mapping, §6 storefront) ---------------

USER_ID = "user_spike_001"
USER_EMAIL = "shivanshamba@mhub.org"
CURRENCY = "USD"
UNIT_PRICE = "12.50"  # decimal string, 2dp -- no floats touch a price
QUANTITY = 1
TOTAL_AMOUNT = "12.50"  # == unit_price * quantity (R5 would enforce this)
PRODUCT_ID = "BL-HOUSE-12"  # <= 50 chars (Prava cap)
PRODUCT_DESC = "Beanline House Blend 12oz"
MERCHANT_NAME = "Beanline Coffee"
MERCHANT_URL = "https://beanline.example.com"  # https required by Prava
MERCHANT_COUNTRY = "US"
EFFECTIVE_MINUTES = 15

POLL_FAST_SECONDS = 2
POLL_SLOW_SECONDS = 5
POLL_BACKOFF_AFTER = 30  # switch 2s -> 5s after this many seconds
POLL_TIMEOUT_SECONDS = 180  # 3 minutes

TERMINAL_STATUSES = {"completed", "failed"}
KNOWN_STATUSES = {"pending", "processing", "awaiting_result", "completed", "failed"}
REDACTED_KEYS = {"token", "dynamic_cvv", "session_token"}


# --- helpers ----------------------------------------------------------------


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_env(path):
    env = {}
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def redact(obj):
    """Deep-copy a response with every credential field masked.

    Nothing from an API response reaches stdout without passing through here.
    """
    obj = copy.deepcopy(obj)

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in REDACTED_KEYS and isinstance(value, str) and value:
                    node[key] = f"<redacted:{len(value)}chars>"
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return obj


class PravaError(RuntimeError):
    pass


def request(method, url, secret, **kwargs):
    resp = requests.request(
        method,
        url,
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        timeout=30,
        **kwargs,
    )
    response_id = resp.headers.get("X-Response-ID", "?")
    try:
        body = resp.json()
    except ValueError:
        raise PravaError(
            f"HTTP {resp.status_code} non-JSON body (X-Response-ID={response_id}): "
            f"{resp.text[:300]}"
        )

    if resp.status_code >= 400:
        error = body.get("error", {})
        code = error.get("code", "?")
        if code == "TRIES_EXHAUSTED":
            log("!!! TRIES_EXHAUSTED -- sandbox session allowance is depleted.")
            log("!!! Email support@prava.space with this X-Response-ID, the timestamp")
            log("!!! + timezone, and the environment. They reset it within minutes.")
        raise PravaError(
            f"HTTP {resp.status_code} {code}: {error.get('message', '')} "
            f"details={json.dumps(error.get('details', {}))} "
            f"(X-Response-ID={response_id})"
        )

    return body, response_id


# --- step 1: create session -------------------------------------------------


def create_session(base, secret):
    # Stands in for mandate_hash in the real system (SPEC.md §5): the tie between
    # Prava's records and the evidence chain. Here it is just a test digest.
    #
    # Per-run nonce, deliberately: Prava enforces external_order_ref UNIQUE per
    # merchant and returns an undocumented 409 DUPLICATE_EXTERNAL_ORDER_REF on
    # reuse. A bare mandate_hash would therefore allow exactly one session per
    # mandate for all time -- no retry after a failed passkey or a decline. How
    # the real system keeps the hash tie while staying retryable is an open SPEC
    # §5 decision; the spike only needs a unique test digest.
    external_order_ref = hashlib.sha256(
        f"spike:{USER_ID}:{TOTAL_AMOUNT}:{PRODUCT_ID}:{uuid.uuid4()}".encode()
    ).hexdigest()

    payload = {
        "user_id": USER_ID,
        "user_email": USER_EMAIL,
        "total_amount": TOTAL_AMOUNT,
        "currency": CURRENCY,
        "integration_type": "full_checkout",
        "external_order_ref": external_order_ref,
        # callback_url deliberately omitted -- optional per docs; we poll instead.
        "purchase_context": [
            {
                "merchant_details": {
                    "name": MERCHANT_NAME,
                    "url": MERCHANT_URL,
                    "country_code_iso2": MERCHANT_COUNTRY,
                },
                "product_details": [
                    {
                        "description": PRODUCT_DESC,
                        "unit_price": UNIT_PRICE,
                        "product_id": PRODUCT_ID,
                        "quantity": QUANTITY,
                    }
                ],
                "effective_until_minutes": EFFECTIVE_MINUTES,
            }
        ],
    }

    log(f"POST /v1/sessions  total={TOTAL_AMOUNT} {CURRENCY}")
    log(f"     external_order_ref={external_order_ref}")
    body, response_id = request("POST", f"{base}/v1/sessions", secret, json=payload)

    log(f"201 created  (X-Response-ID={response_id})")
    log(f"  session_id = {body['session_id']}")
    log(f"  order_id   = {body['order_id']}")
    log(f"  expires_at = {body['expires_at']}  (15 minutes -- finish before this)")
    return body


# --- step 3: poll payment-result --------------------------------------------


def find_credential_line_item(result):
    """Return the newest line item carrying credentials, or None.

    A single session can accumulate several transactions (observed: two, after a
    failed passkey attempt was retried). txn_id is a ULID, so lexicographic
    descending order is newest-first -- take credentials from the latest attempt,
    never the first.
    """
    transactions = sorted(
        result.get("transactions") or [],
        key=lambda txn: txn.get("txn_id") or "",
        reverse=True,
    )
    for txn in transactions:
        for item in txn.get("line_items") or []:
            if item.get("token") and item.get("dynamic_cvv"):
                return item
    return None


def poll(base, secret, session_id, until, label, timeout=POLL_TIMEOUT_SECONDS):
    """Poll payment-result until status is in `until`. Logs every transition."""
    url = f"{base}/v1/sessions/{session_id}/payment-result"
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    last_status = None
    polls = 0

    log(f"--- polling payment-result until {sorted(until)} ({label}) ---")
    while True:
        result, _ = request("GET", url, secret)
        polls += 1
        status = result.get("status")

        if status != last_status:
            # The status transition is the ledger-worthy event (SPEC.md §4 #7).
            elapsed = int(time.monotonic() - started)
            log(f"  transition: {last_status or '(start)'} -> {status}  (+{elapsed}s)")
            if status not in KNOWN_STATUSES:
                # Docs disagree on the full enum; treat unknown as transient.
                log(f"  note: '{status}' is not a documented status -- treating as transient")
            last_status = status

        if status in until:
            log(f"  reached '{status}' after {polls} polls")
            return result

        if status in TERMINAL_STATUSES:
            # Terminal but not what we wanted (e.g. failed).
            log(f"  terminal status '{status}' reached instead")
            for txn in result.get("transactions") or []:
                if txn.get("error"):
                    log(f"  error: {json.dumps(txn['error'])}")
            return result

        if time.monotonic() > deadline:
            log(f"  TIMEOUT after {timeout}s at status '{status}'")
            log(f"  resume with: python spike/spike_checkout.py --session {session_id}")
            raise PravaError(f"timed out waiting for {sorted(until)}")

        interval = (
            POLL_FAST_SECONDS
            if time.monotonic() - started < POLL_BACKOFF_AFTER
            else POLL_SLOW_SECONDS
        )
        time.sleep(interval)


# --- step 5: report-status --------------------------------------------------


def report_status(base, secret, session_id, txn_ref_id):
    # Mock processor values. In the real system these come from the storefront's
    # mock processor after the executor submits the card form (SPEC.md §6).
    authorization_code = f"MOCK{uuid.uuid4().hex[:6].upper()}"
    payload = {
        "txn_ref_id": txn_ref_id,
        "txn_status": "APPROVED",
        "authorization_code": authorization_code,
        "response_code": "00",
    }
    log(f"POST /v1/sessions/{session_id}/report-status  APPROVED")
    log(f"     authorization_code={authorization_code} (mock)  response_code=00")
    body, response_id = request(
        "POST", f"{base}/v1/sessions/{session_id}/report-status", secret, json=payload
    )
    log(f"200 {json.dumps(redact(body))}  (X-Response-ID={response_id})")
    return body


# --- main -------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Prava sandbox seam spike")
    parser.add_argument(
        "--session",
        help="resume an existing session id instead of creating one (saves quota)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=POLL_TIMEOUT_SECONDS,
        help=(
            "seconds to wait for the human at the hosted page. Defaults to the "
            f"SPEC §5 product value ({POLL_TIMEOUT_SECONDS}s); raise it for an "
            "interactive spike run, where the 15-minute session is the real bound."
        ),
    )
    args = parser.parse_args()

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = load_env(os.path.join(repo_root, ".env"))
    base = env["PRAVA_BASE_URL"].rstrip("/")
    secret = env["PRAVA_SECRET_KEY"]  # sk_test_*; the pk_test_ key is unused in hosted mode

    health, _ = request("GET", f"{base}/health", secret)
    log(f"health: {json.dumps(health)}")

    # --- step 1 ---
    if args.session:
        session_id = args.session
        log(f"resuming session {session_id} (no new session created)")
    else:
        session = create_session(base, secret)
        session_id = session["session_id"]

        # --- step 2: hand off to the human ---
        print()
        print("=" * 78)
        print("OPEN THIS URL IN CHROME OR SAFARI AND COMPLETE THE PAYMENT:")
        print()
        print("  " + session["iframe_url"])
        print()
        print("  1. Enter a sandbox Visa test card (docs.prava.space/api-reference/test-cards)")
        print("  2. FIRST-TIME DEVICE ONLY: enter the issuer OTP, then register a passkey")
        print("     -- in that order. Expect a hand-off to the card network's own page.")
        print("  3. Returning device: just verify the existing passkey (no OTP).")
        print()
        print("  Session expires 15 minutes after creation. Polling starts now.")
        print("=" * 78)
        print()

    # --- step 3: poll to awaiting_result ---
    result = poll(
        base,
        secret,
        session_id,
        {"awaiting_result"},
        "waiting for the human",
        timeout=args.timeout,
    )

    # --- step 4: capture credentials, in memory only ---
    line_item = find_credential_line_item(result)
    if line_item is None:
        log("no credentials on any line item -- cannot continue")
        log(f"payment-result (redacted): {json.dumps(redact(result), indent=2)}")
        raise PravaError("expected token + dynamic_cvv while awaiting_result")

    txn_ref_id = line_item["txn_ref_id"]
    token = line_item["token"]  # NEVER printed, logged, or persisted
    dynamic_cvv = line_item["dynamic_cvv"]  # NEVER printed, logged, or persisted
    expiry_month = line_item.get("expiry_month")
    expiry_year = line_item.get("expiry_year")

    log("credentials received (in memory only):")
    log(f"  txn_ref_id    = {txn_ref_id}")
    log(f"  token         = **** **** **** {token[-4:]}   (last 4 only)")
    log(f"  dynamic_cvv   = *** (held in memory, {len(dynamic_cvv)} digits, never shown)")
    log(f"  expiry        = {expiry_month}/{expiry_year}")
    log(f"  total_amount  = {line_item.get('total_amount')}")

    # In the real system the executor would now fill the storefront card form.
    log("(no merchant checkout in this spike -- reporting a mock approval)")
    del token, dynamic_cvv

    # --- step 5: report-status ---
    report = report_status(base, secret, session_id, txn_ref_id)

    # --- step 6: poll to completed ---
    final = poll(base, secret, session_id, TERMINAL_STATUSES, "confirming completion")

    visa_confirmation = report.get("visa_confirmation")
    print()
    print("=" * 78)
    print(f"  final session status : {final.get('status')}")
    print(f"  txn_status           : {report.get('txn_status')}")
    print(f"  visa_confirmation    : {visa_confirmation}")
    print("=" * 78)

    if visa_confirmation == "SUCCESS" and final.get("status") == "completed":
        print("\nSPIKE GREEN -- the Prava seam is proven end to end.\n")
        return 0

    print("\nSPIKE NOT GREEN -- see the transitions above.\n")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PravaError as exc:
        log(f"FAILED: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        log("interrupted")
        sys.exit(130)
