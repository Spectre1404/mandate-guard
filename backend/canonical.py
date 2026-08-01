"""Canonical JSON + SHA-256 — the substrate the mandate hash and ledger chain sit on.

Canonical form: sorted keys, UTF-8, no insignificant whitespace. Two structures
that mean the same thing must produce byte-identical output, or the chain proves
nothing.

Floats are refused outright. A float in a hashed structure is both a money bug and
a determinism bug -- repr varies and equality is approximate.
"""

import hashlib
import json


def canonical_bytes(obj):
    """Deterministic UTF-8 bytes for a JSON-compatible structure."""
    _reject_floats(obj)
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_hash(obj):
    """SHA-256 hex of the canonical form."""
    return sha256_hex(canonical_bytes(obj))


def _reject_floats(node, path="$"):
    if isinstance(node, float):
        raise TypeError(
            f"float at {path}: money must be a 2dp decimal string, "
            "and floats are not deterministic under hashing"
        )
    if isinstance(node, dict):
        for key, value in node.items():
            if not isinstance(key, str):
                raise TypeError(f"non-string key at {path}: {key!r}")
            _reject_floats(value, f"{path}.{key}")
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            _reject_floats(value, f"{path}[{index}]")
