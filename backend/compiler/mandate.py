"""Mandate construction, hashing, and lifecycle — SPEC §1.

The hash covers `{user, intent_text, constraints, created_at}` and deliberately
excludes the mutable lifecycle fields (`status`, `confirmed_at`, `mandate_hash`
itself, and the identifiers). Confirming or consuming a mandate must not move its
hash, because the hash is the genesis link of the evidence chain and the
`external_order_ref` prefix Prava stores.
"""

import uuid
from datetime import datetime, timezone

from backend.canonical import canonical_hash

MANDATE_VERSION = "1.0"

# Sorted, and asserted in tests -- this tuple is the definition of what is signed.
HASHED_FIELDS = ("constraints", "created_at", "intent_text", "user")

LIVE_STATUSES = ("draft", "confirmed")
DEAD_STATUSES = ("consumed", "expired", "revoked")


def iso_utc(dt):
    """ISO-8601 UTC with a trailing Z, the form used everywhere in the ledger."""
    if dt.tzinfo is None:
        raise ValueError("refusing to guess a timezone for a naive datetime")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def compute_mandate_hash(mandate):
    """SHA-256 over the canonical JSON of the signed subset."""
    return canonical_hash({field: mandate[field] for field in HASHED_FIELDS})


def build_draft(user, intent_text, constraints, created_at, mandate_id=None):
    """A draft mandate: no hash yet, because the user has not confirmed it yet."""
    return {
        "mandate_id": mandate_id or str(uuid.uuid4()),
        "version": MANDATE_VERSION,
        "created_at": iso_utc(created_at),
        "user": dict(user),
        "intent_text": intent_text,
        "constraints": constraints,
        "status": "draft",
        "confirmed_at": None,
        "mandate_hash": None,
    }


def confirm(mandate, confirmed_at):
    """User confirmation: freeze the mandate and compute its hash. Returns a new dict."""
    if mandate["status"] != "draft":
        raise ValueError(
            f"only a draft mandate can be confirmed, this one is {mandate['status']!r}"
        )

    confirmed = dict(mandate)
    confirmed["status"] = "confirmed"
    confirmed["confirmed_at"] = iso_utc(confirmed_at)
    confirmed["mandate_hash"] = compute_mandate_hash(confirmed)
    return confirmed


def expires_at(mandate):
    """The instant the mandate stops being valid (exclusive)."""
    from datetime import timedelta

    return parse_iso(mandate["created_at"]) + timedelta(
        minutes=mandate["constraints"]["effective_minutes"]
    )
