"""Canonical JSON and the mandate hash — SPEC §1.

mandate_hash = SHA-256 over the canonical JSON of {user, intent_text, constraints,
created_at}: sorted keys, UTF-8, no insignificant whitespace. Mutable lifecycle
fields are excluded, so confirming a mandate must not change its hash.
"""

import json

from backend.canonical import canonical_bytes, sha256_hex
from backend.compiler.mandate import HASHED_FIELDS, compute_mandate_hash


# --- canonical form ---------------------------------------------------------


def test_canonical_form_sorts_keys_and_strips_insignificant_whitespace():
    assert canonical_bytes({"b": 1, "a": 2}) == b'{"a":2,"b":1}'


def test_key_order_does_not_change_the_bytes():
    assert canonical_bytes({"a": 1, "b": {"c": 2, "d": 3}}) == canonical_bytes(
        {"b": {"d": 3, "c": 2}, "a": 1}
    )


def test_canonical_form_is_utf8_and_does_not_escape_non_ascii():
    assert canonical_bytes({"name": "Café"}) == '{"name":"Café"}'.encode("utf-8")


def test_canonical_form_preserves_list_order():
    """Key order is insignificant; list order is data."""
    assert canonical_bytes({"x": [1, 2]}) != canonical_bytes({"x": [2, 1]})


def test_canonical_bytes_rejects_floats():
    """Floats must never enter a hashed structure -- money is decimal strings."""
    import pytest

    with pytest.raises(TypeError):
        canonical_bytes({"price": 12.5})


def test_sha256_hex_is_64_lowercase_hex():
    digest = sha256_hex(b"anything")
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)  # raises if not hex


# --- mandate hash -----------------------------------------------------------


def test_mandate_hash_covers_exactly_the_documented_fields():
    assert HASHED_FIELDS == ("constraints", "created_at", "intent_text", "user")


def test_mandate_hash_is_stable_across_repeated_computation(mandate):
    assert compute_mandate_hash(mandate) == compute_mandate_hash(mandate)


def test_mandate_hash_ignores_mutable_lifecycle_fields(mandate):
    """Confirming or consuming a mandate must not move its hash -- the chain depends on it."""
    before = compute_mandate_hash(mandate)

    for field, value in [
        ("status", "consumed"),
        ("confirmed_at", "2026-08-01T12:34:56Z"),
        ("mandate_hash", "f" * 64),
        ("mandate_id", "99999999-9999-4999-8999-999999999999"),
        ("version", "9.9"),
    ]:
        moved = dict(mandate)
        moved[field] = value
        assert compute_mandate_hash(moved) == before, f"{field} must not affect the hash"


def test_mandate_hash_changes_when_any_constraint_changes(mandate):
    before = compute_mandate_hash(mandate)

    ceiling = json.loads(json.dumps(mandate))
    ceiling["constraints"]["price_ceiling_total"] = "31.00"
    assert compute_mandate_hash(ceiling) != before

    price = json.loads(json.dumps(mandate))
    price["constraints"]["items"][0]["max_unit_price"] = "14.01"
    assert compute_mandate_hash(price) != before

    merchant = json.loads(json.dumps(mandate))
    merchant["constraints"]["merchant"]["url"] = "https://rival.example.com"
    assert compute_mandate_hash(merchant) != before


def test_mandate_hash_changes_when_intent_or_user_changes(mandate):
    before = compute_mandate_hash(mandate)

    intent = json.loads(json.dumps(mandate))
    intent["intent_text"] = mandate["intent_text"] + " Also a mug."
    assert compute_mandate_hash(intent) != before

    user = json.loads(json.dumps(mandate))
    user["user"]["user_email"] = "someone.else@example.com"
    assert compute_mandate_hash(user) != before


def test_mandate_hash_fits_pravas_external_order_ref_limit(mandate):
    """SPEC §5: {mandate_hash}.{attempt:02d} must fit the 255-char cap."""
    digest = compute_mandate_hash(mandate)
    assert len(digest) == 64
    assert len(f"{digest}.{1:02d}") <= 255
