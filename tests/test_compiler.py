"""Mandate compiler, deterministic half — SPEC §1.

The LLM proposes a draft `constraints` object; this validator is what decides
whether it becomes a mandate. Fenced Generation: nothing here calls a model, so
all of it is testable and none of it can be talked out of a rule.
"""

from datetime import datetime, timezone

import pytest

from backend.compiler.mandate import build_draft, confirm
from backend.compiler.validate import ValidationError, validate_constraints

RAW = {
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
        }
    ],
    "price_ceiling_total": "14.00",
    "currency": "USD",
    "effective_minutes": 15,
    "substitution_policy": "none",
}


def raw_with(**overrides):
    import copy

    draft = copy.deepcopy(RAW)
    draft.update(overrides)
    return draft


# --- happy path and normalization -------------------------------------------


def test_valid_constraints_pass_through_unchanged():
    assert validate_constraints(RAW) == RAW


def test_currency_is_uppercased():
    assert validate_constraints(raw_with(currency="usd"))["currency"] == "USD"


def test_merchant_host_is_lowercased():
    out = validate_constraints(
        raw_with(
            merchant={
                "name": "Beanline Coffee",
                "url": "https://WWW.Beanline.Example.COM/shop",
                "country_code_iso2": "US",
            }
        )
    )
    assert out["merchant"]["url"] == "https://www.beanline.example.com/shop"


@pytest.mark.parametrize("given,expected", [("14", "14.00"), ("14.5", "14.50"), ("0", "0.00")])
def test_short_decimals_are_padded_to_two_places(given, expected):
    """LLMs write '14.5'. Padding is safe; rounding would not be."""
    out = validate_constraints(raw_with(price_ceiling_total=given))
    assert out["price_ceiling_total"] == expected


def test_over_precise_money_is_rejected_not_rounded():
    """Silently rounding 14.005 would invent a price the user never approved."""
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(price_ceiling_total="14.005"))
    assert "price_ceiling_total" in exc.value.details


def test_effective_minutes_defaults_to_15_when_absent():
    draft = raw_with()
    del draft["effective_minutes"]
    assert validate_constraints(draft)["effective_minutes"] == 15


def test_substitution_policy_defaults_to_none_when_absent():
    draft = raw_with()
    del draft["substitution_policy"]
    assert validate_constraints(draft)["substitution_policy"] == "none"


# --- rejections -------------------------------------------------------------


def test_http_merchant_url_is_rejected():
    """Prava forwards the URL to Visa and requires https."""
    with pytest.raises(ValidationError) as exc:
        validate_constraints(
            raw_with(
                merchant={
                    "name": "Beanline Coffee",
                    "url": "http://beanline.example.com",
                    "country_code_iso2": "US",
                }
            )
        )
    assert "merchant.url" in exc.value.details


def test_unsupported_currency_is_rejected():
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(currency="XYZ"))
    assert "currency" in exc.value.details


def test_float_money_is_rejected():
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(price_ceiling_total=14.0))
    assert "price_ceiling_total" in exc.value.details


def test_empty_item_list_is_rejected():
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(items=[]))
    assert "items" in exc.value.details


def test_product_id_over_50_chars_is_rejected():
    """Prava caps product_id at 50 (SPEC §6)."""
    with pytest.raises(ValidationError) as exc:
        validate_constraints(
            raw_with(items=[dict(RAW["items"][0], product_id="X" * 51)])
        )
    assert "items[0].product_id" in exc.value.details


@pytest.mark.parametrize("quantity", [0, -1, 1.5, "1", None])
def test_non_positive_integer_quantity_is_rejected(quantity):
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(items=[dict(RAW["items"][0], quantity=quantity)]))
    assert "items[0].quantity" in exc.value.details


@pytest.mark.parametrize("minutes", [0, -5, "15", 1.5])
def test_non_positive_integer_effective_minutes_is_rejected(minutes):
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(effective_minutes=minutes))
    assert "effective_minutes" in exc.value.details


def test_bad_country_code_is_rejected():
    with pytest.raises(ValidationError) as exc:
        validate_constraints(
            raw_with(
                merchant={
                    "name": "Beanline Coffee",
                    "url": "https://beanline.example.com",
                    "country_code_iso2": "USA",
                }
            )
        )
    assert "merchant.country_code_iso2" in exc.value.details


def test_merchant_name_with_no_visa_safe_characters_is_rejected():
    """`***` sanitizes to empty, so it can never match anything downstream."""
    with pytest.raises(ValidationError) as exc:
        validate_constraints(
            raw_with(
                merchant={
                    "name": "***",
                    "url": "https://beanline.example.com",
                    "country_code_iso2": "US",
                }
            )
        )
    assert "merchant.name" in exc.value.details


def test_v1_rejects_substitution_policies_beyond_none():
    """SPEC §1: same_product_line is a v1.1 stretch field -- do not silently accept it."""
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(substitution_policy="same_product_line"))
    assert "substitution_policy" in exc.value.details


def test_all_failing_fields_are_reported_at_once():
    """One round trip should tell the user everything wrong, not the first thing."""
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(currency="XYZ", price_ceiling_total="1.005", items=[]))
    assert {"currency", "price_ceiling_total", "items"} <= set(exc.value.details)


def test_unknown_top_level_keys_are_rejected():
    """An LLM inventing a field must not have it silently ignored."""
    with pytest.raises(ValidationError) as exc:
        validate_constraints(raw_with(discount_policy="aggressive"))
    assert "discount_policy" in exc.value.details


# --- lifecycle --------------------------------------------------------------


def test_draft_has_no_hash_until_confirmed():
    draft = build_draft(
        user={"user_id": "u1", "user_email": "u@example.com"},
        intent_text="Buy one bag of house blend.",
        constraints=validate_constraints(RAW),
        created_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert draft["status"] == "draft"
    assert draft["confirmed_at"] is None
    assert draft["mandate_hash"] is None
    assert draft["version"] == "1.0"


def test_confirm_sets_status_timestamp_and_hash():
    draft = build_draft(
        user={"user_id": "u1", "user_email": "u@example.com"},
        intent_text="Buy one bag of house blend.",
        constraints=validate_constraints(RAW),
        created_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )

    confirmed = confirm(draft, confirmed_at=datetime(2026, 8, 1, 12, 1, tzinfo=timezone.utc))

    assert confirmed["status"] == "confirmed"
    assert confirmed["confirmed_at"] == "2026-08-01T12:01:00Z"
    assert len(confirmed["mandate_hash"]) == 64


def test_confirm_does_not_mutate_the_draft():
    draft = build_draft(
        user={"user_id": "u1", "user_email": "u@example.com"},
        intent_text="Buy one bag of house blend.",
        constraints=validate_constraints(RAW),
        created_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )

    confirm(draft, confirmed_at=datetime(2026, 8, 1, 12, 1, tzinfo=timezone.utc))

    assert draft["status"] == "draft"
    assert draft["mandate_hash"] is None


def test_confirming_twice_is_rejected():
    draft = build_draft(
        user={"user_id": "u1", "user_email": "u@example.com"},
        intent_text="Buy one bag of house blend.",
        constraints=validate_constraints(RAW),
        created_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )
    confirmed = confirm(draft, confirmed_at=datetime(2026, 8, 1, 12, 1, tzinfo=timezone.utc))

    with pytest.raises(ValueError):
        confirm(confirmed, confirmed_at=datetime(2026, 8, 1, 12, 2, tzinfo=timezone.utc))
