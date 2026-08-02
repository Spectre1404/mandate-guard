"""Server-safe demo runs for the hosted deployment.

Deliberately does NOT import `backend.demo`: that module pulls in Playwright and a
TestClient transport, neither of which may exist on the hosted image. Nothing here
launches a browser, starts a server, or talks to Prava. A run stops at the gate
verdict, because the hosted demo has no payment leg — that is what the video is for.

Two entry points:

  * `run_demo_request`  — the fixed demo mandate, run against the live store.
  * `run_user_request`  — a visitor's own sentence, through the full fence:
                          model extraction → validator → mandate → agent → gate.

Both take a **catalog snapshot** captured once at click time and thread it through.
Concurrent visitors therefore each see a consistent store, even if one of them
flips the drift toggle mid-run.
"""

import copy
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.request import urlopen

from backend.agent.shopper import ShoppingError, propose
from backend.compiler.mandate import build_draft, confirm
from backend.compiler.validate import ValidationError, validate_constraints
from backend.gate.verdict import evaluate
from backend.ledger.chain import append_event, new_ledger
from backend.ledger.store import save_ledger
from backend.orchestrator import GateBlocked, Orchestrator

DEMO_INTENT = "Buy a bag of house blend and two boxes of filters from Beanline, under $30."
MAX_INTENT_CHARS = 400
AGENT_MODEL = "gpt-5-mini"


# --- catalog snapshot --------------------------------------------------------


def catalog_snapshot(storefront_url=None, timeout=4):
    """The live store's catalogue, captured once, as a private copy.

    Fetched over HTTP when a storefront URL is configured, because the hosted
    dashboard and the hosted store are separate processes and cannot share the
    drift toggle in memory. Falls back to the in-process catalogue so local runs
    and tests need no server.

    Returns `(catalog, drift_mode, source)`. The catalogue is deep-copied, so a
    later toggle cannot mutate a run that is already underway.
    """
    if storefront_url:
        try:
            with urlopen(f"{storefront_url.rstrip('/')}/_catalog.json", timeout=timeout) as fh:
                payload = json.loads(fh.read().decode("utf-8"))
            return copy.deepcopy(payload["products"]), payload.get("drift", "none"), "live"
        except Exception:
            pass  # fall through to the local catalogue rather than failing the run

    from storefront import catalog as catalog_module

    return copy.deepcopy(catalog_module.catalog()), catalog_module.drift_mode(), "local"


# --- mandate construction ----------------------------------------------------


def demo_mandate(now=None):
    now = now or datetime.now(timezone.utc)
    constraints = validate_constraints(
        {
            "merchant": {
                "name": "Beanline Coffee",
                "url": "https://beanline.example.com",
                "country_code_iso2": "US",
            },
            "items": [
                {"product_id": "BL-HOUSE-12", "description": "Beanline House Blend 12oz",
                 "max_unit_price": "14.00", "quantity": 1},
                {"product_id": "BL-FILTER-100", "description": "Paper Filters 100ct",
                 "max_unit_price": "8.00", "quantity": 2},
            ],
            "price_ceiling_total": "30.00",
            "currency": "USD",
            "effective_minutes": 15,
            "substitution_policy": "none",
        }
    )
    draft = build_draft(
        user={"user_id": "hosted_demo", "user_email": "demo@mandate-guard.example"},
        intent_text=DEMO_INTENT,
        constraints=constraints,
        created_at=now,
    )
    return confirm(draft, confirmed_at=now), now


# --- the shared tail: agent -> gate -> ledger --------------------------------


def _gate_and_record(mandate, catalog, now, ledger_dir, export_dir):
    """Propose against the snapshot, run the gate, write the ledger. JSON only."""
    proposal = propose(mandate, catalog, created_at=now, model="deterministic")

    orchestrator = Orchestrator(client=None, executor_factory=None)
    ledger = orchestrator.open_ledger(mandate)

    blocked = None
    try:
        verdict = orchestrator.verify(ledger, mandate, proposal, now)
    except GateBlocked as exc:
        blocked = exc
        verdict = exc.verdict

    ledger_path = save_ledger(ledger, ledger_dir) if ledger_dir else None
    export_path = None
    if export_dir:
        from backend.export.pdf import export_json_only

        export_path = export_json_only(ledger, export_dir, basename=_ledger_id(ledger_path))

    return {
        "proposal": proposal,
        "verdict": verdict,
        "blocked": blocked is not None,
        "ledger": ledger,
        "ledger_id": _ledger_id(ledger_path),
        "ledger_path": ledger_path,
        "export_path": export_path,
    }


def _ledger_id(path):
    from backend.ledger.store import LEDGER_SUFFIX

    return os.path.basename(path or "").replace(LEDGER_SUFFIX, "") or None


def run_demo_request(ledger_dir, export_dir, storefront_url=None, now=None):
    """The built-in demo mandate against the live store.

    With the drift toggle off the gate passes; with it on the agent genuinely reads
    the perturbed price and the gate genuinely refuses. Nothing is staged either way.
    """
    catalog, drift, source = catalog_snapshot(storefront_url)
    mandate, now = demo_mandate(now)
    result = _gate_and_record(mandate, catalog, now, ledger_dir, export_dir)
    result.update(mandate=mandate, drift=drift, catalog_source=source, intent_text=DEMO_INTENT)
    return result


# --- "try your own request" --------------------------------------------------


class RequestTooLong(ValueError):
    pass


def run_user_request(
    intent_text, ledger_dir, export_dir, extractor=None, storefront_url=None, now=None
):
    """A visitor's sentence, through the whole fence, with each stage returned.

    A rejected input is a **result**, not an error: the validator's named reasons
    are the interesting output, because they are the fence doing its job. The
    caller renders raw extraction, validator outcome, and final mandate together.
    """
    intent_text = (intent_text or "").strip()
    if not intent_text:
        raise RequestTooLong("say what you want to buy")
    if len(intent_text) > MAX_INTENT_CHARS:
        raise RequestTooLong(
            f"keep it under {MAX_INTENT_CHARS} characters (got {len(intent_text)})"
        )

    catalog, drift, source = catalog_snapshot(storefront_url)
    now = now or datetime.now(timezone.utc)

    stages = {
        "intent_text": intent_text,
        "drift": drift,
        "catalog_source": source,
        "catalog": catalog,
        "raw_extraction": None,
        "extraction_error": None,
        "accepted": False,
        "validation_errors": None,
        "mandate": None,
        "proposal": None,
        "verdict": None,
        "blocked": False,
        "ledger_id": None,
        "agent_error": None,
    }

    merchant = {
        "name": "Beanline Coffee",
        "url": "https://beanline.example.com",
        "country_code_iso2": "US",
    }
    catalog_for_prompt = [
        {"product_id": p["product_id"], "name": p["name"], "price": p["price"]}
        for p in catalog.values()
    ]

    from backend.compiler.extract import ExtractionError, OpenAIExtractor, extract_constraints

    try:
        stages["raw_extraction"] = extract_constraints(
            intent_text,
            merchant,
            catalog_for_prompt,
            extractor=extractor or OpenAIExtractor(model=AGENT_MODEL),
        )
    except ExtractionError as exc:
        stages["extraction_error"] = str(exc)
        return stages
    except Exception as exc:  # a model outage is a result, not a stack trace
        stages["extraction_error"] = f"the model was unavailable: {exc.__class__.__name__}"
        return stages

    try:
        constraints = validate_constraints(stages["raw_extraction"])
    except ValidationError as exc:
        stages["validation_errors"] = dict(exc.details)
        return stages

    stages["accepted"] = True
    mandate = confirm(
        build_draft(
            user={"user_id": "hosted_visitor", "user_email": "visitor@mandate-guard.example"},
            intent_text=intent_text,
            constraints=constraints,
            created_at=now,
        ),
        confirmed_at=now,
    )
    stages["mandate"] = mandate

    try:
        result = _gate_and_record(mandate, catalog, now, ledger_dir, export_dir)
    except ShoppingError as exc:
        stages["agent_error"] = str(exc)
        return stages

    stages.update(
        proposal=result["proposal"],
        verdict=result["verdict"],
        blocked=result["blocked"],
        ledger_id=result["ledger_id"],
    )
    return stages


# --- tamper demonstration ----------------------------------------------------

TAMPER_MARKER = "tampered_demo"


def tamper_copy(ledger, ledger_dir):
    """Write a FORGED COPY of a ledger. Never mutates the original or the seeds.

    The copy is marked so the UI can label it, and the forgery is a real payload
    edit, so recomputing the chain genuinely fails at that link.
    """
    forged = copy.deepcopy(ledger)
    forged[TAMPER_MARKER] = True

    target = None
    for event in forged["events"]:
        if event["type"] == "CHECKOUT_EXECUTED":
            event["payload"]["order_number"] = "BL-FORGED1"
            target = event["type"]
            break
    if target is None:
        # No checkout to forge -- edit the last payload instead. Any payload change
        # breaks that event's hash, which is the point of the demonstration.
        forged["events"][-1]["payload"]["forged_by_visitor"] = True
        target = forged["events"][-1]["type"]

    ledger_id = f"tampered-{uuid.uuid4().hex[:8]}"
    os.makedirs(ledger_dir, exist_ok=True)
    from backend.ledger.store import LEDGER_SUFFIX

    path = os.path.join(ledger_dir, f"{ledger_id}{LEDGER_SUFFIX}")
    with open(path, "w") as fh:
        json.dump(forged, fh, indent=2, sort_keys=True)

    return ledger_id, target
