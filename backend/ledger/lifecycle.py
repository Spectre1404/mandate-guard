"""Mandate lifecycle as a pure projection of the ledger — SPEC §10 Tier 1.6.

The dashboard stores nothing. Every status and every field on it is derived from
ledger events on disk, so the view cannot disagree with the record it describes.
That is the product thesis applied to our own UI: if the evidence says a mandate
was consumed, the dashboard says consumed, and there is no second place for the
truth to live.

Two independent axes, deliberately kept separate because conflating them would
misrepresent how the product works:

  * **Mandate status** — ACTIVE / CONSUMED / EXPIRED. What the authorization is.
  * **Last attempt outcome** — COMPLETED / BLOCKED / DECLINED / NONE. What
    happened the last time an agent tried to use it.

A BLOCKED attempt leaves the mandate ACTIVE: the gate refused a specific cart, it
did not spend the user's authorization. A DECLINED attempt also leaves it ACTIVE,
because a one-time mandate is consumed only on APPROVED. Only an APPROVED charge
consumes it. Showing these as one field would hide exactly the behaviour that
makes the gate worth having.

`now` is always injected. EXPIRED is time-derived, and a clock read inside the
derivation would make it untestable.
"""

from datetime import timedelta

from backend.compiler.mandate import parse_iso
from backend.ledger.chain import verify_chain

ACTIVE = "ACTIVE"
CONSUMED = "CONSUMED"
EXPIRED = "EXPIRED"
# Defensive: our flows always confirm before proposing, but an unconfirmed mandate
# must not be reported as ACTIVE just because its window is open.
DRAFT = "DRAFT"

COMPLETED = "COMPLETED"
BLOCKED = "BLOCKED"
DECLINED = "DECLINED"
NONE = "NONE"


def _payloads(events, event_type):
    return [event["payload"] for event in events if event["type"] == event_type]


def _first(events, event_type):
    payloads = _payloads(events, event_type)
    return payloads[0] if payloads else None


def group_by_mandate(ledgers):
    """Group (ledger, path) pairs by mandate_hash.

    One mandate can span several ledger files -- a retry after a decline writes a
    new chain anchored to the same mandate hash. Each file keeps its own chain
    integrity; the projection reports them per-ledger rather than merging hashes.
    """
    groups = {}
    for ledger, path in ledgers:
        groups.setdefault(ledger["mandate_hash"], []).append((ledger, path))
    return groups


def derive_status(mandate, events, now):
    """CONSUMED > EXPIRED > ACTIVE, with DRAFT for an unconfirmed mandate."""
    if _payloads(events, "MANDATE_CONSUMED"):
        return CONSUMED
    # An explicit expiry event is authoritative over the clock.
    if _payloads(events, "MANDATE_EXPIRED"):
        return EXPIRED

    confirmed = bool(_payloads(events, "MANDATE_CONFIRMED")) or bool(
        (mandate or {}).get("confirmed_at")
    )
    if not confirmed:
        return DRAFT

    expiry = window_expires_at(mandate)
    if expiry is not None and now >= expiry:
        return EXPIRED
    return ACTIVE


def window_expires_at(mandate):
    """created_at + effective_minutes. Exclusive, matching gate rule R6."""
    if not mandate or not mandate.get("created_at"):
        return None
    minutes = ((mandate.get("constraints") or {}).get("effective_minutes"))
    if not isinstance(minutes, int):
        return None
    return parse_iso(mandate["created_at"]) + timedelta(minutes=minutes)


def derive_attempts(events):
    """One entry per payment session, in causal order, with its outcome."""
    attempts = []
    for event in events:
        if event["type"] == "SESSION_CREATED":
            payload = event["payload"]
            attempts.append(
                {
                    "attempt": payload.get("attempt"),
                    "external_order_ref": payload.get("external_order_ref"),
                    "session_id": payload.get("session_id"),
                    "created_at": event["ts"],
                    "outcome": NONE,
                    "order_number": None,
                    "visa_confirmation": None,
                }
            )
        elif event["type"] == "STATUS_REPORTED" and attempts:
            payload = event["payload"]
            approved = payload.get("txn_status") == "APPROVED"
            attempts[-1]["outcome"] = COMPLETED if approved else DECLINED
            attempts[-1]["visa_confirmation"] = payload.get("visa_confirmation")
        elif event["type"] == "CHECKOUT_EXECUTED" and attempts:
            attempts[-1]["order_number"] = event["payload"].get("order_number")
    return attempts


def derive_last_outcome(events, attempts):
    """What happened on the most recent try.

    A gate block is an outcome even though no session exists, so it is read from
    the event stream rather than from the attempt list.
    """
    for event in reversed(events):
        if event["type"] == "GATE_BLOCKED":
            return BLOCKED
        if event["type"] == "STATUS_REPORTED":
            return (
                COMPLETED
                if event["payload"].get("txn_status") == "APPROVED"
                else DECLINED
            )
    return attempts[-1]["outcome"] if attempts else NONE


def items_summary(mandate):
    items = ((mandate or {}).get("constraints") or {}).get("items") or []
    return ", ".join(f"{item['product_id']} ×{item['quantity']}" for item in items)


def project_mandate(mandate_hash, entries, now):
    """One dashboard row, derived entirely from the given ledgers."""
    # Causal order across all of this mandate's ledgers.
    events = sorted(
        (event for ledger, _ in entries for event in ledger["events"]),
        key=lambda event: event["ts"],
    )
    mandate = _first(events, "MANDATE_CREATED")
    mandate = (mandate or {}).get("mandate")

    attempts = derive_attempts(events)
    status = derive_status(mandate, events, now)
    constraints = (mandate or {}).get("constraints") or {}
    expiry = window_expires_at(mandate)

    return {
        "mandate_hash": mandate_hash,
        "hash_prefix": mandate_hash[:12],
        "mandate_id": (mandate or {}).get("mandate_id"),
        "intent_text": (mandate or {}).get("intent_text"),
        "user": (mandate or {}).get("user") or {},
        "merchant": constraints.get("merchant") or {},
        "items_summary": items_summary(mandate),
        "items": constraints.get("items") or [],
        "price_ceiling_total": constraints.get("price_ceiling_total"),
        "currency": constraints.get("currency"),
        "effective_minutes": constraints.get("effective_minutes"),
        "created_at": (mandate or {}).get("created_at"),
        "confirmed_at": (mandate or {}).get("confirmed_at"),
        "expires_at": expiry.isoformat().replace("+00:00", "Z") if expiry else None,
        "status": status,
        "last_outcome": derive_last_outcome(events, attempts),
        "attempt_count": len(attempts),
        "attempts": attempts,
        "events": events,
        "event_count": len(events),
        "ledgers": [
            {
                "path": path,
                "ledger_id": _ledger_id(path),
                "chain_valid": verify_chain(ledger)["valid"],
                "event_count": len(ledger["events"]),
            }
            for ledger, path in entries
        ],
    }


def _ledger_id(path):
    import os

    from backend.ledger.store import LEDGER_SUFFIX

    return os.path.basename(path or "").replace(LEDGER_SUFFIX, "") or None


def project_all(ledgers, now):
    """Dashboard rows, newest mandate first."""
    rows = [
        project_mandate(mandate_hash, entries, now)
        for mandate_hash, entries in group_by_mandate(ledgers).items()
    ]
    return sorted(rows, key=lambda row: row["created_at"] or "", reverse=True)


def summary_counts(rows):
    counts = {
        "total": len(rows),
        ACTIVE: 0,
        CONSUMED: 0,
        EXPIRED: 0,
        DRAFT: 0,
        "blocked_attempts": 0,
        "chain_broken": 0,
    }
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        if row["last_outcome"] == BLOCKED:
            counts["blocked_attempts"] += 1
        if any(not ledger["chain_valid"] for ledger in row["ledgers"]):
            counts["chain_broken"] += 1
    return counts
