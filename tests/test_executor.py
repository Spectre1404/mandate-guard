"""Checkout executor against a live storefront in a real browser — SPEC §7.

These are integration tests: a real uvicorn server and a real Chromium. That is
deliberate. The executor's whole job is to be correct about a *rendered page*, and
a mocked page would test nothing that matters.

The verification logic itself is already covered without a browser in
test_executor_precheck.py. What is proved here is the wiring: that the pre-check
sees what is actually on screen, that a FAIL stops before the card fields are
touched, and that the confirmation proof is real.
"""

import socket
import threading
import time

import pytest

from backend.executor.runner import (
    CheckoutExecutor,
    ExecutionAborted,
    observe_checkout_page,
    parse_money_text,
    sha256_file,
)
from backend.ledger.chain import new_ledger, verify_chain
from backend.origins import build_origin_map
from storefront import catalog as catalog_module

MANDATE_HASH = "e" * 64
CARDHOLDER = "Shiv A"


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def storefront_url():
    """A real uvicorn server; Playwright cannot navigate to an in-process TestClient."""
    import uvicorn

    from storefront.app import app

    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("storefront server did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        # Tracing stays OFF: a trace would capture the typed card credentials.
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    page = context.new_page()
    yield page
    context.close()


@pytest.fixture(autouse=True)
def reset_drift():
    catalog_module.set_drift("none")
    yield
    catalog_module.set_drift("none")


@pytest.fixture
def credentials():
    """The client's in-memory credential shape. Underscored keys never persist."""
    return {
        "txn_ref_id": "tli_executor_test",
        "_token": _luhn_visa(),
        "_dynamic_cvv": "321",
        "expiry_month": "12",
        "expiry_year": "2027",
        "token_last4": _luhn_visa()[-4:],
    }


def _luhn_visa():
    import random

    rng = random.Random(4242)
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


@pytest.fixture
def origin_map(storefront_url):
    """The mandate keeps its canonical https URL; the store is served locally.

    This is the declared-origin mapping: E2 compares the observed page against
    the origin Beanline is actually served from, and all three values land in the
    EXECUTION_PRECHECK event so the substitution is visible in the evidence.
    """
    return build_origin_map({"Beanline Coffee": storefront_url})


@pytest.fixture
def executor(page, storefront_url, tmp_path, origin_map):
    ledger = new_ledger(MANDATE_HASH)
    return CheckoutExecutor(
        page=page,
        storefront_url=storefront_url,
        ledger=ledger,
        mandate_id="m1",
        screenshot_dir=str(tmp_path / "shots"),
        origin_map=origin_map,
    )


# --- helpers ----------------------------------------------------------------


def test_parse_money_text_strips_the_rendered_symbol():
    assert parse_money_text("$27.00") == "27.00"
    assert parse_money_text("  $7.25 ") == "7.25"


# --- observation ------------------------------------------------------------


def test_observation_reads_the_cart_off_the_rendered_page(executor, mandate, proposal):
    executor.build_cart(proposal)

    observation = observe_checkout_page(executor.page)

    assert observation["page_total"] == "27.00"
    assert sorted(
        (i["product_id"], i["unit_price"], i["quantity"]) for i in observation["line_items"]
    ) == [("BL-FILTER-100", "7.25", 2), ("BL-HOUSE-12", "12.50", 1)]
    assert observation["url"].endswith("/checkout")


# --- happy path -------------------------------------------------------------


def test_full_execution_produces_a_confirmation_and_a_hashed_screenshot(
    executor, mandate, proposal, credentials
):
    confirmation = executor.execute(
        mandate, proposal, credentials, session_total="27.00", cardholder_name=CARDHOLDER
    )

    assert confirmation["order_number"].startswith("BL-")
    assert confirmation["response_code"] == "00"
    assert confirmation["authorization_code"]
    assert sha256_file(confirmation["screenshot_path"]) == confirmation["screenshot_sha256"]
    assert len(confirmation["screenshot_sha256"]) == 64


def test_screenshot_exists_on_disk_and_is_a_png(executor, mandate, proposal, credentials):
    confirmation = executor.execute(
        mandate, proposal, credentials, session_total="27.00", cardholder_name=CARDHOLDER
    )

    with open(confirmation["screenshot_path"], "rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n"


def test_ledger_records_precheck_then_execution_and_the_chain_verifies(
    executor, mandate, proposal, credentials
):
    executor.execute(
        mandate, proposal, credentials, session_total="27.00", cardholder_name=CARDHOLDER
    )

    types = [event["type"] for event in executor.ledger["events"]]
    assert types == ["EXECUTION_PRECHECK", "CHECKOUT_EXECUTED"]
    assert verify_chain(executor.ledger)["valid"]


KEY_MARKER = "#key"

# System-generated identifiers and digests. A 3-digit CVV lands inside random hex
# by chance, so substring-scanning these would be flaky without proving anything:
# none of them is a place a credential could be *stored*, only a place three
# digits can coincide. Exact-value checks still apply to them.
IDENTIFIER_FIELDS = {
    "event_hash",
    "prev_hash",
    "mandate_hash",
    "event_id",
    "screenshot_sha256",
    "screenshot_path",
    "order_number",
    "txn_id",
    "txn_ref_id",
    "product_ref_id",
    "authorization_code",  # derived from the token's last 4, which is permitted
}


def _walk(node, field=None):
    """Yield (field_name, scalar) for every leaf, and (KEY_MARKER, key) for keys."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield (KEY_MARKER, key)
            yield from _walk(value, key)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _walk(value, field)
    else:
        yield (field, node)


def test_no_credential_ever_reaches_the_ledger(executor, mandate, proposal, credentials):
    """SPEC §12 -- the chain must be safe to hand to a judge verbatim.

    Three checks: no field stores a credential outright, no forbidden key name
    exists at any depth, and no credential hides inside a longer string (a path,
    a description, a concatenation). Digest and identifier fields are exempt from
    the substring scan only -- see IDENTIFIER_FIELDS.
    """
    executor.execute(
        mandate, proposal, credentials, session_total="27.00", cardholder_name=CARDHOLDER
    )

    walked = list(_walk(executor.ledger))
    keys = {value for field, value in walked if field == KEY_MARKER}
    leaves = [(field, value) for field, value in walked if field != KEY_MARKER]

    assert not {"token", "dynamic_cvv", "_token", "_dynamic_cvv", "cvv", "pan"} & keys

    for field, value in leaves:
        assert value != credentials["_token"], f"full token stored at {field}"
        assert value != credentials["_dynamic_cvv"], f"CVV stored at {field}"

        if isinstance(value, str) and field not in IDENTIFIER_FIELDS:
            assert credentials["_token"] not in value, f"token embedded in {field}"
            assert credentials["_dynamic_cvv"] not in value, f"CVV embedded in {field}"


def test_confirmation_result_carries_no_credential(executor, mandate, proposal, credentials):
    confirmation = executor.execute(
        mandate, proposal, credentials, session_total="27.00", cardholder_name=CARDHOLDER
    )

    assert credentials["_token"] not in str(confirmation)
    assert credentials["_dynamic_cvv"] not in str(confirmation)


# --- abort paths ------------------------------------------------------------


def test_e1_mismatch_aborts_before_any_card_is_typed(
    executor, mandate, proposal, credentials
):
    """The session authorized 27.00; if the page says otherwise, stop."""
    with pytest.raises(ExecutionAborted) as exc:
        executor.execute(
            mandate, proposal, credentials, session_total="99.00", cardholder_name=CARDHOLDER
        )

    assert "E1" in exc.value.precheck_result["failed_rule_ids"]
    # The card fields must still be empty -- nothing was typed.
    assert executor.page.input_value("#card-number") == ""
    assert executor.page.input_value("#cvv") == ""
    assert executor.page.query_selector("#order-number") is None


def test_price_drift_on_the_storefront_is_caught_by_the_precheck(
    executor, mandate, proposal, credentials, storefront_url
):
    """The demo failure: the merchant raises a price after the session was created."""
    executor.page.goto(f"{storefront_url}/_admin")
    executor.page.click("#drift-price_hike")
    executor.page.wait_for_load_state()

    with pytest.raises(ExecutionAborted) as exc:
        executor.execute(
            mandate, proposal, credentials, session_total="27.00", cardholder_name=CARDHOLDER
        )

    failed = exc.value.precheck_result["failed_rule_ids"]
    assert "E1" in failed  # page total no longer matches the authorized amount
    assert "E3" in failed  # and the line items no longer match the verified proposal


def test_an_aborted_run_still_ledgers_the_precheck_failure(
    executor, mandate, proposal, credentials
):
    """A blocked execution is evidence too -- it must be in the record."""
    with pytest.raises(ExecutionAborted):
        executor.execute(
            mandate, proposal, credentials, session_total="99.00", cardholder_name=CARDHOLDER
        )

    events = executor.ledger["events"]
    assert [e["type"] for e in events] == ["EXECUTION_PRECHECK"]
    assert events[0]["payload"]["verdict"] == "FAIL"
    assert verify_chain(executor.ledger)["valid"]


def test_declined_card_raises_rather_than_inventing_a_confirmation(
    executor, mandate, proposal, credentials
):
    """A non-Visa token is declined by the mock processor; do not fake success."""
    bad = dict(credentials, _token="5" + credentials["_token"][1:])

    with pytest.raises(RuntimeError, match="no confirmation page"):
        executor.execute(
            mandate, proposal, bad, session_total="27.00", cardholder_name=CARDHOLDER
        )


# --- declared origin mapping ------------------------------------------------


def test_precheck_passes_under_the_declared_origin_mapping(executor, mandate, proposal):
    """The mandate says beanline.example.com; the page is on 127.0.0.1 -- and that
    is fine, because the mapping declares it. The canonical URL is untouched."""
    executor.build_cart(proposal)

    result = executor.run_precheck(mandate, proposal, session_total="27.00")

    assert result["verdict"] == "PASS"
    assert mandate["constraints"]["merchant"]["url"] == "https://beanline.example.com"


def test_wrong_origin_still_fails_e2_under_the_mapping(
    page, storefront_url, tmp_path, mandate, proposal
):
    """The mapping redirects the comparison; it must not relax it.

    Beanline is declared as served from somewhere else entirely, so the page the
    executor is standing on is not the declared origin and E2 must refuse.
    """
    executor = CheckoutExecutor(
        page=page,
        storefront_url=storefront_url,
        ledger=new_ledger(MANDATE_HASH),
        mandate_id="m1",
        screenshot_dir=str(tmp_path / "shots"),
        origin_map=build_origin_map({"Beanline Coffee": "https://someone-elses-shop.test"}),
    )
    executor.build_cart(proposal)

    with pytest.raises(ExecutionAborted) as exc:
        executor.run_precheck(mandate, proposal, session_total="27.00")

    assert "E2" in exc.value.precheck_result["failed_rule_ids"]


def test_the_origin_mapping_is_disclosed_in_the_ledger(executor, mandate, proposal, storefront_url):
    """All three values are in evidence, so the substitution is not hidden."""
    executor.build_cart(proposal)
    executor.run_precheck(mandate, proposal, session_total="27.00")

    disclosure = executor.ledger["events"][0]["payload"]["origin_disclosure"]

    assert disclosure["canonical_merchant_url"] == "https://beanline.example.com"
    assert disclosure["declared_origin"] == storefront_url
    assert disclosure["observed_host"] == "127.0.0.1"


def test_executor_does_not_talk_to_prava(executor):
    """Reporting belongs to the orchestrator; the executor only drives the page."""
    assert not hasattr(executor, "report")
