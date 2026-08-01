"""Ledger persistence: one JSON file per run.

Deliberately boring. The ledger is a plain dict, so a file per run is enough for
the demo and the browser, and it keeps the chain logic storage-agnostic (SPEC §4
names Supabase Postgres for the real deployment; the same `{mandate_hash, events}`
shape goes into a row without the hashing rules changing).

Files are named by mandate id so a run is findable, and the directory listing is
sorted newest-first by the first event's timestamp — what the browser wants.
"""

import json
import os

LEDGER_SUFFIX = ".ledger.json"


def ledger_filename(ledger):
    mandate_id = None
    for event in ledger["events"]:
        if event.get("mandate_id"):
            mandate_id = event["mandate_id"]
            break
    return f"{mandate_id or ledger['mandate_hash'][:16]}{LEDGER_SUFFIX}"


def save_ledger(ledger, directory):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, ledger_filename(ledger))
    with open(path, "w") as handle:
        json.dump(ledger, handle, indent=2, sort_keys=True)
    return path


def load_ledger(path):
    with open(path) as handle:
        return json.load(handle)


def list_ledgers(directory):
    """Summaries for the browser index, newest first."""
    if not os.path.isdir(directory):
        return []

    summaries = []
    for name in os.listdir(directory):
        if not name.endswith(LEDGER_SUFFIX):
            continue
        path = os.path.join(directory, name)
        try:
            ledger = load_ledger(path)
        except (json.JSONDecodeError, OSError):
            continue
        summaries.append(summarize(ledger, path))

    return sorted(summaries, key=lambda s: s["started_at"] or "", reverse=True)


def summarize(ledger, path=None):
    events = ledger["events"]
    types = [event["type"] for event in events]

    def payload_of(event_type):
        for event in events:
            if event["type"] == event_type:
                return event["payload"]
        return None

    mandate = (payload_of("MANDATE_CREATED") or {}).get("mandate") or {}
    report = payload_of("STATUS_REPORTED") or {}
    checkout = payload_of("CHECKOUT_EXECUTED") or {}
    gate = payload_of("GATE_VERDICT") or {}

    return {
        "ledger_id": os.path.basename(path).replace(LEDGER_SUFFIX, "") if path else None,
        "path": path,
        "mandate_hash": ledger["mandate_hash"],
        "mandate_id": mandate.get("mandate_id"),
        "intent_text": mandate.get("intent_text"),
        "started_at": events[0]["ts"] if events else None,
        "event_count": len(events),
        "gate_verdict": gate.get("verdict"),
        "failed_rule_ids": gate.get("failed_rule_ids") or [],
        "blocked": "GATE_BLOCKED" in types,
        "order_number": checkout.get("order_number"),
        "visa_confirmation": report.get("visa_confirmation"),
    }
