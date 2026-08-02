"""Hosted demo surfaces — headless runs, snapshot isolation, tamper safety, limiter.

Everything here runs offline: the extractor is always injected, and no test may
reach OpenAI or Prava. The properties that matter on a public URL:

  * a run captures the catalogue ONCE, so a concurrent drift toggle cannot change
    a run that is already underway
  * tampering writes a COPY and never touches the original or the seeds
  * the limiter is shared across every endpoint that does work
"""

import copy
import json
import os

import pytest
from fastapi.testclient import TestClient

from backend.demo_headless import (
    DEMO_INTENT,
    MAX_INTENT_CHARS,
    RequestTooLong,
    catalog_snapshot,
    run_demo_request,
    run_user_request,
    tamper_copy,
)
from backend.ledger.chain import verify_chain
from backend.ledger.store import LEDGER_SUFFIX, load_ledger, save_ledger
from storefront import catalog as catalog_module

WELL_FORMED = {
    "merchant": {
        "name": "Beanline Coffee",
        "url": "https://beanline.example.com",
        "country_code_iso2": "US",
    },
    "items": [
        {"product_id": "BL-HOUSE-12", "description": "Beanline House Blend 12oz",
         "max_unit_price": "14.00", "quantity": 1},
    ],
    "price_ceiling_total": "14.00",
    "currency": "USD",
    "effective_minutes": 15,
    "substitution_policy": "none",
}


def stub(payload):
    return lambda system, user: copy.deepcopy(payload)


@pytest.fixture(autouse=True)
def reset_drift():
    catalog_module.set_drift("none")
    yield
    catalog_module.set_drift("none")


@pytest.fixture
def dirs(tmp_path):
    return str(tmp_path / "ledgers"), str(tmp_path / "exports")


# --- catalogue snapshot -----------------------------------------------------


def test_snapshot_is_a_private_copy(dirs):
    catalog, drift, source = catalog_snapshot()

    catalog["BL-HOUSE-12"]["price"] = "99.99"

    assert catalog_module.catalog()["BL-HOUSE-12"]["price"] == "12.50"
    assert drift == "none"
    assert source == "local"


def test_a_drift_toggle_mid_run_cannot_change_a_snapshot_already_taken():
    """Race safety: two visitors, one flips drift, the other's run is unaffected."""
    before, _, _ = catalog_snapshot()

    catalog_module.set_drift("price_hike")
    after, drift_after, _ = catalog_snapshot()

    assert before["BL-HOUSE-12"]["price"] == "12.50"
    assert after["BL-HOUSE-12"]["price"] == "16.90"
    assert drift_after == "price_hike"


# --- demo run ---------------------------------------------------------------


def test_demo_run_passes_the_gate_with_no_drift(dirs):
    ledger_dir, export_dir = dirs

    result = run_demo_request(ledger_dir, export_dir)

    assert result["verdict"]["verdict"] == "PASS"
    assert result["blocked"] is False
    assert verify_chain(result["ledger"])["valid"]
    assert os.path.exists(result["ledger_path"])
    assert result["export_path"].endswith(".json")


def test_demo_run_is_blocked_when_the_store_drifts(dirs):
    ledger_dir, export_dir = dirs
    catalog_module.set_drift("price_hike")

    result = run_demo_request(ledger_dir, export_dir)

    assert result["blocked"] is True
    assert {"R3", "R5"} <= set(result["verdict"]["failed_rule_ids"])
    types = [e["type"] for e in result["ledger"]["events"]]
    assert types[-1] == "GATE_BLOCKED"


def test_a_hosted_run_never_creates_a_session_or_touches_prava(dirs):
    ledger_dir, export_dir = dirs

    result = run_demo_request(ledger_dir, export_dir)
    types = [e["type"] for e in result["ledger"]["events"]]

    for forbidden in ("SESSION_CREATED", "CREDENTIALS_RECEIVED", "STATUS_REPORTED"):
        assert forbidden not in types
    assert result["intent_text"] == DEMO_INTENT


def test_hosted_export_is_json_only(dirs):
    ledger_dir, export_dir = dirs

    result = run_demo_request(ledger_dir, export_dir)

    assert not any(f.endswith(".pdf") for f in os.listdir(export_dir))
    with open(result["export_path"]) as fh:
        assert json.load(fh)["chain"]["valid"]


# --- try your own request ---------------------------------------------------


def test_a_valid_request_reaches_the_gate(dirs):
    ledger_dir, export_dir = dirs

    stages = run_user_request("Buy one house blend", ledger_dir, export_dir,
                              extractor=stub(WELL_FORMED))

    assert stages["accepted"]
    assert stages["validation_errors"] is None
    assert stages["verdict"]["verdict"] == "PASS"
    assert stages["ledger_id"]


def test_a_rejected_request_is_a_first_class_result_not_an_error(dirs):
    """The validator's named reasons ARE the interesting output."""
    ledger_dir, export_dir = dirs
    bad = dict(WELL_FORMED, currency="XYZ", price_ceiling_total="1.005")

    stages = run_user_request("Buy one house blend", ledger_dir, export_dir,
                              extractor=stub(bad))

    assert stages["accepted"] is False
    assert set(stages["validation_errors"]) >= {"currency", "price_ceiling_total"}
    assert stages["mandate"] is None
    assert stages["ledger_id"] is None
    # Nothing was written: a rejected extraction never becomes an authorization.
    assert not os.path.isdir(ledger_dir) or not os.listdir(ledger_dir)


def test_the_raw_model_output_is_shown_even_when_rejected(dirs):
    ledger_dir, export_dir = dirs
    bad = dict(WELL_FORMED, currency="XYZ")

    stages = run_user_request("anything", ledger_dir, export_dir, extractor=stub(bad))

    assert stages["raw_extraction"]["currency"] == "XYZ"


def test_a_model_outage_is_reported_not_raised(dirs):
    ledger_dir, export_dir = dirs

    def broken(system, user):
        raise RuntimeError("upstream down")

    stages = run_user_request("anything", ledger_dir, export_dir, extractor=broken)

    assert stages["extraction_error"]
    assert stages["accepted"] is False


def test_over_long_input_is_refused_before_any_model_call(dirs):
    ledger_dir, export_dir = dirs
    calls = []

    with pytest.raises(RequestTooLong):
        run_user_request(
            "x" * (MAX_INTENT_CHARS + 1), ledger_dir, export_dir,
            extractor=lambda s, u: calls.append(1),
        )

    assert calls == []


def test_empty_input_is_refused(dirs):
    ledger_dir, export_dir = dirs

    with pytest.raises(RequestTooLong):
        run_user_request("   ", ledger_dir, export_dir, extractor=stub(WELL_FORMED))


def test_a_user_request_runs_against_the_drifted_store(dirs):
    """A judge flips drift, then submits their own sentence, and the gate catches it."""
    ledger_dir, export_dir = dirs
    catalog_module.set_drift("price_hike")

    stages = run_user_request("Buy one house blend under $14", ledger_dir, export_dir,
                              extractor=stub(WELL_FORMED))

    assert stages["drift"] == "price_hike"
    assert stages["verdict"]["verdict"] == "FAIL"
    assert "R3" in stages["verdict"]["failed_rule_ids"]


# --- tamper safety ----------------------------------------------------------


def test_tamper_writes_a_copy_and_leaves_the_original_untouched(dirs, tmp_path):
    ledger_dir, export_dir = dirs
    original = run_demo_request(ledger_dir, export_dir)["ledger"]
    original_path = save_ledger(original, ledger_dir)
    before = open(original_path).read()

    new_id, _ = tamper_copy(load_ledger(original_path), ledger_dir)

    assert open(original_path).read() == before
    assert verify_chain(load_ledger(original_path))["valid"]
    assert new_id != os.path.basename(original_path).replace(LEDGER_SUFFIX, "")


def test_the_tampered_copy_actually_fails_verification(dirs):
    ledger_dir, export_dir = dirs
    result = run_demo_request(ledger_dir, export_dir)
    path = save_ledger(result["ledger"], ledger_dir)

    new_id, _ = tamper_copy(load_ledger(path), ledger_dir)
    forged = load_ledger(os.path.join(ledger_dir, f"{new_id}{LEDGER_SUFFIX}"))

    assert not verify_chain(forged)["valid"]
    assert forged["tampered_demo"] is True


def test_tamper_never_touches_the_seed_directory(dirs, tmp_path):
    """Seeds are copied into the runtime dir; the originals must stay pristine."""
    ledger_dir, export_dir = dirs
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    seeded = run_demo_request(str(seed_dir), export_dir)["ledger"]
    seed_path = save_ledger(seeded, str(seed_dir))
    before = open(seed_path).read()

    tamper_copy(load_ledger(seed_path), ledger_dir)

    assert open(seed_path).read() == before
    assert verify_chain(load_ledger(seed_path))["valid"]


# --- the HTTP surface -------------------------------------------------------


@pytest.fixture
def ui(tmp_path, monkeypatch):
    monkeypatch.setenv("MANDATE_GUARD_LEDGER_DIR", str(tmp_path / "ledgers"))
    monkeypatch.setenv("MANDATE_GUARD_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("MANDATE_GUARD_SEED_DIR", str(tmp_path / "seeds"))
    monkeypatch.setenv("MANDATE_GUARD_HOSTED", "1")

    import importlib

    import ledger_ui.app as module

    importlib.reload(module)
    module._HITS.clear()
    return module


@pytest.fixture
def client(ui):
    with TestClient(ui.app) as c:
        yield c


def test_seeds_are_restored_on_startup(tmp_path, monkeypatch):
    seed_dir = tmp_path / "seeds"
    seed_dir.mkdir()
    ledger_dir = tmp_path / "ledgers"
    monkeypatch.setenv("MANDATE_GUARD_LEDGER_DIR", str(ledger_dir))
    monkeypatch.setenv("MANDATE_GUARD_SEED_DIR", str(seed_dir))

    import importlib

    import ledger_ui.app as module

    importlib.reload(module)
    result = run_demo_request(str(seed_dir), str(tmp_path / "exports"))
    save_ledger(result["ledger"], str(seed_dir))

    restored = module.restore_seeds()

    assert restored
    assert os.listdir(str(ledger_dir))


def test_hosted_banner_appears_on_every_page(client):
    for path in ("/mandates", "/", "/try"):
        assert 'id="hosted-banner"' in client.get(path).text, path


def test_og_tags_are_present(client):
    body = client.get("/mandates").text

    assert 'property="og:title"' in body
    assert 'property="og:description"' in body
    assert 'name="twitter:card"' in body


def test_the_limiter_is_shared_across_endpoints(client, ui):
    """Ten requests total, not ten per endpoint."""
    for _ in range(ui.RATE_LIMIT_PER_MINUTE):
        client.post("/demo/run", follow_redirects=False)

    assert "Easy there" in client.post("/demo/run", follow_redirects=True).text
    assert "Easy there" in client.post("/try", data={"intent_text": "x"},
                                       follow_redirects=True).text


def test_the_try_form_renders(client):
    body = client.get("/try").text

    assert 'id="try-form"' in body
    assert 'id="intent"' in body


def test_run_demo_redirects_to_the_new_chain(client):
    response = client.post("/demo/run", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/ledger/")


def test_tamper_button_creates_a_copy_and_redirects(client, ui):
    client.post("/demo/run", follow_redirects=False)
    ledger_id = os.listdir(ui.LEDGER_DIR)[0].replace(LEDGER_SUFFIX, "")

    response = client.post(f"/ledger/{ledger_id}/tamper", follow_redirects=False)

    assert response.status_code == 303
    assert "tampered-" in response.headers["location"]
    assert verify_chain(load_ledger(os.path.join(ui.LEDGER_DIR,
                                                 f"{ledger_id}{LEDGER_SUFFIX}")))["valid"]


def test_a_tampered_copy_is_labelled_in_the_ui(client, ui):
    client.post("/demo/run", follow_redirects=False)
    ledger_id = os.listdir(ui.LEDGER_DIR)[0].replace(LEDGER_SUFFIX, "")
    location = client.post(f"/ledger/{ledger_id}/tamper",
                           follow_redirects=False).headers["location"]

    body = client.get(location).text

    assert 'id="tamper-note"' in body
    assert "deliberately" in body.lower()
    assert ">BROKEN<" in body


def test_hosted_swaps_the_pdf_button_for_the_sample_packet(client, ui):
    client.post("/demo/run", follow_redirects=False)
    ledger_id = os.listdir(ui.LEDGER_DIR)[0].replace(LEDGER_SUFFIX, "")

    body = client.get(f"/ledger/{ledger_id}").text

    assert "/sample-packet.pdf" in body
    assert "/export.pdf" not in body
