"""Shared fixtures: a confirmed mandate and the proposal that should pass the gate.

Every drift case in test_gate_rules.py is expressed as a mutation of these two,
so a test reads as "the happy pair, except <one thing>".
"""

import copy
from datetime import datetime, timedelta, timezone

import pytest

MANDATE_ID = "11111111-1111-4111-8111-111111111111"
PROPOSAL_ID = "22222222-2222-4222-8222-222222222222"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@pytest.fixture
def now():
    return datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def mandate(now):
    """A confirmed, in-window, unconsumed mandate for two products."""
    return {
        "mandate_id": MANDATE_ID,
        "version": "1.0",
        "created_at": iso(now - timedelta(minutes=1)),
        "user": {"user_id": "user_001", "user_email": "shiv@example.com"},
        "intent_text": "Buy me a bag of the house blend and a box of filters from Beanline, under $30 total.",
        "constraints": {
            "merchant": {
                "name": "Beanline Coffee",
                "url": "https://beanline.example.com",
                "country_code_iso2": "US",
            },
            "items": [
                {
                    "product_id": "BL-HOUSE-12",
                    "description": "Beanline House Blend 12oz",
                    "max_unit_price": "14.00",
                    "quantity": 1,
                },
                {
                    "product_id": "BL-FILTER-100",
                    "description": "Paper Filters 100ct",
                    "max_unit_price": "8.00",
                    "quantity": 2,
                },
            ],
            "price_ceiling_total": "30.00",
            "currency": "USD",
            "effective_minutes": 15,
            "substitution_policy": "none",
        },
        "status": "confirmed",
        "confirmed_at": iso(now - timedelta(minutes=1)),
        "mandate_hash": "0" * 64,
    }


@pytest.fixture
def proposal(now):
    """Matches the mandate exactly: 12.50 + (2 x 7.25) = 27.00, under the 30.00 ceiling."""
    return {
        "proposal_id": PROPOSAL_ID,
        "mandate_id": MANDATE_ID,
        "created_at": iso(now),
        "merchant": {"name": "Beanline Coffee", "url": "https://beanline.example.com"},
        "line_items": [
            {
                "product_id": "BL-HOUSE-12",
                "description": "Beanline House Blend 12oz",
                "unit_price": "12.50",
                "quantity": 1,
            },
            {
                "product_id": "BL-FILTER-100",
                "description": "Paper Filters 100ct",
                "unit_price": "7.25",
                "quantity": 2,
            },
        ],
        "proposed_total": "27.00",
        "agent_meta": {"model": "test-model", "rationale": "Cheapest in-policy options."},
    }


@pytest.fixture
def mutate():
    """Return a deep-copied object with one edit applied, leaving fixtures pristine."""

    def _mutate(obj, fn):
        clone = copy.deepcopy(obj)
        fn(clone)
        return clone

    return _mutate
