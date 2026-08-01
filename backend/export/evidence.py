"""Evidence packet assembly — SPEC §8.

Turns a hash-chained ledger into the artifact a finance team, a merchant, or an
issuer reads when an agent purchase is contested. Pure and deterministic: this
module reads the chain and rearranges it. It never recomputes a fact, never
infers one, and never fills a gap — a missing stage is reported as missing.

The narrative is the single LLM contribution and it is additive commentary on the
deterministic record below it, always labeled as such.

On framing: fields are annotated as *mapped to* the categories issuers evaluate
under frameworks like Visa CE 3.0. This is not a CE 3.0 packet and must never be
described as one — the annotation says what the field corresponds to, nothing more.
"""

import base64
import os

from backend.ledger.chain import verify_chain

# What each section corresponds to in issuer-style dispute evidence. Deliberately
# worded as a mapping, never as a claim of compliance.
EVIDENCE_CATEGORY = {
    "mandate": "Cardholder authorization scope (mapped to the evidence categories "
    "issuers evaluate under frameworks like Visa CE 3.0)",
    "proposal": "Transaction as proposed by the agent",
    "gate": "Pre-authorization verification result",
    "session": "Payment authorization scope registered with the network",
    "credentials": "Credential issuance record (masked)",
    "precheck": "Point-of-sale re-verification immediately before card entry",
    "checkout": "Proof of completed purchase (mapped to the evidence categories "
    "issuers evaluate under frameworks like Visa CE 3.0)",
    "report": "Outcome reported to the card network",
    "chain": "Tamper-evidence for the record above",
}

NARRATIVE_LABEL = "Narrative (AI-generated summary of the deterministic record below)"
MISSING = None


def _first(ledger, event_type):
    for event in ledger["events"]:
        if event["type"] == event_type:
            return event
    return None


def _payload(ledger, event_type):
    event = _first(ledger, event_type)
    return event["payload"] if event else MISSING


def embed_screenshot(path):
    """Inline the confirmation screenshot as a data URI so the PDF is self-contained.

    Returns None when the file is absent rather than inventing a placeholder: a
    missing proof is a fact about the record.
    """
    if not path or not os.path.exists(path):
        return None
    with open(path, "rb") as handle:
        return "data:image/png;base64," + base64.b64encode(handle.read()).decode("ascii")


def build_evidence(ledger, narrative=None):
    """Assemble the packet. Every value is copied from the chain, never derived."""
    mandate_event = _payload(ledger, "MANDATE_CREATED")
    mandate = (mandate_event or {}).get("mandate")
    proposal = (_payload(ledger, "AGENT_PROPOSAL") or {}).get("proposal")
    gate = _payload(ledger, "GATE_VERDICT")
    blocked = _payload(ledger, "GATE_BLOCKED")
    session = _payload(ledger, "SESSION_CREATED")
    credentials = _payload(ledger, "CREDENTIALS_RECEIVED")
    precheck = _payload(ledger, "EXECUTION_PRECHECK")
    checkout = _payload(ledger, "CHECKOUT_EXECUTED")
    report = _payload(ledger, "STATUS_REPORTED")
    consumed = _payload(ledger, "MANDATE_CONSUMED")

    chain = verify_chain(ledger)

    outcome = "BLOCKED_AT_GATE" if blocked else (
        "COMPLETED" if (report or {}).get("visa_confirmation") == "SUCCESS" else "INCOMPLETE"
    )

    return {
        "packet_version": "1.0",
        "outcome": outcome,
        "cover": {
            "mandate_id": (mandate or {}).get("mandate_id"),
            "mandate_hash": ledger["mandate_hash"],
            "intent_text": (mandate or {}).get("intent_text"),
            "user": (mandate or {}).get("user"),
            "merchant": ((mandate or {}).get("constraints") or {}).get("merchant"),
            "currency": ((mandate or {}).get("constraints") or {}).get("currency"),
            "amount_authorized": (session or {}).get("purchase_context", [{}])[0].get(
                "product_details"
            )
            and (proposal or {}).get("proposed_total"),
            "order_number": (checkout or {}).get("order_number"),
            "visa_confirmation": (report or {}).get("visa_confirmation"),
            "event_count": len(ledger["events"]),
            "chain_valid": chain["valid"],
        },
        "mandate": mandate,
        "proposal": proposal,
        "gate": gate,
        "gate_blocked": blocked,
        "session": session,
        "credentials": credentials,
        "precheck": precheck,
        "checkout": checkout,
        "report": report,
        "consumed": consumed,
        "chain": chain,
        "events": ledger["events"],
        "narrative": narrative,
        "narrative_label": NARRATIVE_LABEL,
        "categories": EVIDENCE_CATEGORY,
    }


FALLBACK_NARRATIVE = (
    "No AI-generated narrative was produced for this packet. The deterministic "
    "record below is complete without it."
)


def write_narrative(evidence, writer):
    """Ask the model for the plain-English summary. Never load-bearing.

    The narrative describes a record that already exists. If the model is
    unavailable the packet is still complete, so a failure degrades to a fixed
    sentence rather than an error.
    """
    if writer is None:
        return FALLBACK_NARRATIVE
    try:
        text = writer(evidence)
    except Exception:
        return FALLBACK_NARRATIVE
    return (text or "").strip() or FALLBACK_NARRATIVE


class OpenAINarrativeWriter:
    """The one LLM call in the export."""

    SYSTEM = (
        "You write a short, plain-English summary of a payment authorization "
        "record for a finance or dispute analyst. Three short paragraphs at most. "
        "State only what the record shows. Do not speculate about intent, do not "
        "add reassurance, and do not assert that anything is compliant, verified, "
        "or legally sufficient -- you are summarizing evidence, not certifying it. "
        "If the record shows the purchase was blocked, say so plainly."
    )

    def __init__(self, api_key=None, model="gpt-5-mini", client=None):
        self.model = model
        self._client = client
        self._api_key = api_key

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI

            from backend.config import require

            self._client = OpenAI(api_key=self._api_key or require("openai_api_key"))
        return self._client

    def __call__(self, evidence):
        cover = evidence["cover"]
        gate = evidence.get("gate") or {}
        summary = {
            "outcome": evidence["outcome"],
            "request": cover.get("intent_text"),
            "merchant": (cover.get("merchant") or {}).get("name"),
            "currency": cover.get("currency"),
            "gate_verdict": gate.get("verdict"),
            "failed_rules": gate.get("failed_rule_ids"),
            "order_number": cover.get("order_number"),
            "visa_confirmation": cover.get("visa_confirmation"),
            "chain_valid": cover.get("chain_valid"),
            "event_count": cover.get("event_count"),
        }
        import json

        response = self._get_client().chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": self.SYSTEM},
                {"role": "user", "content": json.dumps(summary, indent=2)},
            ],
        )
        return response.choices[0].message.content
