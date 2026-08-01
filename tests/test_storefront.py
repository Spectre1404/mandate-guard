"""Beanline storefront and its mock processor — SPEC §6.

Covers the processor's card rules, the order/confirmation path the executor
screenshots, the element ids the executor depends on, and the drift toggle --
including the property that matters most for honesty: drift changes the *store*,
so the resulting cart genuinely differs, rather than any verdict being faked.
"""

import pytest
from fastapi.testclient import TestClient

from storefront import catalog as catalog_module
from storefront.app import app
from storefront.processor import APPROVED, DECLINED, authorize, luhn_ok

def _synthetic_visa():
    """A Luhn-valid Visa-prefixed number generated at runtime.

    Deliberately NOT a literal: sandbox test card numbers must never appear in
    this repository (SPEC §12), and a hardcoded PAN here would be exactly that
    even if it were only "test" data. Seeded, so a failure reproduces.
    """
    import random

    rng = random.Random(20260801)
    digits = [4] + [rng.randint(0, 9) for _ in range(14)]
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    digits.append((10 - checksum % 10) % 10)
    return "".join(str(d) for d in digits)


GOOD_CARD = _synthetic_visa()
GOOD_EXPIRY = "12/30"
GOOD_CVV = "321"
GOOD_NAME = "Shiv A"


@pytest.fixture(autouse=True)
def reset_drift():
    catalog_module.set_drift("none")
    yield
    catalog_module.set_drift("none")


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def add_to_cart(client, product_id, quantity=1):
    return client.post(
        "/cart/add", data={"product_id": product_id, "quantity": quantity}, follow_redirects=True
    )


# --- processor --------------------------------------------------------------


def test_luhn_accepts_a_valid_number_and_rejects_a_bent_one():
    assert luhn_ok(GOOD_CARD)
    assert not luhn_ok(GOOD_CARD[:-1] + "0")


def test_valid_visa_card_is_approved():
    decision = authorize(GOOD_CARD, GOOD_EXPIRY, GOOD_CVV, GOOD_NAME)

    assert decision["approved"]
    assert decision["response_code"] == APPROVED
    assert decision["authorization_code"]


def test_response_code_fits_pravas_two_character_cap():
    """Prava caps response_code at 2 chars, so both outcomes must fit."""
    assert len(APPROVED) <= 2
    assert len(DECLINED) <= 2


def test_non_visa_card_is_declined():
    mastercard = "5555555555554444"
    decision = authorize(mastercard, GOOD_EXPIRY, GOOD_CVV, GOOD_NAME)

    assert not decision["approved"]
    assert decision["response_code"] == DECLINED


@pytest.mark.parametrize(
    "number,expiry,cvv,name",
    [
        (GOOD_CARD[:-1] + "0", GOOD_EXPIRY, GOOD_CVV, GOOD_NAME),  # bad Luhn
        (GOOD_CARD, "13/30", GOOD_CVV, GOOD_NAME),  # month 13
        (GOOD_CARD, "12/20", GOOD_CVV, GOOD_NAME),  # expired
        (GOOD_CARD, GOOD_EXPIRY, "1", GOOD_NAME),  # short CVV
        (GOOD_CARD, GOOD_EXPIRY, GOOD_CVV, ""),  # no name
    ],
)
def test_malformed_cards_are_declined(number, expiry, cvv, name):
    assert not authorize(number, expiry, cvv, name)["approved"]


def test_spaces_and_dashes_in_the_card_number_are_tolerated():
    spaced = " ".join(GOOD_CARD[i : i + 4] for i in range(0, 16, 4))
    dashed = "-".join(GOOD_CARD[i : i + 4] for i in range(0, 16, 4))

    assert authorize(spaced, GOOD_EXPIRY, GOOD_CVV, GOOD_NAME)["approved"]
    assert authorize(dashed, GOOD_EXPIRY, GOOD_CVV, GOOD_NAME)["approved"]


# --- catalog ----------------------------------------------------------------


def test_catalog_has_three_products_with_prava_safe_ids():
    products = catalog_module.catalog()

    assert len(products) == 3
    for product_id in products:
        assert len(product_id) <= 50


def test_prices_are_2dp_decimal_strings():
    for product in catalog_module.catalog().values():
        whole, _, fraction = product["price"].partition(".")
        assert whole.isdigit() and len(fraction) == 2


# --- shopping and checkout --------------------------------------------------


def test_cart_totals_are_exact(client):
    add_to_cart(client, "BL-HOUSE-12")
    response = add_to_cart(client, "BL-FILTER-100", quantity=2)

    assert 'id="page-total">$27.00' in response.text


def test_checkout_page_exposes_the_ids_the_executor_depends_on(client):
    add_to_cart(client, "BL-HOUSE-12")

    body = client.get("/checkout").text

    for element_id in (
        'id="page-total"',
        'id="card-number"',
        'id="expiry"',
        'id="cvv"',
        'id="cardholder-name"',
        'id="submit-payment"',
    ):
        assert element_id in body
    assert "data-line-item" in body
    assert 'data-product-id="BL-HOUSE-12"' in body


def test_successful_payment_renders_a_confirmation_with_an_order_number(client):
    add_to_cart(client, "BL-HOUSE-12")

    response = client.post(
        "/checkout",
        data={
            "cardholder_name": GOOD_NAME,
            "card_number": GOOD_CARD,
            "expiry": GOOD_EXPIRY,
            "cvv": GOOD_CVV,
        },
        follow_redirects=True,
    )

    assert 'id="order-number"' in response.text
    assert "BL-" in response.text
    assert "Order confirmed" in response.text


def test_declined_payment_returns_to_checkout_with_an_error(client):
    add_to_cart(client, "BL-HOUSE-12")

    response = client.post(
        "/checkout",
        data={
            "cardholder_name": GOOD_NAME,
            "card_number": "5555555555554444",
            "expiry": GOOD_EXPIRY,
            "cvv": GOOD_CVV,
        },
        follow_redirects=True,
    )

    assert 'id="checkout-error"' in response.text
    assert 'id="order-number"' not in response.text


def test_confirmation_page_never_shows_the_full_card_number(client):
    """SPEC §12: the page the executor screenshots must not carry a PAN."""
    add_to_cart(client, "BL-HOUSE-12")

    response = client.post(
        "/checkout",
        data={
            "cardholder_name": GOOD_NAME,
            "card_number": GOOD_CARD,
            "expiry": GOOD_EXPIRY,
            "cvv": GOOD_CVV,
        },
        follow_redirects=True,
    )

    assert GOOD_CARD not in response.text
    assert GOOD_CVV not in response.text
    assert GOOD_CARD[-4:] in response.text


def test_cart_is_emptied_after_a_successful_order(client):
    add_to_cart(client, "BL-HOUSE-12")
    client.post(
        "/checkout",
        data={
            "cardholder_name": GOOD_NAME,
            "card_number": GOOD_CARD,
            "expiry": GOOD_EXPIRY,
            "cvv": GOOD_CVV,
        },
        follow_redirects=True,
    )

    assert "Empty" in client.get("/cart", follow_redirects=True).text


# --- drift toggle -----------------------------------------------------------


def test_drift_defaults_to_none(client):
    assert client.get("/_admin/state").json()["drift"] == "none"


def test_price_hike_drift_changes_the_served_price(client):
    before = catalog_module.catalog()["BL-HOUSE-12"]["price"]

    client.post("/_admin/drift", data={"mode": "price_hike"}, follow_redirects=True)
    after = catalog_module.catalog()["BL-HOUSE-12"]["price"]

    assert before == "12.50"
    assert after == "16.90"
    assert client.get("/_admin/state").json()["drift"] == "price_hike"


def test_price_hike_flows_through_to_the_cart_total(client):
    client.post("/_admin/drift", data={"mode": "price_hike"}, follow_redirects=True)

    response = add_to_cart(client, "BL-HOUSE-12")

    assert 'id="page-total">$16.90' in response.text


def test_product_swap_drift_puts_a_different_product_in_the_basket(client):
    client.post("/_admin/drift", data={"mode": "product_swap"}, follow_redirects=True)

    response = add_to_cart(client, "BL-HOUSE-12")

    assert 'data-product-id="BL-DECAF-12"' in response.text
    assert 'data-product-id="BL-HOUSE-12"' not in response.text


def test_active_drift_is_disclosed_on_the_page(client):
    """It has to be unmissable on camera -- we are not hiding the injection."""
    client.post("/_admin/drift", data={"mode": "price_hike"}, follow_redirects=True)

    assert 'id="drift-banner"' in client.get("/").text
    assert "SIMULATED" in client.get("/").text


def test_no_banner_when_drift_is_off(client):
    assert 'id="drift-banner"' not in client.get("/").text


def test_unknown_drift_mode_is_ignored(client):
    client.post("/_admin/drift", data={"mode": "chaos"}, follow_redirects=True)

    assert client.get("/_admin/state").json()["drift"] == "none"
