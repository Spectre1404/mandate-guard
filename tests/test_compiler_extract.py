"""The fence around the LLM — SPEC §1 step 1.

Fully offline: every test injects a stub extractor. The point of these tests is
not that the model is clever, it is that a model returning something wrong,
inflated, or invented cannot become a mandate.
"""

from datetime import datetime, timezone

import pytest

from backend.compiler.extract import (
    CONSTRAINTS_SCHEMA,
    SYSTEM_PROMPT,
    ExtractionError,
    build_user_prompt,
    extract_constraints,
)
from backend.compiler.mandate import build_draft, confirm
from backend.compiler.validate import ValidationError, validate_constraints

CATALOG = [
    {"product_id": "BL-HOUSE-12", "name": "Beanline House Blend 12oz", "price": "12.50"},
    {"product_id": "BL-FILTER-100", "name": "Paper Filters 100ct", "price": "7.25"},
]
MERCHANT = {
    "name": "Beanline Coffee",
    "url": "https://beanline.example.com",
    "country_code_iso2": "US",
}
INTENT = "Buy a bag of house blend and two boxes of filters from Beanline, under $30."

WELL_FORMED = {
    "merchant": dict(MERCHANT),
    "items": [
        {
            "product_id": "BL-HOUSE-12",
            "description": "Beanline House Blend 12oz",
            "max_unit_price": "12.50",
            "quantity": 1,
        },
        {
            "product_id": "BL-FILTER-100",
            "description": "Paper Filters 100ct",
            "max_unit_price": "7.25",
            "quantity": 2,
        },
    ],
    "price_ceiling_total": "30.00",
    "currency": "USD",
    "effective_minutes": 15,
    "substitution_policy": "none",
}


def stub(payload, record=None):
    """An extractor that returns `payload`, optionally recording the prompts."""

    def _extractor(system, user):
        if record is not None:
            record.append({"system": system, "user": user})
        return payload

    return _extractor


# --- prompt construction ----------------------------------------------------


def test_the_request_reaches_the_model_verbatim():
    """A paraphrase here would mean the mandate describes a request nobody made."""
    prompt = build_user_prompt(INTENT, MERCHANT, CATALOG)

    assert INTENT in prompt


def test_prompt_carries_the_catalog_ids_the_model_must_copy():
    prompt = build_user_prompt(INTENT, MERCHANT, CATALOG)

    assert "BL-HOUSE-12" in prompt
    assert "BL-FILTER-100" in prompt
    assert "https://beanline.example.com" in prompt


def test_schema_is_closed_so_the_model_cannot_add_fields():
    assert CONSTRAINTS_SCHEMA["additionalProperties"] is False
    assert CONSTRAINTS_SCHEMA["properties"]["items"]["items"]["additionalProperties"] is False


def test_system_prompt_forbids_inventing_limits():
    assert "never invent" in SYSTEM_PROMPT.lower()


# --- extraction -------------------------------------------------------------


def test_well_formed_extraction_validates_into_constraints():
    draft = extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(WELL_FORMED))

    assert validate_constraints(draft) == WELL_FORMED


def test_extraction_returns_the_draft_unvalidated():
    """The fence lives at the call site, not inside extract_constraints."""
    junk = dict(WELL_FORMED, currency="XYZ")

    draft = extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(junk))

    assert draft["currency"] == "XYZ"  # extraction did not sanitize it
    with pytest.raises(ValidationError):
        validate_constraints(draft)  # the validator did


def test_empty_intent_is_refused_before_any_model_call():
    calls = []

    with pytest.raises(ExtractionError):
        extract_constraints("   ", MERCHANT, CATALOG, extractor=stub(WELL_FORMED, calls))

    assert calls == []


def test_non_object_model_output_is_refused():
    with pytest.raises(ExtractionError):
        extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(["not", "an", "object"]))


# --- the fence holds against bad model output -------------------------------


def test_a_hallucinated_product_id_cannot_become_a_mandate():
    """The model inventing an id it was never given is a substitution at source."""
    hallucinated = dict(WELL_FORMED)
    hallucinated["items"] = [dict(WELL_FORMED["items"][0], product_id="X" * 51)]

    draft = extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(hallucinated))

    with pytest.raises(ValidationError):
        validate_constraints(draft)


def test_a_model_returning_float_money_is_rejected():
    floated = dict(WELL_FORMED, price_ceiling_total=30.0)

    draft = extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(floated))

    with pytest.raises(ValidationError) as exc:
        validate_constraints(draft)
    assert "price_ceiling_total" in exc.value.details


def test_a_model_inventing_an_extra_field_is_rejected():
    invented = dict(WELL_FORMED, auto_approve_overages=True)

    draft = extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(invented))

    with pytest.raises(ValidationError) as exc:
        validate_constraints(draft)
    assert "auto_approve_overages" in exc.value.details


def test_a_model_downgrading_the_substitution_policy_is_rejected():
    loosened = dict(WELL_FORMED, substitution_policy="any")

    draft = extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(loosened))

    with pytest.raises(ValidationError):
        validate_constraints(draft)


def test_an_http_merchant_url_from_the_model_is_rejected():
    downgraded = dict(WELL_FORMED)
    downgraded["merchant"] = dict(MERCHANT, url="http://beanline.example.com")

    draft = extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(downgraded))

    with pytest.raises(ValidationError):
        validate_constraints(draft)


# --- end to end, still offline ----------------------------------------------


def test_extract_validate_confirm_produces_a_hashed_mandate():
    draft = extract_constraints(INTENT, MERCHANT, CATALOG, extractor=stub(WELL_FORMED))
    constraints = validate_constraints(draft)

    mandate = build_draft(
        user={"user_id": "u1", "user_email": "shiv@example.com"},
        intent_text=INTENT,
        constraints=constraints,
        created_at=datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    )
    confirmed = confirm(mandate, confirmed_at=datetime(2026, 8, 1, 12, 1, tzinfo=timezone.utc))

    assert confirmed["intent_text"] == INTENT
    assert len(confirmed["mandate_hash"]) == 64
    assert confirmed["constraints"]["items"][1]["quantity"] == 2
