"""Ledger browser — SPEC §4 verify-chain UI, SPEC §11 Act 3.

The element IDs asserted here are the contract documented at the top of
ledger_ui/app.py. This page is on camera, so a renamed ID is a broken demo.

The behaviour that matters most: the verify button must be a real recomputation.
A ledger tampered with on disk between page loads must turn red without a restart.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.ledger.chain import append_event, new_ledger
from backend.ledger.store import LEDGER_SUFFIX, save_ledger

MANDATE_HASH = "b" * 64


def _mandate(mandate_id):
    """A realistic mandate, so the PDF export path renders a real record."""
    return {
        "mandate_id": mandate_id,
        "version": "1.0",
        "created_at": "2026-08-01T11:59:00Z",
        "confirmed_at": "2026-08-01T11:59:30Z",
        "status": "confirmed",
        "user": {"user_id": "u1", "user_email": "shiv@example.com"},
        "intent_text": "Buy house blend under $30.",
        "constraints": {
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
            "price_ceiling_total": "30.00",
            "currency": "USD",
            "effective_minutes": 15,
            "substitution_policy": "none",
        },
        "mandate_hash": MANDATE_HASH,
    }


def _completed_ledger(mandate_id="m-happy"):
    ledger = new_ledger(MANDATE_HASH)
    stamp = "2026-08-01T12:00:00Z"
    append_event(ledger, "MANDATE_CREATED", {"mandate": _mandate(mandate_id)},
                 ts=stamp, mandate_id=mandate_id)
    append_event(ledger, "GATE_VERDICT", {"verdict": "PASS", "results": [],
                 "failed_rule_ids": []}, ts=stamp, mandate_id=mandate_id)
    append_event(ledger, "CHECKOUT_EXECUTED", {"order_number": "BL-TEST01"},
                 ts=stamp, mandate_id=mandate_id)
    append_event(ledger, "STATUS_REPORTED", {"txn_status": "APPROVED",
                 "visa_confirmation": "SUCCESS"}, ts=stamp, mandate_id=mandate_id)
    return ledger


def _blocked_ledger(mandate_id="m-blocked"):
    ledger = new_ledger(MANDATE_HASH)
    stamp = "2026-08-01T13:00:00Z"
    append_event(ledger, "MANDATE_CREATED", {"mandate": _mandate(mandate_id)},
                 ts=stamp, mandate_id=mandate_id)
    append_event(ledger, "GATE_VERDICT", {"verdict": "FAIL", "results": [],
                 "failed_rule_ids": ["R3", "R5"]}, ts=stamp, mandate_id=mandate_id)
    append_event(ledger, "GATE_BLOCKED", {"failed_rule_ids": ["R3", "R5"],
                 "reason": "no payment session was created"}, ts=stamp, mandate_id=mandate_id)
    return ledger


@pytest.fixture
def ledger_dir(tmp_path, monkeypatch):
    directory = tmp_path / "ledgers"
    directory.mkdir()
    monkeypatch.setenv("MANDATE_GUARD_LEDGER_DIR", str(directory))
    monkeypatch.setenv("MANDATE_GUARD_EXPORT_DIR", str(tmp_path / "exports"))
    # Isolate from the committed seed ledgers: startup restores them into the
    # ledger dir, which would otherwise pollute every count in this file.
    (tmp_path / "empty-seeds").mkdir(exist_ok=True)
    monkeypatch.setenv("MANDATE_GUARD_SEED_DIR", str(tmp_path / "empty-seeds"))

    import importlib

    import ledger_ui.app as module

    importlib.reload(module)
    return directory, module


@pytest.fixture
def client(ledger_dir):
    _, module = ledger_dir
    with TestClient(module.app) as c:
        yield c


# --- index ------------------------------------------------------------------


def test_empty_index_tells_you_how_to_generate_a_run(client):
    body = client.get("/").text

    assert "No runs recorded yet" in body
    assert "scripts/run_demo.py happy" in body


def test_index_lists_runs_with_the_contract_ids(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_completed_ledger(), str(directory))
    save_ledger(_blocked_ledger(), str(directory))

    body = client.get("/").text

    assert 'id="ledger-list"' in body
    assert body.count("data-ledger-row") == 2
    assert 'data-ledger-id="m-happy"' in body
    assert 'data-ledger-id="m-blocked"' in body


def test_index_distinguishes_a_blocked_run_from_a_completed_one(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_completed_ledger(), str(directory))
    save_ledger(_blocked_ledger(), str(directory))

    body = client.get("/").text

    assert "GATE FAIL" in body
    assert "GATE PASS" in body
    assert "R3, R5" in body


# --- chain view -------------------------------------------------------------


def test_chain_view_renders_every_event_with_the_contract_ids(client, ledger_dir):
    directory, _ = ledger_dir
    ledger = _completed_ledger()
    save_ledger(ledger, str(directory))

    body = client.get("/ledger/m-happy").text

    for element_id in ('id="chain-table"', 'id="verify-chain"', 'id="export-json"',
                       'id="export-pdf"', 'id="outcome-banner"'):
        assert element_id in body, element_id
    assert body.count("data-event-row") == len(ledger["events"])
    assert body.count("data-link-status") == len(ledger["events"])
    assert 'data-event-type="STATUS_REPORTED"' in body


def test_chain_view_shows_the_mandate_hash_anchor(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_completed_ledger(), str(directory))

    assert MANDATE_HASH in client.get("/ledger/m-happy").text


def test_completed_run_shows_a_success_banner(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_completed_ledger(), str(directory))

    body = client.get("/ledger/m-happy").text

    assert "confirmed by the card network" in body
    assert "BL-TEST01" in body


def test_blocked_run_says_no_session_was_created(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_blocked_ledger(), str(directory))

    body = client.get("/ledger/m-blocked").text

    assert "BLOCKED by the verification gate" in body
    assert "No payment session was created" in body


def test_unknown_ledger_is_handled(client):
    assert "No such ledger" in client.get("/ledger/nope").text


def test_a_partial_mandate_degrades_rather_than_crashing(client, ledger_dir):
    """An evidence tool that 500s on an odd record is useless when it is needed."""
    directory, _ = ledger_dir
    ledger = new_ledger(MANDATE_HASH)
    append_event(
        ledger,
        "MANDATE_CREATED",
        {"mandate": {"mandate_id": "m-partial", "intent_text": "no constraints here"}},
        ts="2026-08-01T12:00:00Z",
        mandate_id="m-partial",
    )
    save_ledger(ledger, str(directory))

    assert client.get("/ledger/m-partial").status_code == 200
    response = client.get("/ledger/m-partial/export.pdf")
    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


# --- the verify button ------------------------------------------------------


def test_verify_button_reports_verified_for_an_intact_chain(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_completed_ledger(), str(directory))

    body = client.post("/ledger/m-happy/verify", follow_redirects=True).text

    assert 'id="verify-result"' in body
    assert 'id="chain-verdict"' in body
    assert ">VERIFIED<" in body
    assert "recomputed just now" in body


def test_verify_recomputes_from_disk_so_tampering_shows_without_a_restart(
    client, ledger_dir
):
    """The demo moment: edit the file, click verify, watch it turn red."""
    directory, _ = ledger_dir
    save_ledger(_completed_ledger(), str(directory))
    path = os.path.join(str(directory), f"m-happy{LEDGER_SUFFIX}")

    assert ">VERIFIED<" in client.post(
        "/ledger/m-happy/verify", follow_redirects=True
    ).text

    tampered = json.load(open(path))
    tampered["events"][2]["payload"]["order_number"] = "BL-FORGED"
    with open(path, "w") as handle:
        json.dump(tampered, handle)

    body = client.post("/ledger/m-happy/verify", follow_redirects=True).text

    assert ">BROKEN<" in body
    assert ">VERIFIED<" not in body
    assert "first break at index 2" in body


def test_a_broken_link_is_marked_broken_in_its_own_row(client, ledger_dir):
    directory, _ = ledger_dir
    ledger = _completed_ledger()
    ledger["events"][1]["payload"]["verdict"] = "TAMPERED"
    save_ledger(ledger, str(directory))

    body = client.get("/ledger/m-happy").text

    assert "BROKEN" in body
    assert "event_hash mismatch" in body


def test_the_chain_status_is_recomputed_not_stored(client, ledger_dir):
    """No cached 'valid' flag is trusted: a forged one must not survive."""
    directory, _ = ledger_dir
    ledger = _completed_ledger()
    ledger["chain_valid"] = True  # a lie planted in the file
    ledger["events"][2]["payload"]["order_number"] = "BL-FORGED"
    save_ledger(ledger, str(directory))

    body = client.post("/ledger/m-happy/verify", follow_redirects=True).text

    assert ">BROKEN<" in body


# --- exports ----------------------------------------------------------------


def test_export_json_returns_the_packet(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_completed_ledger(), str(directory))

    response = client.get("/ledger/m-happy/export.json")

    assert response.status_code == 200
    packet = json.loads(response.content)
    assert packet["outcome"] == "COMPLETED"
    assert packet["chain"]["valid"]


def test_export_pdf_returns_a_real_pdf(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_completed_ledger(), str(directory))

    response = client.get("/ledger/m-happy/export.pdf")

    assert response.status_code == 200
    assert response.content[:5] == b"%PDF-"


def test_export_of_a_blocked_run_reports_blocked(client, ledger_dir):
    directory, _ = ledger_dir
    save_ledger(_blocked_ledger(), str(directory))

    packet = json.loads(client.get("/ledger/m-blocked/export.json").content)

    assert packet["outcome"] == "BLOCKED_AT_GATE"
    assert packet["session"] is None
