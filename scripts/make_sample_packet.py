"""Generate the sample evidence packet from a real end-to-end run.

Runs the whole system for real -- compiler, shopping agent, verification gate,
Prava client, Playwright checkout against the Beanline storefront -- and exports
the evidence packet. The only substitution is `fake_prava` standing in for the
Prava sandbox, so the run costs no sandbox quota.

    .venv/bin/python scripts/make_sample_packet.py

Writes evidence/sample/evidence-packet.{json,pdf}. The narrative section is a live
gpt-5-mini call and needs OPENAI_API_KEY in .env; pass --no-narrative to skip it.
"""

import argparse
import os
import socket
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from fastapi.testclient import TestClient  # noqa: E402

from backend.agent.shopper import OpenAIRationaleWriter, propose  # noqa: E402
from backend.compiler.mandate import build_draft, confirm  # noqa: E402
from backend.compiler.validate import validate_constraints  # noqa: E402
from backend.executor.runner import CheckoutExecutor  # noqa: E402
from backend.export.evidence import OpenAINarrativeWriter  # noqa: E402
from backend.export.pdf import export_packet  # noqa: E402
from backend.orchestrator import Orchestrator  # noqa: E402
from backend.origins import build_origin_map  # noqa: E402
from backend.prava_client.client import PravaClient  # noqa: E402
from fake_prava.app import app as fake_prava_app  # noqa: E402
from storefront import catalog as catalog_module  # noqa: E402
from storefront.app import app as storefront_app  # noqa: E402

INTENT = "Buy a bag of house blend and two boxes of filters from Beanline, under $30."
DEFAULT_OUT = os.path.join(REPO_ROOT, "evidence", "sample")


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


def build_mandate(now):
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
    return confirm(draft, confirmed_at=now - timedelta(minutes=1))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument(
        "--no-narrative", action="store_true", help="skip all live gpt-5-mini calls (narrative and agent rationale)"
    )
    args = parser.parse_args()

    catalog_module.set_drift("none")
    server, storefront_url = start_storefront()
    print(f"storefront: {storefront_url}")

    now = datetime.now(timezone.utc)
    mandate = build_mandate(now)
    print(f"mandate_hash: {mandate['mandate_hash']}")

    # Live rationale writer by default: agent_meta.model lands in the packet's
    # "Agent model" field, which is OpenAI-track evidence sitting in the artifact.
    # Tests always inject a stub instead; the live writer is only a default here.
    if args.no_narrative:
        rationale_writer, agent_model = None, "deterministic"
    else:
        agent_model = "gpt-5-mini"
        rationale_writer = OpenAIRationaleWriter(model=agent_model)

    proposal = propose(
        mandate,
        catalog_module.catalog(),
        created_at=now,
        rationale_writer=rationale_writer,
        model=agent_model,
    )
    print(f"proposed total: {proposal['proposed_total']}")
    print(f"agent model:    {proposal['agent_meta']['model']}")
    print(f"rationale:      {proposal['agent_meta']['rationale']}")

    with TestClient(fake_prava_app) as prava:
        prava.post("/_control/reset")
        client = PravaClient(
            base_url="", secret_key="sk_test_fake", session=prava, sleep=lambda _: None
        )

        # Stand in for the human at Prava's hosted page: approve on the first poll.
        original_poll = client.poll_until_credentials

        def poll_with_approval(session, on_status=None, now=None):
            def hook(status):
                if status == "pending":
                    prava.post(f"/_control/sessions/{session['session_id']}/approve")
                if on_status:
                    on_status(status)

            return original_poll(session, on_status=hook, now=now)

        client.poll_until_credentials = poll_with_approval

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
                    screenshot_dir=os.path.join(args.out, "screenshots"),
                    origin_map=build_origin_map({"Beanline Coffee": storefront_url}),
                )

            orchestrator = Orchestrator(client=client, executor_factory=executor_factory)
            result = orchestrator.run(
                mandate, proposal, now=now, cardholder_name="Shivansh Amba"
            )

            print(f"visa_confirmation: {result['visa_confirmation']}")
            print(f"order_number:      {result['order_number']}")
            print(f"final_status:      {result['final_status']}")

            writer = None if args.no_narrative else OpenAINarrativeWriter(model="gpt-5-mini")
            packet = export_packet(
                result["ledger"],
                args.out,
                basename="evidence-packet",
                narrative_writer=writer,
                page=page,
            )
            browser.close()

    server.should_exit = True
    print(f"\nJSON: {packet['json_path']} ({os.path.getsize(packet['json_path'])} bytes)")
    print(f"PDF:  {packet['pdf_path']} ({os.path.getsize(packet['pdf_path'])} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
