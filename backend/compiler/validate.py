"""Deterministic validator for the mandate compiler — SPEC §1, step 2.

An LLM produces a draft `constraints` object from natural language. This module
decides whether that draft is allowed to become a mandate. It is the fence: no
model output reaches the rest of the system without passing through here.

Design choices worth stating:
  * Every failing field is collected and reported together. One round trip should
    tell the user everything that is wrong.
  * Short decimals are padded (`14.5` -> `14.50`) because LLMs write them that way
    and padding is lossless. Over-precise money (`14.005`) is rejected rather than
    rounded -- rounding would invent a price the user never approved.
  * Unknown keys are rejected. A model that invents a field must not have it
    silently dropped, because a dropped field is an unenforced constraint.
"""

import re

from backend.normalize import lowercase_url_host, visa_safe_name

# From Prava's create-session reference (currency must be a supported code).
SUPPORTED_CURRENCIES = {
    "USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY", "SGD", "AED", "HKD", "MXN",
    "BRL", "CHF", "CNY", "NZD", "SEK", "NOK", "DKK", "ZAR", "THB", "KRW", "PLN",
    "TWD", "PHP", "IDR", "MYR", "CZK", "ILS", "CLP", "ARS", "COP", "PEN", "SAR",
    "QAR", "KWD", "BHD", "OMR", "EGP", "NGN", "KES", "GHS", "TZS", "UGX", "PKR",
    "BDT", "LKR", "VND", "MMK", "NPR",
}

CONSTRAINT_KEYS = {
    "merchant",
    "items",
    "price_ceiling_total",
    "currency",
    "effective_minutes",
    "substitution_policy",
}
MERCHANT_KEYS = {"name", "url", "country_code_iso2"}
ITEM_KEYS = {"product_id", "description", "max_unit_price", "quantity"}

PRODUCT_ID_MAX = 50  # Prava caps product_details[].product_id at 50 chars
DEFAULT_EFFECTIVE_MINUTES = 15
LOOSE_MONEY_RE = re.compile(r"^\d+(\.\d{1,2})?$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")


class ValidationError(ValueError):
    """Carries every failing field, not just the first."""

    def __init__(self, details):
        self.details = details
        super().__init__(f"mandate constraints failed validation: {sorted(details)}")


def validate_constraints(raw):
    """Validate and normalize a draft constraints object. Raises ValidationError."""
    details = {}

    if not isinstance(raw, dict):
        raise ValidationError({"constraints": "must be an object"})

    for key in sorted(set(raw) - CONSTRAINT_KEYS):
        details[key] = "unknown field"

    out = {
        "merchant": _merchant(raw.get("merchant"), details),
        "items": _items(raw.get("items"), details),
        "price_ceiling_total": _money(
            raw.get("price_ceiling_total"), "price_ceiling_total", details
        ),
        "currency": _currency(raw.get("currency"), details),
        "effective_minutes": _effective_minutes(raw.get("effective_minutes"), details),
        "substitution_policy": _substitution_policy(
            raw.get("substitution_policy"), details
        ),
    }

    if details:
        raise ValidationError(details)
    return out


def _merchant(merchant, details):
    if not isinstance(merchant, dict):
        details["merchant"] = "must be an object"
        return None

    for key in sorted(set(merchant) - MERCHANT_KEYS):
        details[f"merchant.{key}"] = "unknown field"

    name = merchant.get("name")
    if not isinstance(name, str) or not name.strip():
        details["merchant.name"] = "must be a non-empty string"
    elif not visa_safe_name(name):
        details["merchant.name"] = (
            "contains no Visa-safe characters, so it can never match a merchant"
        )

    url = merchant.get("url")
    normalized_url = None
    if not isinstance(url, str) or not url.strip():
        details["merchant.url"] = "must be a non-empty string"
    else:
        scheme = url.split("://", 1)[0].lower() if "://" in url else ""
        if scheme != "https":
            details["merchant.url"] = "must use https (Prava forwards the URL to Visa)"
        else:
            normalized_url = lowercase_url_host(url)

    country = merchant.get("country_code_iso2")
    normalized_country = None
    if not isinstance(country, str) or not COUNTRY_RE.match(country.upper()):
        details["merchant.country_code_iso2"] = "must be 2 ISO 3166-1 letters, e.g. US"
    else:
        normalized_country = country.upper()

    return {
        "name": name,
        "url": normalized_url,
        "country_code_iso2": normalized_country,
    }


def _items(items, details):
    if not isinstance(items, list) or not items:
        details["items"] = "must be a non-empty list"
        return []

    out = []
    for index, item in enumerate(items):
        prefix = f"items[{index}]"
        if not isinstance(item, dict):
            details[prefix] = "must be an object"
            continue

        for key in sorted(set(item) - ITEM_KEYS):
            details[f"{prefix}.{key}"] = "unknown field"

        product_id = item.get("product_id")
        if not isinstance(product_id, str) or not product_id.strip():
            details[f"{prefix}.product_id"] = "must be a non-empty string"
        elif len(product_id) > PRODUCT_ID_MAX:
            details[f"{prefix}.product_id"] = (
                f"must be at most {PRODUCT_ID_MAX} characters (Prava's cap)"
            )

        description = item.get("description")
        if not isinstance(description, str) or not description.strip():
            details[f"{prefix}.description"] = "must be a non-empty string"

        out.append(
            {
                "product_id": product_id,
                "description": description,
                "max_unit_price": _money(
                    item.get("max_unit_price"), f"{prefix}.max_unit_price", details
                ),
                "quantity": _quantity(item.get("quantity"), f"{prefix}.quantity", details),
            }
        )
    return out


def _money(value, field, details):
    """Pad 0-2dp strings to 2dp. Reject floats and anything more precise."""
    if isinstance(value, bool) or not isinstance(value, str):
        details[field] = "must be a decimal string, e.g. \"14.00\" (never a number)"
        return None
    if not LOOSE_MONEY_RE.match(value):
        details[field] = (
            "must be a non-negative decimal string with at most 2 decimal places"
        )
        return None
    whole, _, fraction = value.partition(".")
    return f"{whole}.{fraction.ljust(2, '0')}"


def _quantity(value, field, details):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        details[field] = "must be a positive integer"
        return None
    return value


def _currency(value, details):
    if not isinstance(value, str):
        details["currency"] = "must be a 3-letter ISO 4217 code"
        return None
    code = value.upper()
    if code not in SUPPORTED_CURRENCIES:
        details["currency"] = f"unsupported currency code: {value!r}"
        return None
    return code


def _effective_minutes(value, details):
    if value is None:
        return DEFAULT_EFFECTIVE_MINUTES
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        details["effective_minutes"] = "must be a positive integer"
        return None
    return value


def _substitution_policy(value, details):
    if value is None:
        return "none"
    if value != "none":
        details["substitution_policy"] = (
            "v1 supports only \"none\"; same_product_line is a v1.1 field"
        )
        return None
    return value
