"""Mandate lifecycle dashboard — SPEC §10 Tier 1.6.

The dashboard is a projection: it stores nothing, and every status is derived from
ledger events on request. These tests drive it through the HTTP surface with an
injected clock, which is the only way EXPIRED is testable.

Status derivation itself is covered exhaustively in test_lifecycle.py; here the
concern is that the page shows what the derivation says, and that the element-ID
contract in ledger_ui/app.py holds — this page is on camera.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.ledger.chain import append_event, new_ledger
from backend.ledger.store import save_ledger

CREATED = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
INSIDE = CREATED + timedelta(minutes=5)
OUTSIDE = CREATED + timedelta(minutes=20)
HAPPY_HASH = "a" * 64
BLOCKED_HASH = "b" * 64


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def mandate(mandate_hash, mandate_id):
    return {
        "mandate_id": mandate_id,
        "created_at": iso(CREATED),
        "confirmed_at": iso(CREATED),
        "status": "confirmed",
        "user": {"user_id": "u1", "user_email": "shiv@example.com"},
        "intent_text": "Buy house blend and filters under $30.",
        "constraints": {
            "merchant": {"name": "Beanline Coffee", "url": "https://beanline.example.com"},
            "items": [
                {"product_id": "BL-HOUSE-12", "description": "House Blend",
                 "max_unit_price": "14.00", "quantity": 1},
                {"product_id": "BL-FILTER-100", "description": "Filters",
                 "max_unit_price": "8.00", "quantity": 2},
            ],
            "price_ceiling_total": "30.00",
            "currency": "USD",
            "effective_minutes": 15,
        },
        "mandate_hash": mandate_hash,
    }


def happy_ledger():
    ledger = new_ledger(HAPPY_HASH)
    ts = iso(CREATED)
    add = lambda t, p: append_event(ledger, t, p, ts=ts, mandate_id="m-happy")
    add("MANDATE_CREATED", {"mandate": mandate(HAPPY_HASH, "m-happy")})
    add("MANDATE_CONFIRMED", {"confirmed_at": ts})
    add("AGENT_PROPOSAL", {"proposal": {"proposal_id": "p1"}})
    add("GATE_VERDICT", {"verdict": "PASS", "results": [], "failed_rule_ids": []})
    add("SESSION_CREATED", {"session_id": "ses_01", "order_id": "ord_01",
        "external_order_ref": f"{HAPPY_HASH}.01", "attempt": 1})
    add("CREDENTIALS_RECEIVED", {"txn_ref_id": "tli_01", "token_last4": "5981",
        "expiry_month": "12", "expiry_year": "2030"})
    add("CHECKOUT_EXECUTED", {"order_number": "BL-DFBD782B"})
    add("STATUS_REPORTED", {"txn_status": "APPROVED", "visa_confirmation": "SUCCESS"})
    add("MANDATE_CONSUMED", {"order_number": "BL-DFBD782B"})
    return ledger


def blocked_ledger():
    ledger = new_ledger(BLOCKED_HASH)
    ts = iso(CREATED)
    add = lambda t, p: append_event(ledger, t, p, ts=ts, mandate_id="m-blocked")
    add("MANDATE_CREATED", {"mandate": mandate(BLOCKED_HASH, "m-blocked")})
    add("MANDATE_CONFIRMED", {"confirmed_at": ts})
    add("AGENT_PROPOSAL", {"proposal": {"proposal_id": "p2"}})
    add("GATE_VERDICT", {"verdict": "FAIL", "results": [], "failed_rule_ids": ["R3", "R5"]})
    add("GATE_BLOCKED", {"failed_rule_ids": ["R3", "R5"],
        "reason": "no payment session was created"})
    return ledger


@pytest.fixture
def ui(tmp_path, monkeypatch):
    directory = tmp_path / "ledgers"
    directory.mkdir()
    monkeypatch.setenv("MANDATE_GUARD_LEDGER_DIR", str(directory))
    monkeypatch.setenv("MANDATE_GUARD_EXPORT_DIR", str(tmp_path / "exports"))

    import importlib

    import ledger_ui.app as module

    importlib.reload(module)
    module.CLOCK = lambda: INSIDE
    return module, directory


@pytest.fixture
def client(ui):
    module, _ = ui
    with TestClient(module.app) as c:
        yield c


def seed(ui, *ledgers):
    _, directory = ui
    for ledger in ledgers:
        save_ledger(ledger, str(directory))


# --- the projection constraint ----------------------------------------------


def test_every_page_states_it_is_derived_from_the_ledger(client, ui):
    seed(ui, happy_ledger())

    for path in ("/mandates", f"/mandates/{HAPPY_HASH}", "/"):
        body = client.get(path).text
        assert 'id="derived-footer"' in body, path
        assert "derived entirely from the hash-chained ledger" in body, path


def test_no_page_emits_a_single_quoted_id_attribute(client, ui):
    """Systemic guard: `id='x'` renders fine but breaks every `id="x"` selector.

    The element IDs are a documented contract that Playwright and the demo script
    depend on, and mixed quoting has silently broken it three times. Cheaper to
    assert the convention than to rediscover it per page.
    """
    seed(ui, happy_ledger(), blocked_ledger())

    for path in ("/mandates", f"/mandates/{HAPPY_HASH}", f"/mandates/{BLOCKED_HASH}",
                 "/", "/ledger/m-happy"):
        assert "id='" not in client.get(path).text, path


def test_the_dashboard_holds_no_state_of_its_own(client, ui):
    """Deleting the ledger files empties the dashboard: nothing is cached."""
    seed(ui, happy_ledger())
    assert "data-mandate-row" in client.get("/mandates").text

    _, directory = ui
    for path in directory.iterdir():
        path.unlink()

    assert "data-mandate-row" not in client.get("/mandates").text
    assert "No mandates recorded yet" in client.get("/mandates").text


# --- index ------------------------------------------------------------------


def test_index_lists_both_runs_with_the_contract_ids(client, ui):
    seed(ui, happy_ledger(), blocked_ledger())

    body = client.get("/mandates").text

    assert 'id="mandate-list"' in body
    assert 'id="mandate-summary"' in body
    assert body.count("data-mandate-row") == 2
    assert f'data-mandate-hash="{HAPPY_HASH}"' in body
    assert f'data-mandate-hash="{BLOCKED_HASH}"' in body
    assert body.count("data-status") == 2
    assert body.count("data-last-outcome") == 2


def test_index_shows_the_derived_fields(client, ui):
    seed(ui, happy_ledger())

    body = client.get("/mandates").text

    assert "Beanline Coffee" in body
    assert "BL-HOUSE-12 ×1, BL-FILTER-100 ×2" in body
    assert "30.00" in body
    assert HAPPY_HASH[:12] in body


def test_a_blocked_attempt_is_shown_as_active_with_a_blocked_outcome(client, ui):
    """The distinction the product turns on, visible on the row."""
    seed(ui, blocked_ledger())

    body = client.get("/mandates").text

    assert '<span class="chip ACTIVE">ACTIVE</span>' in body
    assert '<span class="chip BLOCKED">BLOCKED</span>' in body
    assert "CONSUMED" not in body.split("id=\"mandate-list\"")[1]


def test_a_completed_run_is_shown_as_consumed(client, ui):
    seed(ui, happy_ledger())

    body = client.get("/mandates").text

    assert '<span class="chip CONSUMED">CONSUMED</span>' in body
    assert '<span class="chip COMPLETED">COMPLETED</span>' in body


def test_summary_counts_are_rendered(client, ui):
    seed(ui, happy_ledger(), blocked_ledger())

    body = client.get("/mandates").text

    assert 'data-count="total"' in body
    assert 'data-count="ACTIVE"' in body
    assert 'data-count="CONSUMED"' in body
    assert 'data-count="blocked_attempts"' in body


def test_expired_is_time_derived_via_the_injected_clock(client, ui):
    """Same files, later clock, different status -- nothing on disk changed."""
    module, _ = ui
    seed(ui, blocked_ledger())

    assert '<span class="chip ACTIVE">ACTIVE</span>' in client.get("/mandates").text

    module.CLOCK = lambda: OUTSIDE
    body = client.get("/mandates").text

    assert '<span class="chip EXPIRED">EXPIRED</span>' in body
    assert '<span class="chip ACTIVE">ACTIVE</span>' not in body
    # A blocked attempt is still reported, independent of the window.
    assert '<span class="chip BLOCKED">BLOCKED</span>' in body


def test_a_consumed_mandate_does_not_decay_into_expired(client, ui):
    module, _ = ui
    seed(ui, happy_ledger())

    module.CLOCK = lambda: OUTSIDE

    assert '<span class="chip CONSUMED">CONSUMED</span>' in client.get("/mandates").text


# --- detail -----------------------------------------------------------------


def test_detail_renders_constraints_timeline_and_attempts(client, ui):
    seed(ui, happy_ledger())

    body = client.get(f"/mandates/{HAPPY_HASH}").text

    for element_id in ('id="constraints-card"', 'id="lifecycle-timeline"',
                       'id="attempt-history"', 'id="mandate-chains"'):
        assert element_id in body, element_id
    assert "Buy house blend and filters under $30." in body
    assert "BL-DFBD782B" in body


def test_detail_timeline_is_in_causal_order(client, ui):
    seed(ui, happy_ledger())

    body = client.get(f"/mandates/{HAPPY_HASH}").text
    timeline = body.split('id="lifecycle-timeline"')[1]

    assert timeline.index("MANDATE_CREATED") < timeline.index("GATE_VERDICT")
    assert timeline.index("GATE_VERDICT") < timeline.index("SESSION_CREATED")
    assert timeline.index("STATUS_REPORTED") < timeline.index("MANDATE_CONSUMED")


def test_detail_attempt_history_shows_the_order_ref_suffix(client, ui):
    seed(ui, happy_ledger())

    body = client.get(f"/mandates/{HAPPY_HASH}").text

    assert "data-attempt-row" in body
    assert 'data-attempt="1"' in body
    assert ".01" in body
    assert "ses_01" in body


def test_detail_of_a_blocked_mandate_explains_the_absent_session(client, ui):
    seed(ui, blocked_ledger())

    body = client.get(f"/mandates/{BLOCKED_HASH}").text

    assert "No payment session was ever created" in body
    assert "data-attempt-row" not in body


def test_detail_links_to_the_chain_view_and_both_exports(client, ui):
    seed(ui, happy_ledger())

    body = client.get(f"/mandates/{HAPPY_HASH}").text

    assert "/ledger/m-happy" in body
    assert "/ledger/m-happy/export.json" in body
    assert "/ledger/m-happy/export.pdf" in body


def test_detail_reports_a_broken_chain(client, ui):
    ledger = happy_ledger()
    ledger["events"][2]["payload"]["proposal"]["proposal_id"] = "tampered"
    seed(ui, ledger)

    body = client.get(f"/mandates/{HAPPY_HASH}").text

    assert "chain BROKEN" in body


def test_unknown_mandate_is_handled(client, ui):
    seed(ui, happy_ledger())

    assert "No such mandate" in client.get("/mandates/" + "f" * 64).text
