"""Orchestrator: the whole flow, in one readable place — SPEC §0.

    mandate -> agent proposes -> GATE -> session -> passkey -> credentials
            -> executor pre-check -> checkout -> report-status -> consumed

Every stage writes a ledger event, so the chain is a complete account of what
happened and in what order. The orchestrator owns the entire Prava conversation:
the executor drives the page and proves what happened, and reporting the outcome
back to the network happens here.

The one rule that shapes everything: **a FAIL at the gate is terminal.** No
session is created, so no credential is ever minted, so there is nothing to
report and nothing to revoke. `GATE_BLOCKED` is the last event on that path. That
is the applause line in the demo and it has to be true in the code, not narrated
over it.

Reporting discipline after a token exists (Prava's docs are explicit): once
credentials have been issued and used, an outcome is ALWAYS reported. A pre-check
abort happens before any card entry, so no token was used and nothing is
reported; if a token was entered and checkout then failed, DECLINED is reported.
"""

from datetime import datetime, timezone

from backend.compiler.mandate import compute_mandate_hash
from backend.gate.verdict import evaluate
from backend.ledger.chain import append_event, new_ledger
from backend.executor.runner import ExecutionAborted


def _now_iso(dt=None):
    dt = dt or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class GateBlocked(RuntimeError):
    """The proposal failed verification. Terminal: no session was created.

    Carries the ledger, because a blocked purchase is exactly the case someone
    wants an evidence packet for -- the caller needs the chain, not just the reason.
    """

    def __init__(self, verdict, ledger=None):
        self.verdict = verdict
        self.ledger = ledger
        super().__init__(f"gate blocked the proposal: {verdict['failed_rule_ids']}")


class PurchaseResult(dict):
    """Plain dict; a class only so the outcome reads clearly at the call site."""

    @property
    def succeeded(self):
        return self.get("visa_confirmation") == "SUCCESS"


class Orchestrator:
    def __init__(self, client, executor_factory, clock=None, attempt_counter=None):
        """`executor_factory(ledger, mandate_id) -> CheckoutExecutor`.

        The executor is built late because it needs a live browser page, and the
        orchestrator should not own a browser. `attempt_counter(mandate_hash) -> int`
        supplies the per-mandate attempt number for the external_order_ref; in
        production it reads the DB, in tests it is a counter.
        """
        self.client = client
        self.executor_factory = executor_factory
        self.clock = clock or _now_iso
        self.attempt_counter = attempt_counter or (lambda _hash: 1)

    # --- ledger helper ------------------------------------------------------

    def _record(self, ledger, mandate, event_type, payload):
        return append_event(
            ledger, event_type, payload, ts=self.clock(), mandate_id=mandate["mandate_id"]
        )

    # --- stages -------------------------------------------------------------

    def open_ledger(self, mandate):
        """Genesis is anchored to the mandate hash (SPEC §4)."""
        ledger = new_ledger(mandate["mandate_hash"])
        self._record(
            ledger,
            mandate,
            "MANDATE_CREATED",
            {"mandate": mandate, "mandate_hash": mandate["mandate_hash"]},
        )
        self._record(
            ledger, mandate, "MANDATE_CONFIRMED", {"confirmed_at": mandate["confirmed_at"]}
        )
        return ledger

    def verify(self, ledger, mandate, proposal, now):
        """Run the gate. A FAIL ends the run here, before any Prava call."""
        self._record(ledger, mandate, "AGENT_PROPOSAL", {"proposal": proposal})

        verdict = evaluate(mandate, proposal, now=now)
        self._record(
            ledger,
            mandate,
            "GATE_VERDICT",
            {
                "verdict": verdict["verdict"],
                "results": verdict["results"],
                "failed_rule_ids": verdict["failed_rule_ids"],
                "proposal_id": proposal["proposal_id"],
            },
        )

        if verdict["verdict"] != "PASS":
            self._record(
                ledger,
                mandate,
                "GATE_BLOCKED",
                {
                    "failed_rule_ids": verdict["failed_rule_ids"],
                    "reason": "no payment session was created",
                },
            )
            raise GateBlocked(verdict, ledger)
        return verdict

    def open_session(self, ledger, mandate, proposal, verdict):
        attempt = self.attempt_counter(mandate["mandate_hash"])
        session = self.client.create_session(mandate, proposal, attempt, verdict=verdict)

        self._record(
            ledger,
            mandate,
            "SESSION_CREATED",
            {
                "session_id": session["session_id"],
                "order_id": session["order_id"],
                "expires_at": session["expires_at"],
                "external_order_ref": f"{mandate['mandate_hash']}.{attempt:02d}",
                "attempt": attempt,
                "purchase_context": [
                    {
                        "merchant_details": {
                            "name": mandate["constraints"]["merchant"]["name"],
                            "url": mandate["constraints"]["merchant"]["url"],
                            "country_code_iso2": mandate["constraints"]["merchant"][
                                "country_code_iso2"
                            ],
                        },
                        "product_details": proposal["line_items"],
                    }
                ],
            },
        )
        return session

    def await_credentials(self, ledger, mandate, session):
        """Poll to awaiting_result. Every transition is an event."""
        transitions = []

        def on_status(status):
            transitions.append(status)
            if status == "awaiting_result":
                # Reaching awaiting_result implies the passkey was approved.
                self._record(
                    ledger,
                    mandate,
                    "APPROVAL_OBSERVED",
                    {"session_id": session["session_id"], "status": status},
                )

        result = self.client.poll_until_credentials(session, on_status=on_status)
        credentials = self.client.extract_credentials(result)
        if credentials is None:
            raise RuntimeError("poll returned without credentials")

        self._record(
            ledger,
            mandate,
            "CREDENTIALS_RECEIVED",
            # Masked shape only: last 4 + expiry + txn_ref_id (SPEC §12).
            self.client.credentials_ledger_payload(credentials),
        )
        return credentials, transitions

    def report(self, ledger, mandate, session, credentials, confirmation, approved):
        report = self.client.report_status(
            session["session_id"],
            credentials["txn_ref_id"],
            "APPROVED" if approved else "DECLINED",
            authorization_code=confirmation.get("authorization_code") if approved else None,
            response_code=confirmation.get("response_code") if approved else "05",
        )
        self._record(
            ledger,
            mandate,
            "STATUS_REPORTED",
            {
                "txn_ref_id": report.get("txn_ref_id"),
                "txn_status": report.get("txn_status"),
                "visa_confirmation": report.get("visa_confirmation"),
                "authorization_code": confirmation.get("authorization_code"),
                "response_code": confirmation.get("response_code") if approved else "05",
            },
        )
        return report

    # --- the whole thing ----------------------------------------------------

    def run(self, mandate, proposal, now, cardholder_name):
        """Happy path end to end. Raises GateBlocked before any session exists."""
        if mandate.get("mandate_hash") != compute_mandate_hash(mandate):
            raise ValueError("mandate hash does not match its contents")

        ledger = self.open_ledger(mandate)
        verdict = self.verify(ledger, mandate, proposal, now)

        session = self.open_session(ledger, mandate, proposal, verdict)
        credentials, transitions = self.await_credentials(ledger, mandate, session)

        executor = self.executor_factory(ledger, mandate["mandate_id"])
        try:
            confirmation = executor.execute(
                mandate,
                proposal,
                credentials,
                session_total=proposal["proposed_total"],
                cardholder_name=cardholder_name,
            )
        except ExecutionAborted:
            # No card was entered, so no token was used: nothing to report.
            # The EXECUTION_PRECHECK failure is already in the ledger.
            raise
        except Exception:
            # A token WAS used and checkout failed -- Prava requires a report.
            self.report(ledger, mandate, session, credentials, {}, approved=False)
            raise

        report = self.report(ledger, mandate, session, credentials, confirmation, approved=True)

        approved = report.get("txn_status") == "APPROVED"
        if approved:
            # A one-time mandate is spent on APPROVED.
            self._record(
                ledger,
                mandate,
                "MANDATE_CONSUMED",
                {"session_id": session["session_id"], "order_number": confirmation["order_number"]},
            )

        final = self.client.poll_until_terminal(session)

        return PurchaseResult(
            ledger=ledger,
            session_id=session["session_id"],
            order_number=confirmation["order_number"],
            screenshot_path=confirmation["screenshot_path"],
            screenshot_sha256=confirmation["screenshot_sha256"],
            authorization_code=confirmation["authorization_code"],
            visa_confirmation=report.get("visa_confirmation"),
            final_status=final.get("status"),
            status_transitions=transitions,
            gate_verdict=verdict,
        )
