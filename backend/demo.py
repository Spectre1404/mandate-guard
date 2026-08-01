"""Demo runner: the two scenarios, each one command — SPEC §11.

    happy   : mandate -> agent -> gate PASS -> session -> checkout -> SUCCESS
    blocked : drift on -> agent proposes the drifted cart -> gate FAIL (R3/R5)
              -> GATE_BLOCKED, terminal, no session ever created

Both export an evidence packet and persist the ledger, because both are evidence.
The blocked run is arguably the more interesting artifact: it is the case a finance
team actually wants documented.

Everything runs against `fake_prava` and the local storefront. No sandbox quota is
spent, and the fake never appears in the demo recording itself.
"""

import os
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.agent.shopper import OpenAIRationaleWriter, propose
from backend.compiler.mandate import build_draft, confirm
from backend.compiler.validate import validate_constraints
from backend.executor.runner import CheckoutExecutor
from backend.export.evidence import OpenAINarrativeWriter
from backend.export.pdf import export_packet
from backend.ledger.store import save_ledger
from backend.orchestrator import GateBlocked, Orchestrator
from backend.origins import build_origin_map
from backend.prava_client.client import PravaClient
from fake_prava.app import app as fake_prava_app
from storefront import catalog as catalog_module
from storefront.app import app as storefront_app

INTENT = "Buy a bag of house blend and two boxes of filters from Beanline, under $30."
CARDHOLDER = "Shivansh Amba"
AGENT_MODEL = "gpt-5-mini"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_storefront():
    import uvicorn

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(storefront_app, host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("storefront did not start")
    return server, f"http://127.0.0.1:{port}"


def build_demo_mandate(now=None):
    now = now or datetime.now(timezone.utc)
    constraints = validate_constraints(
        {
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
        }
    )
    draft = build_draft(
        user={"user_id": "user_001", "user_email": "shivanshamba@mhub.org"},
        intent_text=INTENT,
        constraints=constraints,
        created_at=now - timedelta(minutes=1),
    )
    return confirm(draft, confirmed_at=now - timedelta(minutes=1)), now


def _writers(live):
    if not live:
        return None, None, "deterministic"
    return (
        OpenAIRationaleWriter(model=AGENT_MODEL),
        OpenAINarrativeWriter(model=AGENT_MODEL),
        AGENT_MODEL,
    )


def run_scenario(scenario, out_dir, live_llm=True, ledger_dir=None, log=print):
    """Run `happy` or `blocked` end to end and export the evidence packet."""
    if scenario not in ("happy", "blocked"):
        raise ValueError(f"unknown scenario: {scenario!r}")

    drift = "price_hike" if scenario == "blocked" else "none"
    catalog_module.set_drift(drift)
    log(f"scenario: {scenario}  ·  storefront drift: {drift}")

    server, storefront_url = start_storefront()
    rationale_writer, narrative_writer, agent_model = _writers(live_llm)

    try:
        mandate, now = build_demo_mandate()
        log(f"mandate_hash: {mandate['mandate_hash']}")

        # The agent shops the store as it finds it. Under drift it genuinely reads
        # the raised price -- nothing about its output is staged.
        proposal = propose(
            mandate,
            catalog_module.catalog(),
            created_at=now,
            rationale_writer=rationale_writer,
            model=agent_model,
        )
        log(f"agent proposed: {proposal['proposed_total']} (model: {agent_model})")

        with TestClient(fake_prava_app) as prava:
            prava.post("/_control/reset")
            client = PravaClient(
                base_url="", secret_key="sk_test_fake", session=prava, sleep=lambda _: None
            )
            _auto_approve(client, prava)

            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()  # tracing deliberately off
                page = browser.new_page()

                def executor_factory(ledger, mandate_id):
                    return CheckoutExecutor(
                        page=page,
                        storefront_url=storefront_url,
                        ledger=ledger,
                        mandate_id=mandate_id,
                        screenshot_dir=os.path.join(out_dir, "screenshots"),
                        origin_map=build_origin_map({"Beanline Coffee": storefront_url}),
                    )

                orchestrator = Orchestrator(
                    client=client, executor_factory=executor_factory
                )

                blocked = None
                result = None
                try:
                    result = orchestrator.run(
                        mandate, proposal, now=now, cardholder_name=CARDHOLDER
                    )
                    ledger = result["ledger"]
                    log(f"visa_confirmation: {result['visa_confirmation']}")
                    log(f"order_number:      {result['order_number']}")
                except GateBlocked as exc:
                    blocked = exc
                    ledger = exc.ledger
                    log(f"GATE BLOCKED — failed rules: {exc.verdict['failed_rule_ids']}")
                    log("no payment session was created; no credential was ever issued")

                packet = export_packet(
                    ledger,
                    out_dir,
                    basename=f"evidence-{scenario}",
                    narrative_writer=narrative_writer,
                    page=page,
                )
                browser.close()

        ledger_path = None
        if ledger_dir:
            ledger_path = save_ledger(ledger, ledger_dir)
            log(f"ledger saved: {ledger_path}")

        log(f"JSON: {packet['json_path']}")
        log(f"PDF:  {packet['pdf_path']}")

        return {
            "scenario": scenario,
            "ledger": ledger,
            "ledger_path": ledger_path,
            "packet": packet,
            "result": result,
            "blocked": blocked.verdict if blocked else None,
            "storefront_url": storefront_url,
        }
    finally:
        server.should_exit = True
        catalog_module.set_drift("none")


def _auto_approve(client, prava):
    """Stand in for the human at Prava's hosted page: approve on the first poll."""
    original = client.poll_until_credentials

    def patched(session, on_status=None, now=None):
        def hook(status):
            if status == "pending":
                prava.post(f"/_control/sessions/{session['session_id']}/approve")
            if on_status:
                on_status(status)

        return original(session, on_status=hook, now=now)

    client.poll_until_credentials = patched
