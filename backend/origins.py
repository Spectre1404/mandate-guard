"""Declared origin mapping: canonical merchant URL -> the origin actually served.

Two constraints collide. Prava requires `merchant_details.url` to be **https**, and
it forwards that URL to Visa as the merchant of record — so the mandate has to
carry the canonical `https://beanline.example.com`. But the demo merchant is
self-hosted and served from something like `http://127.0.0.1:8200`. Executor
pre-check E2 compares the page it is standing on against the merchant the user
mandated, and those two strings will never match.

Rather than weaken E2 or lie in the mandate, the mapping is made explicit: a
merchant name maps to the origin that actually serves it. E2 then checks the
observed page host against the **declared** origin, and all three values —
canonical URL, declared origin, observed host — are written into the
EXECUTION_PRECHECK ledger event, so the substitution is disclosed in the evidence
itself rather than hidden in configuration.

With no mapping declared, E2 falls back to the canonical URL. A merchant that is
genuinely served where it claims to be needs no entry here.
"""

from backend.normalize import visa_safe_name


def normalize_key(merchant_name):
    """Key on the Visa-safe form so `H&M` and `HM` are the same merchant."""
    return visa_safe_name(merchant_name)


def build_origin_map(mapping=None):
    """{merchant name: serving origin} -> a lookup keyed by Visa-safe name."""
    return {normalize_key(name): origin for name, origin in (mapping or {}).items()}


def declared_origin(merchant_name, origin_map=None):
    """The origin this merchant is actually served from, or None if undeclared."""
    if not origin_map:
        return None
    return origin_map.get(normalize_key(merchant_name))
