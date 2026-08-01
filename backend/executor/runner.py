"""Checkout executor — SPEC §7.

Playwright drives the storefront. It does exactly two things: **observe** the
rendered page, and **type** into it. Every verification decision is made by the
pure functions in `precheck.py`, so the logic that decides whether to spend money
is unit-testable without a browser and identical whether or not one is running.

Order of operations is deliberate:

    navigate -> add verified items -> observe -> E1-E3 -> [abort | type card]

The pre-check runs against the *rendered page*, after the cart is built and before
a single credential is typed. A FAIL aborts with the card fields still empty.

Credential handling (SPEC §12): the full token and dynamic CVV arrive in the
client's in-memory shape (`_token`, `_dynamic_cvv`), are passed straight to
`page.fill`, and are never logged, returned, screenshotted, or written to the
ledger. Playwright tracing is off for the whole run precisely because a trace
would capture the typed values.

Scope: this class drives the page and proves what happened. It does NOT talk to
Prava. `execute` returns the confirmation facts (order number, authorization
code, response code, screenshot hash) and the orchestrator owns the whole Prava
conversation, including report-status and the STATUS_REPORTED event -- one object
holds the payment conversation rather than two.
"""

import hashlib
import os
from datetime import datetime, timezone

from backend.executor.precheck import precheck
from backend.ledger.chain import append_event

DEFAULT_SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "evidence",
    "screenshots",
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_money_text(text):
    """'$27.00' -> '27.00'. Rendering adds a symbol; money stays a decimal string."""
    return (text or "").strip().lstrip("$").strip()


def observe_checkout_page(page):
    """Read the rendered page into the plain dict the pre-check consumes.

    Pure observation -- no assertions, no decisions. Anything this returns is a
    fact about the page, which is what makes the pre-check's verdict meaningful.
    """
    line_items = []
    for element in page.query_selector_all("[data-line-item]"):
        line_items.append(
            {
                "product_id": element.get_attribute("data-product-id"),
                "unit_price": element.get_attribute("data-unit-price"),
                "quantity": int(element.get_attribute("data-quantity")),
            }
        )
    return {
        "url": page.url,
        "page_total": parse_money_text(page.inner_text("#page-total")),
        "line_items": line_items,
    }


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


class ExecutionAborted(RuntimeError):
    """The pre-check failed. No card was entered, so nothing needs reporting."""

    def __init__(self, precheck_result):
        self.precheck_result = precheck_result
        super().__init__(
            "execution aborted before card entry: "
            f"{precheck_result['failed_rule_ids']}"
        )


class CheckoutExecutor:
    def __init__(
        self,
        page,
        storefront_url,
        ledger=None,
        mandate_id=None,
        screenshot_dir=DEFAULT_SCREENSHOT_DIR,
        clock=_now_iso,
        origin_map=None,
    ):
        """`page` is an already-open Playwright page.

        Injected rather than owned so the caller controls headed/headless (headed
        for the demo recording) and so tracing stays the caller's decision -- it
        must be OFF across card entry.
        """
        self.page = page
        self.storefront_url = storefront_url.rstrip("/")
        self.ledger = ledger
        self.mandate_id = mandate_id
        self.screenshot_dir = screenshot_dir
        self.clock = clock
        self.origin_map = origin_map

    def _record(self, event_type, payload):
        if self.ledger is None:
            return None
        return append_event(
            self.ledger, event_type, payload, ts=self.clock(), mandate_id=self.mandate_id
        )

    # --- steps --------------------------------------------------------------

    def build_cart(self, proposal):
        """Add exactly the verified line items. Nothing else goes in the basket."""
        self.page.goto(f"{self.storefront_url}/")
        for line in proposal["line_items"]:
            for _ in range(line["quantity"]):
                self.page.click(f'[data-add="{line["product_id"]}"]')
                self.page.wait_for_load_state()
                self.page.goto(f"{self.storefront_url}/")
        self.page.goto(f"{self.storefront_url}/checkout")
        self.page.wait_for_selector("#page-total")

    def run_precheck(self, mandate, proposal, session_total):
        """E1-E3 against the rendered page. Ledgers the result either way."""
        observation = observe_checkout_page(self.page)
        result = precheck(observation, proposal, mandate, session_total, self.origin_map)

        self._record(
            "EXECUTION_PRECHECK",
            {
                "verdict": result["verdict"],
                "results": result["results"],
                "failed_rule_ids": result["failed_rule_ids"],
                "observation": observation,
                "session_total": session_total,
                # Canonical URL, declared origin, observed host -- the mapping is
                # disclosed in the evidence rather than buried in config.
                "origin_disclosure": result["origin_disclosure"],
            },
        )
        if result["verdict"] != "PASS":
            raise ExecutionAborted(result)
        return result

    def enter_card_and_submit(self, credentials, cardholder_name):
        """Type the one-time credentials. Nothing here is logged or returned."""
        expiry = f"{credentials['expiry_month']}/{str(credentials['expiry_year'])[-2:]}"

        self.page.fill("#cardholder-name", cardholder_name)
        self.page.fill("#card-number", credentials["_token"])
        self.page.fill("#expiry", expiry)
        self.page.fill("#cvv", credentials["_dynamic_cvv"])
        self.page.click("#submit-payment")
        self.page.wait_for_load_state()

    def capture_confirmation(self):
        """Order number + full-page screenshot + its SHA-256. The completion proof."""
        if not self.page.query_selector("#order-number"):
            error = self.page.query_selector("#checkout-error")
            raise RuntimeError(
                f"no confirmation page: {error.inner_text() if error else 'unknown failure'}"
            )

        order_number = self.page.inner_text("#order-number").strip()
        os.makedirs(self.screenshot_dir, exist_ok=True)
        path = os.path.join(self.screenshot_dir, f"{order_number}.png")
        self.page.screenshot(path=path, full_page=True)

        return {
            "order_number": order_number,
            "authorization_code": self.page.inner_text("#authorization-code").strip(),
            "response_code": self.page.inner_text("#response-code").strip(),
            "screenshot_path": path,
            "screenshot_sha256": sha256_file(path),
            "confirmation_url": self.page.url,
        }

    # --- orchestration ------------------------------------------------------

    def execute(self, mandate, proposal, credentials, session_total, cardholder_name):
        """Cart -> pre-check -> card entry -> confirmation. Returns the proof.

        Raises ExecutionAborted if the pre-check fails, with the card fields
        untouched. The caller reports nothing to Prava in that case: no token was
        used, so there is no outcome to report.
        """
        self.build_cart(proposal)
        self.run_precheck(mandate, proposal, session_total)
        self.enter_card_and_submit(credentials, cardholder_name)
        confirmation = self.capture_confirmation()

        self._record("CHECKOUT_EXECUTED", confirmation)
        return confirmation
