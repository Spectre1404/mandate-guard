"""Prava REST client — SPEC §5.

Written against the behavior the sandbox actually exhibited on Jul 31, not the
docs alone, and tested against `fake_prava` so routine iteration spends no
sandbox quota.

Four decisions that came out of the spike and are load-bearing here:

  * `external_order_ref` is `{mandate_hash}.{attempt:02d}`. Prava treats the ref
    as a permanent per-merchant idempotency key (undocumented 409), so a bare
    mandate_hash would allow exactly one session per mandate for all time --
    no retry after a failed passkey, and no Tier 2a declined-run.
  * The poller is bounded by the session's own `expires_at`, not a wall-clock
    timeout. Polling is quota-free, and a poller that quits before the session
    does manufactures false failures while the session is still payable.
  * Credentials are read from the NEWEST transaction. A retried session carries a
    dead transaction too, and its line item has no token.
  * Credential-readiness keys off token presence, never off a status field --
    line-item status disagreed with transaction status in the live sandbox.

The session payload is built ONLY from a gate-verified proposal. `build_session_payload`
will refuse a FAIL verdict outright rather than trusting the caller to have checked.
"""

import time
from datetime import datetime, timezone

DEFAULT_TIMEOUT = 30
POLL_FAST_SECONDS = 2
POLL_SLOW_SECONDS = 5
POLL_BACKOFF_AFTER = 30
EXPIRY_GRACE_SECONDS = 10
TERMINAL_STATUSES = {"completed", "failed"}
VALID_TXN_STATUSES = {"APPROVED", "DECLINED"}
RESPONSE_CODE_MAX = 2  # Prava caps response_code at 2 characters


class PravaError(RuntimeError):
    def __init__(self, message, code=None, status=None, details=None, response_id=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details or {}
        self.response_id = response_id

    @property
    def is_quota_exhausted(self):
        return self.code == "TRIES_EXHAUSTED"

    @property
    def is_duplicate_order_ref(self):
        return self.code == "DUPLICATE_EXTERNAL_ORDER_REF"


class PollTimeout(PravaError):
    """The session expired before credentials appeared."""


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def build_session_payload(mandate, proposal, attempt, require_verdict=None):
    """Map a gate-verified proposal onto Prava's create-session body.

    `require_verdict`, when given, is checked before anything is built. Pass the
    gate's verdict here so a FAIL can never reach the network.
    """
    if require_verdict is not None and require_verdict.get("verdict") != "PASS":
        raise PravaError(
            "refusing to build a session payload from a proposal that failed the gate: "
            f"{require_verdict.get('failed_rule_ids')}"
        )
    if not mandate.get("mandate_hash"):
        raise PravaError("mandate has no hash; only a confirmed mandate can create a session")

    merchant = mandate["constraints"]["merchant"]
    return {
        "user_id": mandate["user"]["user_id"],
        "user_email": mandate["user"]["user_email"],
        # The verified total becomes the authorized amount cap.
        "total_amount": proposal["proposed_total"],
        "currency": mandate["constraints"]["currency"],
        "integration_type": "full_checkout",
        # 64 hex + '.' + 2 digits = 67 chars, well inside the 255 limit.
        "external_order_ref": f"{mandate['mandate_hash']}.{attempt:02d}",
        # callback_url deliberately omitted -- optional for all integration types,
        # and we poll rather than take a redirect.
        "purchase_context": [
            {
                "merchant_details": {
                    "name": merchant["name"],
                    "url": merchant["url"],
                    "country_code_iso2": merchant["country_code_iso2"],
                },
                "product_details": [
                    {
                        "description": line["description"],
                        "unit_price": line["unit_price"],
                        "product_id": line["product_id"],
                        "quantity": line["quantity"],
                    }
                    for line in proposal["line_items"]
                ],
                "effective_until_minutes": mandate["constraints"]["effective_minutes"],
            }
        ],
    }


class PravaClient:
    def __init__(self, base_url, secret_key, session=None, sleep=time.sleep, timeout=None):
        """`session` accepts any requests-compatible object.

        A FastAPI TestClient satisfies that interface, which is how the whole
        suite runs against fake_prava with no network and no quota. TestClient
        rejects a `timeout` kwarg, so it defaults on only for a real session.
        """
        self.base_url = (base_url or "").rstrip("/")
        self.secret_key = secret_key
        self.sleep = sleep
        if session is None:
            import requests

            session = requests.Session()
            timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        self.session = session
        self.timeout = timeout

    # --- plumbing -----------------------------------------------------------

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.secret_key:
            headers["Authorization"] = f"Bearer {self.secret_key}"
        return headers

    def _request(self, method, path, **kwargs):
        if self.timeout is not None:
            kwargs.setdefault("timeout", self.timeout)
        response = self.session.request(
            method, f"{self.base_url}{path}", headers=self._headers(), **kwargs
        )
        response_id = response.headers.get("X-Response-ID")
        try:
            body = response.json()
        except ValueError:
            raise PravaError(
                f"non-JSON response ({response.status_code})",
                status=response.status_code,
                response_id=response_id,
            )

        if response.status_code >= 400:
            error = body.get("error", {}) if isinstance(body, dict) else {}
            raise PravaError(
                error.get("message", f"HTTP {response.status_code}"),
                code=error.get("code"),
                status=response.status_code,
                details=error.get("details"),
                response_id=response_id,
            )
        return body

    # --- endpoints ----------------------------------------------------------

    def health(self):
        """Quota-free connectivity check. Cheaper than proving reachability with a session."""
        return self._request("GET", "/health")

    def create_session(self, mandate, proposal, attempt, verdict=None):
        payload = build_session_payload(mandate, proposal, attempt, require_verdict=verdict)
        return self._request("POST", "/v1/sessions", json=payload)

    def get_payment_result(self, session_id):
        return self._request("GET", f"/v1/sessions/{session_id}/payment-result")

    def report_status(
        self,
        session_id,
        txn_ref_id,
        txn_status,
        authorization_code=None,
        response_code=None,
    ):
        if txn_status not in VALID_TXN_STATUSES:
            raise PravaError(f"txn_status must be APPROVED or DECLINED, got {txn_status!r}")
        if response_code is not None and len(response_code) > RESPONSE_CODE_MAX:
            raise PravaError(
                f"response_code is capped at {RESPONSE_CODE_MAX} characters, got {response_code!r}"
            )

        payload = {"txn_ref_id": txn_ref_id, "txn_status": txn_status}
        if authorization_code:
            payload["authorization_code"] = authorization_code
        if response_code:
            payload["response_code"] = response_code
        return self._request("POST", f"/v1/sessions/{session_id}/report-status", json=payload)

    # --- polling ------------------------------------------------------------

    def _poll(self, session, done, on_status=None, now=None):
        session_id = session["session_id"]
        deadline = parse_iso(session["expires_at"]).timestamp() + EXPIRY_GRACE_SECONDS
        clock = now or (lambda: datetime.now(timezone.utc).timestamp())
        started = clock()
        last_status = None

        while True:
            result = self.get_payment_result(session_id)
            status = result.get("status")

            if status != last_status:
                # Every transition is a ledger-worthy event (SPEC §4).
                if on_status:
                    on_status(status)
                last_status = status

            if done(result, status):
                return result

            if clock() > deadline:
                raise PollTimeout(
                    f"session {session_id} expired at {session['expires_at']} "
                    f"while status was {status!r}",
                    code="SESSION_EXPIRED",
                )

            elapsed = clock() - started
            self.sleep(POLL_FAST_SECONDS if elapsed < POLL_BACKOFF_AFTER else POLL_SLOW_SECONDS)

    def poll_until_credentials(self, session, on_status=None, now=None):
        """Poll until credentials exist, bounded by the session's own expires_at."""
        return self._poll(
            session,
            done=lambda result, status: self.extract_credentials(result) is not None,
            on_status=on_status,
            now=now,
        )

    def poll_until_terminal(self, session, on_status=None, now=None):
        """Poll until completed/failed. Used after report-status to close the loop."""
        return self._poll(
            session,
            done=lambda result, status: status in TERMINAL_STATUSES,
            on_status=on_status,
            now=now,
        )

    # --- credentials --------------------------------------------------------

    @staticmethod
    def extract_credentials(result):
        """Credentials from the NEWEST transaction, or None.

        txn_id is a ULID, so lexicographic descending is newest-first. Readiness is
        determined by token presence, never by a status field: the live sandbox
        returned a `failed` transaction holding a `pending` line item.

        The returned dict carries the full token under `_token` and the CVV under
        `_dynamic_cvv`. The underscore is the convention that says: memory only,
        never persisted, never logged. Use `credentials_ledger_payload` for
        anything that gets written down.
        """
        transactions = sorted(
            result.get("transactions") or [],
            key=lambda txn: txn.get("txn_id") or "",
            reverse=True,
        )
        for txn in transactions:
            for item in txn.get("line_items") or []:
                if item.get("token") and item.get("dynamic_cvv"):
                    return {
                        "txn_ref_id": item["txn_ref_id"],
                        "_token": item["token"],
                        "_dynamic_cvv": item["dynamic_cvv"],
                        "expiry_month": item.get("expiry_month"),
                        "expiry_year": item.get("expiry_year"),
                        "token_last4": item["token"][-4:],
                        "total_amount": item.get("total_amount"),
                    }
        return None

    @staticmethod
    def credentials_ledger_payload(credentials):
        """The only credential shape allowed to be written down (SPEC §12)."""
        return {
            "txn_ref_id": credentials["txn_ref_id"],
            "token_last4": credentials["token_last4"],
            "expiry_month": credentials["expiry_month"],
            "expiry_year": credentials["expiry_year"],
        }
