"""End-to-end happy path — SPEC §10 Tier 0.2.

Everything real except Prava: a real gpt-5-mini-shaped compiler output (stubbed
extractor, validated for real), the real gate, the real client talking to
fake_prava, a real uvicorn storefront, and a real Chromium executing checkout.
No sandbox quota is spent.

The end state this proves: `visa_confirmation: SUCCESS`, an order number on a
screenshotted confirmation page, and a hash chain that verifies from the mandate
hash through to MANDATE_CONSUMED.
"""

import socket
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.agent.shopper import propose
from backend.compiler.mandate import build_draft, confirm
from backend.compiler.validate import validate_constraints
from backend.executor.runner import CheckoutExecutor, ExecutionAborted
from backend.ledger.chain import verify_chain
from backend.orchestrator import GateBlocked, Orchestrator
from backend.origins import build_origin_map
from backend.prava_client.client import PravaClient
from fake_prava.app import app as fake_prava_app
from storefront import catalog as catalog_module

CARDHOLDER = "Shiv A"
INTENT = "Buy a bag of house blend and two boxes of filters from Beanline, under $30."


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def storefront_url():
    import uvicorn

    from storefront.app import app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        pytest.fail("storefront did not start")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)  # tracing deliberately off
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
def prava(storefront_url):
    with TestClient(fake_prava_app) as transport:
        transport.post("/_control/reset")
        yield transport
        transport.post("/_control/reset")


@pytest.fixture
def client(prava):
    return PravaClient(
        base_url="", secret_key="sk_test_fake", session=prava, sleep=lambda _: None
    )


@pytest.fixture
def compiled_mandate(now):
    """Compiler output, validated for real. The extractor is stubbed, the fence is not."""
    draft_constraints = {
        "merchant": {
            "name": "Beanline Coffee",
            "url": "https://beanline.example.com",
            "country_code_iso2": "US",
        },
        "items": [
            {
                "product_id": "BL-HOUSE-12",
                "description": "Beanline House Blend 12oz",
                "max_unit_price": "14",  # short decimal, as a model would write it
                "quantity": 1,
            },
            {
                "product_id": "BL-FILTER-100",
                "description": "Paper Filters 100ct",
                "max_unit_price": "8.00",
                "quantity": 2,
            },
        ],
        "price_ceiling_total": "30",
        "currency": "usd",  # lowercase, as a model would write it
        "effective_minutes": 15,
        "substitution_policy": "none",
    }
    constraints = validate_constraints(draft_constraints)
    draft = build_draft(
        user={"user_id": "user_001", "user_email": "shiv@example.com"},
        intent_text=INTENT,
        constraints=constraints,
        created_at=now - timedelta(minutes=1),
    )
    return confirm(draft, confirmed_at=now - timedelta(minutes=1))


@pytest.fixture
def live_catalog(storefront_url):
    """The catalog exactly as the storefront currently serves it, drift included."""
    return catalog_module.catalog()


@pytest.fixture
def orchestrator(client, page, storefront_url, tmp_path):
    attempts = {"n": 0}

    def next_attempt(_mandate_hash):
        attempts["n"] += 1
        return attempts["n"]

    def executor_factory(ledger, mandate_id):
        return CheckoutExecutor(
            page=page,
            storefront_url=storefront_url,
            ledger=ledger,
            mandate_id=mandate_id,
            screenshot_dir=str(tmp_path / "shots"),
            origin_map=build_origin_map({"Beanline Coffee": storefront_url}),
        )

    return Orchestrator(
        client=client, executor_factory=executor_factory, attempt_counter=next_attempt
    )


def approve_when_pending(prava, client):
    """Stand in for the human at the hosted page: approve on the first poll."""
    original = client.poll_until_credentials

    def patched(session, on_status=None, now=None):
        def hook(status):
            if status == "pending":
                prava.post(f"/_control/sessions/{session['session_id']}/approve")
            if on_status:
                on_status(status)

        return original(session, on_status=hook, now=now)

    client.poll_until_credentials = patched


# --- the happy path ---------------------------------------------------------


def test_happy_path_end_to_end(
    orchestrator, client, prava, compiled_mandate, live_catalog, now
):
    approve_when_pending(prava, client)
    proposal = propose(compiled_mandate, live_catalog, created_at=now)

    result = orchestrator.run(compiled_mandate, proposal, now=now, cardholder_name=CARDHOLDER)

    assert result.succeeded
    assert result["visa_confirmation"] == "SUCCESS"
    assert result["final_status"] == "completed"
    assert result["order_number"].startswith("BL-")
    assert len(result["screenshot_sha256"]) == 64
    assert result["gate_verdict"]["verdict"] == "PASS"


def test_the_chain_verifies_and_covers_every_stage(
    orchestrator, client, prava, compiled_mandate, live_catalog, now
):
    approve_when_pending(prava, client)
    proposal = propose(compiled_mandate, live_catalog, created_at=now)

    result = orchestrator.run(compiled_mandate, proposal, now=now, cardholder_name=CARDHOLDER)
    ledger = result["ledger"]

    assert [event["type"] for event in ledger["events"]] == [
        "MANDATE_CREATED",
        "MANDATE_CONFIRMED",
        "AGENT_PROPOSAL",
        "GATE_VERDICT",
        "SESSION_CREATED",
        "APPROVAL_OBSERVED",
        "CREDENTIALS_RECEIVED",
        "EXECUTION_PRECHECK",
        "CHECKOUT_EXECUTED",
        "STATUS_REPORTED",
        "MANDATE_CONSUMED",
    ]
    assert verify_chain(ledger)["valid"]


def test_the_chain_is_anchored_to_the_mandate_hash(
    orchestrator, client, prava, compiled_mandate, live_catalog, now
):
    approve_when_pending(prava, client)
    proposal = propose(compiled_mandate, live_catalog, created_at=now)

    ledger = orchestrator.run(
        compiled_mandate, proposal, now=now, cardholder_name=CARDHOLDER
    )["ledger"]

    assert ledger["mandate_hash"] == compiled_mandate["mandate_hash"]
    assert ledger["events"][0]["prev_hash"] == compiled_mandate["mandate_hash"]


def test_external_order_ref_ties_prava_to_the_evidence_chain(
    orchestrator, client, prava, compiled_mandate, live_catalog, now
):
    approve_when_pending(prava, client)
    proposal = propose(compiled_mandate, live_catalog, created_at=now)

    ledger = orchestrator.run(
        compiled_mandate, proposal, now=now, cardholder_name=CARDHOLDER
    )["ledger"]
    session_event = next(e for e in ledger["events"] if e["type"] == "SESSION_CREATED")

    assert session_event["payload"]["external_order_ref"] == (
        f"{compiled_mandate['mandate_hash']}.01"
    )
    assert session_event["payload"]["attempt"] == 1


def test_no_credential_appears_anywhere_in_the_chain(
    orchestrator, client, prava, compiled_mandate, live_catalog, now
):
    approve_when_pending(prava, client)
    proposal = propose(compiled_mandate, live_catalog, created_at=now)

    ledger = orchestrator.run(
        compiled_mandate, proposal, now=now, cardholder_name=CARDHOLDER
    )["ledger"]
    credentials_event = next(
        e for e in ledger["events"] if e["type"] == "CREDENTIALS_RECEIVED"
    )

    assert set(credentials_event["payload"]) == {
        "txn_ref_id",
        "token_last4",
        "expiry_month",
        "expiry_year",
    }
    assert len(credentials_event["payload"]["token_last4"]) == 4


def test_the_money_survives_the_whole_trip_unchanged(
    orchestrator, client, prava, compiled_mandate, live_catalog, now
):
    """27.00 at the agent, at the gate, at Prava, on the page, in the ledger."""
    approve_when_pending(prava, client)
    proposal = propose(compiled_mandate, live_catalog, created_at=now)
    assert proposal["proposed_total"] == "27.00"

    ledger = orchestrator.run(
        compiled_mandate, proposal, now=now, cardholder_name=CARDHOLDER
    )["ledger"]

    precheck = next(e for e in ledger["events"] if e["type"] == "EXECUTION_PRECHECK")
    assert precheck["payload"]["session_total"] == "27.00"
    assert precheck["payload"]["observation"]["page_total"] == "27.00"
    assert precheck["payload"]["verdict"] == "PASS"


# --- the fail path: a FAIL means no session ever exists ---------------------


def test_drifted_price_is_blocked_at_the_gate_and_no_session_is_created(
    orchestrator, prava, compiled_mandate, storefront_url, now
):
    """The applause line, asserted: gate FAIL, and Prava was never called."""
    prava.post("/_control/reset")
    drifted = dict(catalog_module.catalog())
    drifted["BL-HOUSE-12"] = dict(drifted["BL-HOUSE-12"], price="16.90")
    proposal = propose(compiled_mandate, drifted, created_at=now)

    from fake_prava.app import SEEN_EXTERNAL_ORDER_REFS, SESSIONS

    assert SESSIONS == {}, "precondition: no sessions before the run"

    with pytest.raises(GateBlocked) as exc:
        orchestrator.run(compiled_mandate, proposal, now=now, cardholder_name=CARDHOLDER)

    assert {"R3", "R5"} <= set(exc.value.verdict["failed_rule_ids"])
    # The claim that matters: Prava was never called, so no session exists and no
    # external_order_ref was burned. A FAIL at the gate is genuinely terminal.
    assert SESSIONS == {}
    assert SEEN_EXTERNAL_ORDER_REFS == set()


def test_a_blocked_run_still_leaves_a_verifiable_chain_ending_in_gate_blocked(
    orchestrator, compiled_mandate, now
):
    drifted = dict(catalog_module.catalog())
    drifted["BL-HOUSE-12"] = dict(drifted["BL-HOUSE-12"], price="16.90")
    proposal = propose(compiled_mandate, drifted, created_at=now)

    ledger = orchestrator.open_ledger(compiled_mandate)
    with pytest.raises(GateBlocked):
        orchestrator.verify(ledger, compiled_mandate, proposal, now)

    assert [e["type"] for e in ledger["events"]] == [
        "MANDATE_CREATED",
        "MANDATE_CONFIRMED",
        "AGENT_PROPOSAL",
        "GATE_VERDICT",
        "GATE_BLOCKED",
    ]
    assert verify_chain(ledger)["valid"]
    assert ledger["events"][-1]["payload"]["reason"] == "no payment session was created"


def test_a_tampered_mandate_is_refused_before_anything_runs(
    orchestrator, compiled_mandate, live_catalog, now
):
    """Editing a confirmed mandate invalidates its hash, and the run stops."""
    proposal = propose(compiled_mandate, live_catalog, created_at=now)
    tampered = dict(compiled_mandate)
    tampered["constraints"] = dict(compiled_mandate["constraints"], price_ceiling_total="99.00")

    with pytest.raises(ValueError, match="hash does not match"):
        orchestrator.run(tampered, proposal, now=now, cardholder_name=CARDHOLDER)


# --- executor abort: token issued, but no card typed, so nothing reported ---


def test_precheck_abort_reports_nothing_to_prava(
    orchestrator, client, prava, compiled_mandate, live_catalog, now, monkeypatch
):
    """A pre-check abort happens before card entry, so there is no outcome to report."""
    approve_when_pending(prava, client)
    proposal = propose(compiled_mandate, live_catalog, created_at=now)

    reported = []
    original_report = client.report_status
    monkeypatch.setattr(
        client,
        "report_status",
        lambda *args, **kwargs: reported.append(args) or original_report(*args, **kwargs),
    )
    # Storefront drifts AFTER the session is created: the page no longer matches.
    original_execute = orchestrator.executor_factory

    def drifting_factory(ledger, mandate_id):
        catalog_module.set_drift("price_hike")
        return original_execute(ledger, mandate_id)

    orchestrator.executor_factory = drifting_factory

    with pytest.raises(ExecutionAborted):
        orchestrator.run(compiled_mandate, proposal, now=now, cardholder_name=CARDHOLDER)

    assert reported == [], "no token was used, so nothing should have been reported"
