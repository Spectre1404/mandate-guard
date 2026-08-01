"""Evidence ledger: append-only, hash-chained — SPEC §4.

    event_hash = sha256(canonical(event minus event_hash) + prev_hash)

Genesis `prev_hash` is the mandate_hash, anchoring the chain to the exact thing the
user confirmed. Re-anchoring the chain to a different mandate is therefore
detectable, which matters: a chain that verifies internally but describes a
different authorization is not evidence about this purchase.

Storage-agnostic on purpose. A ledger is a plain dict (`{mandate_hash, events}`),
so the same chain logic backs an in-memory test, a JSONL file, and a Postgres
table without the hashing rules being reimplemented per store.

Two rules are enforced here rather than trusted to callers, because both are hard
requirements that would be silent failures:
  * the event taxonomy is closed — a typo'd type cannot enter the record
  * credential fields are refused outright — SPEC §12 says the full token and
    dynamic CVV never reach the ledger, so the ledger itself rejects them
"""

import uuid

from backend.canonical import canonical_bytes, sha256_hex

# SPEC §4, in causal order.
EVENT_TYPES = (
    "MANDATE_CREATED",
    "MANDATE_CONFIRMED",
    "AGENT_PROPOSAL",
    "GATE_VERDICT",
    "GATE_BLOCKED",
    "SESSION_CREATED",
    "APPROVAL_OBSERVED",
    "CREDENTIALS_RECEIVED",
    "EXECUTION_PRECHECK",
    "CHECKOUT_EXECUTED",
    "STATUS_REPORTED",
    "MANDATE_CONSUMED",
    "MANDATE_EXPIRED",
)

# Field names that must never appear in a payload, at any depth.
FORBIDDEN_PAYLOAD_KEYS = {"token", "dynamic_cvv", "cvv", "pan", "card_number", "session_token"}

HASHED_EVENT_FIELDS = ("event_id", "mandate_id", "payload", "prev_hash", "ts", "type")


class LedgerError(ValueError):
    """An append that would corrupt the evidence record."""


def new_ledger(mandate_hash):
    return {"mandate_hash": mandate_hash, "events": []}


def compute_event_hash(event, prev_hash):
    """sha256(canonical(event minus event_hash) + prev_hash)."""
    body = {field: event[field] for field in HASHED_EVENT_FIELDS}
    return sha256_hex(canonical_bytes(body) + prev_hash.encode("utf-8"))


def append_event(ledger, event_type, payload, ts, mandate_id=None, event_id=None):
    """Append one event and return it. The only way to write to a ledger."""
    if event_type not in EVENT_TYPES:
        raise LedgerError(f"unknown event type: {event_type!r}")

    payload = _validated_payload(payload)

    prev_hash = (
        ledger["events"][-1]["event_hash"] if ledger["events"] else ledger["mandate_hash"]
    )
    event = {
        "event_id": event_id or str(uuid.uuid4()),
        "ts": ts,
        "mandate_id": mandate_id,
        "type": event_type,
        "payload": payload,
        "prev_hash": prev_hash,
    }
    event["event_hash"] = compute_event_hash(event, prev_hash)
    ledger["events"].append(event)
    return event


def verify_chain(ledger):
    """Recompute every link. Returns per-link results so the UI can render green/red."""
    links = []
    valid = True
    expected_prev = ledger["mandate_hash"]

    for index, event in enumerate(ledger["events"]):
        reason = None

        if event["prev_hash"] != expected_prev:
            reason = (
                "genesis prev_hash is not the mandate_hash"
                if index == 0
                else "prev_hash does not match the preceding event"
            )
        elif compute_event_hash(event, event["prev_hash"]) != event["event_hash"]:
            reason = "event_hash mismatch"

        link_valid = reason is None
        valid = valid and link_valid
        links.append(
            {
                "index": index,
                "event_id": event["event_id"],
                "type": event["type"],
                "valid": link_valid,
                "reason": reason,
            }
        )
        expected_prev = event["event_hash"]

    return {"valid": valid, "links": links}


def _validated_payload(payload):
    """Deep-copy via the canonical encoder, which also rejects floats.

    Copying matters: the caller must not be able to mutate a payload after it has
    been hashed, or the ledger would disagree with its own hashes.
    """
    import json

    _reject_forbidden_keys(payload)
    try:
        return json.loads(canonical_bytes(payload).decode("utf-8"))
    except TypeError as exc:
        raise LedgerError(str(exc)) from exc


def _reject_forbidden_keys(node, path="payload"):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FORBIDDEN_PAYLOAD_KEYS:
                raise LedgerError(
                    f"{path}.{key} is a credential field and must never be persisted "
                    "(SPEC §12: last 4 + expiry + txn_ref_id only)"
                )
            _reject_forbidden_keys(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _reject_forbidden_keys(value, f"{path}[{index}]")
