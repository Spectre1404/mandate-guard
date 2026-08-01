"""Evidence export — SPEC §8.

Packet assembly and HTML rendering are pure and tested offline; the PDF test
drives a real Chromium. The narrative writer is always injected.

What matters most here: the packet must never contain a credential, must never
claim to be a CE 3.0 packet, must report a missing stage as missing rather than
inventing one, and must show a broken chain as broken.
"""

import json
import os

import pytest

from backend.export.evidence import (
    FALLBACK_NARRATIVE,
    NARRATIVE_LABEL,
    build_evidence,
    write_narrative,
)
from backend.export.pdf import export_packet, html_to_pdf
from backend.export.render import render_evidence_html
from backend.ledger.chain import append_event, new_ledger

MANDATE_HASH = "f" * 64


def _ledger_with(mandate, proposal, *, blocked=False, screenshot=None):
    ledger = new_ledger(MANDATE_HASH)
    stamp = "2026-08-01T12:00:00Z"
    append_event(ledger, "MANDATE_CREATED", {"mandate": mandate}, ts=stamp)
    append_event(ledger, "MANDATE_CONFIRMED", {"confirmed_at": stamp}, ts=stamp)
    append_event(ledger, "AGENT_PROPOSAL", {"proposal": proposal}, ts=stamp)

    gate_results = [
        {"rule_id": "R1", "name": "merchant_match", "pass": True, "expected": "x", "actual": "x"},
        {
            "rule_id": "R3",
            "name": "unit_price_cap",
            "pass": not blocked,
            "expected": "<=14.00",
            "actual": "16.90" if blocked else "12.50",
        },
    ]
    append_event(
        ledger,
        "GATE_VERDICT",
        {
            "verdict": "FAIL" if blocked else "PASS",
            "results": gate_results,
            "failed_rule_ids": ["R3"] if blocked else [],
            "proposal_id": proposal["proposal_id"],
        },
        ts=stamp,
    )
    if blocked:
        append_event(
            ledger,
            "GATE_BLOCKED",
            {"failed_rule_ids": ["R3"], "reason": "no payment session was created"},
            ts=stamp,
        )
        return ledger

    append_event(
        ledger,
        "SESSION_CREATED",
        {
            "session_id": "ses_01TEST",
            "order_id": "ord_01TEST",
            "expires_at": "2026-08-01T12:15:00Z",
            "external_order_ref": f"{MANDATE_HASH}.01",
            "attempt": 1,
        },
        ts=stamp,
    )
    append_event(ledger, "APPROVAL_OBSERVED", {"status": "awaiting_result"}, ts=stamp)
    append_event(
        ledger,
        "CREDENTIALS_RECEIVED",
        {
            "txn_ref_id": "tli_01TEST",
            "token_last4": "9563",
            "expiry_month": "12",
            "expiry_year": "2027",
        },
        ts=stamp,
    )
    append_event(
        ledger,
        "EXECUTION_PRECHECK",
        {
            "verdict": "PASS",
            "results": [
                {
                    "rule_id": "E1",
                    "name": "page_total_matches_session",
                    "pass": True,
                    "expected": "27.00",
                    "actual": "27.00",
                }
            ],
            "failed_rule_ids": [],
            "observation": {"page_total": "27.00"},
            "session_total": "27.00",
            "origin_disclosure": {
                "canonical_merchant_url": "https://beanline.example.com",
                "declared_origin": "http://127.0.0.1:8200",
                "observed_host": "127.0.0.1",
            },
        },
        ts=stamp,
    )
    append_event(
        ledger,
        "CHECKOUT_EXECUTED",
        {
            "order_number": "BL-ABCD1234",
            "authorization_code": "BL956312",
            "response_code": "00",
            "screenshot_path": screenshot or "/nonexistent/shot.png",
            "screenshot_sha256": "a" * 64,
            "confirmation_url": "http://127.0.0.1:8200/order/BL-ABCD1234",
        },
        ts=stamp,
    )
    append_event(
        ledger,
        "STATUS_REPORTED",
        {
            "txn_ref_id": "tli_01TEST",
            "txn_status": "APPROVED",
            "visa_confirmation": "SUCCESS",
            "authorization_code": "BL956312",
            "response_code": "00",
        },
        ts=stamp,
    )
    append_event(ledger, "MANDATE_CONSUMED", {"order_number": "BL-ABCD1234"}, ts=stamp)
    return ledger


@pytest.fixture
def completed(mandate, proposal):
    return _ledger_with(mandate, proposal)


@pytest.fixture
def blocked(mandate, proposal):
    return _ledger_with(mandate, proposal, blocked=True)


# --- assembly ---------------------------------------------------------------


def test_completed_packet_reports_a_completed_outcome(completed):
    evidence = build_evidence(completed)

    assert evidence["outcome"] == "COMPLETED"
    assert evidence["cover"]["visa_confirmation"] == "SUCCESS"
    assert evidence["cover"]["order_number"] == "BL-ABCD1234"
    assert evidence["cover"]["chain_valid"]


def test_blocked_packet_reports_blocked_and_has_no_session(blocked):
    evidence = build_evidence(blocked)

    assert evidence["outcome"] == "BLOCKED_AT_GATE"
    assert evidence["session"] is None
    assert evidence["credentials"] is None
    assert evidence["checkout"] is None
    assert evidence["gate_blocked"]["failed_rule_ids"] == ["R3"]


def test_packet_carries_every_section_in_causal_order(completed):
    evidence = build_evidence(completed)

    for key in ("mandate", "proposal", "gate", "session", "credentials", "precheck",
                "checkout", "report", "chain"):
        assert evidence[key] is not None, key
    assert [e["type"] for e in evidence["events"]][0] == "MANDATE_CREATED"


def test_external_order_ref_is_carried_into_the_packet(completed):
    assert build_evidence(completed)["session"]["external_order_ref"] == f"{MANDATE_HASH}.01"


def test_a_broken_chain_is_reported_as_broken(completed):
    completed["events"][2]["payload"]["proposal"]["proposed_total"] = "1.00"

    evidence = build_evidence(completed)

    assert not evidence["cover"]["chain_valid"]
    assert not evidence["chain"]["valid"]


# --- narrative --------------------------------------------------------------


def test_narrative_is_labeled_as_ai_generated(completed):
    evidence = build_evidence(completed)

    assert "AI-generated" in evidence["narrative_label"]
    assert NARRATIVE_LABEL == evidence["narrative_label"]


def test_narrative_falls_back_when_no_writer(completed):
    assert write_narrative(build_evidence(completed), None) == FALLBACK_NARRATIVE


def test_narrative_falls_back_when_the_model_fails(completed):
    def broken(_evidence):
        raise RuntimeError("model down")

    assert write_narrative(build_evidence(completed), broken) == FALLBACK_NARRATIVE


def test_injected_narrative_is_used(completed):
    text = write_narrative(build_evidence(completed), lambda e: "The purchase completed.")

    assert text == "The purchase completed."


def test_the_narrative_writer_cannot_change_the_facts(completed):
    """Generated prose is commentary; the record underneath is untouched."""
    evidence = build_evidence(completed)
    evidence["narrative"] = write_narrative(evidence, lambda e: "Nothing was ever purchased.")

    assert evidence["cover"]["visa_confirmation"] == "SUCCESS"
    assert evidence["report"]["txn_status"] == "APPROVED"


# --- rendering --------------------------------------------------------------


def test_html_contains_every_required_section(completed):
    evidence = build_evidence(completed)
    evidence["narrative"] = "Summary text."

    html = render_evidence_html(evidence)

    for heading in (
        "1. Summary",
        "2. Mandate",
        "3. Agent proposal",
        "4. Verification gate",
        "5. Payment session",
        "6. Credential issuance",
        "7. Point-of-sale re-verification",
        "8. Checkout proof",
        "9. Outcome reported",
        "10. Chain verification",
    ):
        assert heading in html, heading
    assert NARRATIVE_LABEL in html
    assert "AI-GENERATED" in html


def test_html_maps_to_ce3_categories_without_claiming_to_be_one(completed):
    evidence = build_evidence(completed)
    evidence["narrative"] = "x"

    html = render_evidence_html(evidence)

    assert "mapped to the evidence categories issuers evaluate under frameworks like Visa CE 3.0" in html
    for overclaim in ("is a CE 3.0", "CE 3.0 compliant", "CE3.0 packet", "certified"):
        assert overclaim not in html


def test_html_shows_the_masked_token_only(completed):
    evidence = build_evidence(completed)
    evidence["narrative"] = "x"

    html = render_evidence_html(evidence)

    assert "9563" in html
    assert "•••• •••• ••••" in html
    for forbidden in ("dynamic_cvv", "_token", "session_token"):
        assert forbidden not in html


def test_a_verified_chain_is_coloured_as_a_pass_not_a_failure(completed):
    """A packet claiming success in the colour of failure is worse than no colour.

    The first version of this renderer only knew PASS/SUCCESS, so 'VERIFIED' and
    every 'VALID' chain link printed red on an intact chain.
    """
    evidence = build_evidence(completed)
    evidence["narrative"] = "x"

    html = render_evidence_html(evidence)

    assert '<span class="pass">VERIFIED</span>' in html
    assert '<span class="fail">VERIFIED</span>' not in html
    assert '<span class="fail">VALID</span>' not in html
    assert html.count('<span class="pass">VALID</span>') == len(evidence["chain"]["links"])


def test_a_broken_chain_is_coloured_as_a_failure(completed):
    completed["events"][2]["payload"]["proposal"]["proposed_total"] = "1.00"
    evidence = build_evidence(completed)
    evidence["narrative"] = "x"

    html = render_evidence_html(evidence)

    assert '<span class="fail">BROKEN</span>' in html
    assert '<span class="pass">BROKEN</span>' not in html


def test_a_failed_rule_row_is_coloured_as_a_failure(blocked):
    evidence = build_evidence(blocked)
    evidence["narrative"] = "x"

    html = render_evidence_html(evidence)

    assert '<span class="fail">FAIL</span>' in html
    assert '<span class="pass">FAIL</span>' not in html


def test_blocked_html_says_no_session_was_created(blocked):
    evidence = build_evidence(blocked)
    evidence["narrative"] = "x"

    html = render_evidence_html(evidence)

    assert "No payment session was created" in html
    assert "BLOCKED" in html


def test_html_reports_a_missing_screenshot_rather_than_faking_one(completed):
    evidence = build_evidence(completed)
    evidence["narrative"] = "x"

    html = render_evidence_html(evidence)

    assert "Screenshot file not available" in html


# --- PDF --------------------------------------------------------------------


def test_export_writes_a_real_pdf_and_matching_json(completed, tmp_path):
    result = export_packet(
        completed, str(tmp_path), narrative_writer=lambda e: "Two sentence summary."
    )

    assert os.path.exists(result["pdf_path"])
    with open(result["pdf_path"], "rb") as handle:
        assert handle.read(5) == b"%PDF-"
    assert os.path.getsize(result["pdf_path"]) > 5000

    with open(result["json_path"]) as handle:
        packet = json.load(handle)
    assert packet["outcome"] == "COMPLETED"
    assert packet["narrative"] == "Two sentence summary."


def test_json_and_pdf_are_built_from_the_same_evidence(completed, tmp_path):
    result = export_packet(completed, str(tmp_path), narrative_writer=lambda e: "n")

    with open(result["json_path"]) as handle:
        packet = json.load(handle)
    assert packet["cover"]["order_number"] == result["evidence"]["cover"]["order_number"]
    assert packet["chain"]["valid"] == result["evidence"]["chain"]["valid"]


def test_exported_json_carries_no_credential(completed, tmp_path):
    result = export_packet(completed, str(tmp_path), narrative_writer=lambda e: "n")

    with open(result["json_path"]) as handle:
        raw = handle.read()
    for forbidden in ('"token"', '"dynamic_cvv"', '"_token"', '"session_token"'):
        assert forbidden not in raw
    assert '"token_last4": "9563"' in raw


def test_the_screenshot_is_inlined_in_the_pdf_when_present(completed, tmp_path):
    shot = tmp_path / "shot.png"
    # Smallest valid PNG.
    shot.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
        b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
        b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    completed["events"][-3]["payload"]["screenshot_path"] = str(shot)

    result = export_packet(completed, str(tmp_path), narrative_writer=lambda e: "n")

    assert result["evidence"]["checkout"]["screenshot_data_uri"].startswith("data:image/png;base64,")
    with open(result["json_path"]) as handle:
        assert "screenshot_data_uri" not in handle.read()


def test_blocked_run_also_exports(blocked, tmp_path):
    """A blocked purchase is exactly the case someone will want evidence of."""
    result = export_packet(blocked, str(tmp_path), narrative_writer=lambda e: "Blocked.")

    with open(result["pdf_path"], "rb") as handle:
        assert handle.read(5) == b"%PDF-"
    with open(result["json_path"]) as handle:
        assert json.load(handle)["outcome"] == "BLOCKED_AT_GATE"


def test_html_to_pdf_creates_missing_directories(tmp_path):
    target = tmp_path / "nested" / "deep" / "out.pdf"

    html_to_pdf("<h1>hi</h1>", str(target))

    assert target.exists()
