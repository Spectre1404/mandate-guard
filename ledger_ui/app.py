"""Ledger browser — SPEC §4 ("verify-chain endpoint + UI button") and §11 Act 3.

Server-rendered, same as the storefront: no build step, nothing to go wrong on
camera. Reads persisted ledgers from disk (evidence/ledgers/) and renders the
event chain with a per-link result, a verify button that recomputes every hash on
click, and export buttons for the JSON and PDF packets.

The verify button is a real recomputation, not a cached flag. Clicking it re-reads
the ledger from disk and rehashes every event, so a file tampered with between
page loads turns red. That is the point of the demo moment.

Element ID contract (this page is on camera in Act 3, and the demo script depends
on these):

    #ledger-list                  index table of runs
    [data-ledger-row]             one per run, with data-ledger-id
    #chain-table                  the event chain
    [data-event-row]              one per event, with data-index / data-event-type
    [data-link-status]            per-link VALID / BROKEN cell
    #verify-chain                 the verify button
    #verify-result                verdict banner after verifying
    #chain-verdict                overall VERIFIED / BROKEN text
    #export-json  #export-pdf     export buttons
    #outcome-banner               happy / blocked summary banner
    #mandate-summary              dashboard counts strip
    [data-count="<NAME>"]         individual count tile
    #mandate-list                 mandates index table
    [data-mandate-row]            one per mandate, with data-mandate-hash
    [data-status]                 derived mandate STATUS chip
    [data-last-outcome]           derived last-attempt outcome chip
    #constraints-card             mandate detail constraints
    #lifecycle-timeline           that mandate's events in causal order
    #attempt-history              per-attempt table with external_order_ref
    #derived-footer               "derived entirely from the hash-chained ledger"

The dashboard holds NO state of its own: every status and field is derived from
ledger events on disk by backend/ledger/lifecycle.py, so the view cannot disagree
with the record. The clock is injectable (CLOCK) because EXPIRED is time-derived.

Run:  .venv/bin/uvicorn ledger_ui.app:app --port 8300
"""

import os
from html import escape

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from backend.export.evidence import build_evidence, write_narrative
from backend.export.pdf import export_packet
from backend.ledger.chain import verify_chain
from backend.ledger.lifecycle import project_all, summary_counts
from backend.ledger.store import LEDGER_SUFFIX, list_ledgers, load_ledger, summarize

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_DIR = os.environ.get("MANDATE_GUARD_LEDGER_DIR", os.path.join(REPO_ROOT, "evidence", "ledgers"))
EXPORT_DIR = os.environ.get("MANDATE_GUARD_EXPORT_DIR", os.path.join(REPO_ROOT, "evidence", "exports"))

app = FastAPI(title="Mandate Guard — Ledger")


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)


# Injectable so EXPIRED, which is time-derived, is testable.
CLOCK = _utcnow


STYLE = """
  body{font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
       max-width:1080px;margin:0 auto;padding:28px 20px 64px;color:#16181d}
  h1{font-size:26px;margin:0 0 4px} h2{font-size:17px;margin:32px 0 10px}
  .sub{color:#5b6270;margin:0 0 24px}
  a{color:#16181d}
  table{width:100%;border-collapse:collapse;margin:0 0 16px;font-size:14px}
  th,td{text-align:left;padding:9px 10px;border-bottom:1px solid #e3e5ea;vertical-align:top}
  th{color:#5b6270;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
  .mono{font-family:'SF Mono',Menlo,Consolas,monospace;font-size:12px;word-break:break-all}
  .pass{color:#146c2e;font-weight:700} .fail{color:#b3261e;font-weight:700}
  .pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;font-weight:600}
  .pill.ok{background:#e8f6ec;color:#146c2e} .pill.bad{background:#fdeaea;color:#b3261e}
  .banner{padding:14px 18px;border-radius:10px;margin:0 0 22px}
  .banner.ok{background:#e8f6ec;border:1px solid #146c2e}
  .banner.bad{background:#fdeaea;border:1px solid #b3261e}
  .banner.idle{background:#f6f7f9;border:1px solid #d6d9e0;color:#5b6270}
  button{background:#16181d;color:#fff;border:0;border-radius:8px;padding:10px 18px;
         font-size:14px;cursor:pointer;font-weight:600}
  button.ghost{background:#fff;color:#16181d;border:1px solid #c9ccd4}
  .actions{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 8px}
  form{display:inline}
  td.payload{color:#5b6270;font-size:12.5px}
  .empty{background:#f6f7f9;border:1px dashed #c9ccd4;border-radius:10px;padding:28px;
         text-align:center;color:#5b6270}
  code{font-family:'SF Mono',Menlo,monospace;font-size:12.5px}
  nav{display:flex;gap:18px;margin:0 0 22px;font-size:14px}
  nav a{color:#5b6270;text-decoration:none;font-weight:600}
  nav a.on{color:#16181d;border-bottom:2px solid #16181d;padding-bottom:2px}
  .tiles{display:flex;gap:12px;flex-wrap:wrap;margin:0 0 24px}
  .tile{border:1px solid #e3e5ea;border-radius:10px;padding:12px 18px;min-width:104px}
  .tile .n{font-size:26px;font-weight:700;font-variant-numeric:tabular-nums}
  .tile .k{font-size:11px;color:#5b6270;text-transform:uppercase;letter-spacing:.05em}
  .chip{display:inline-block;padding:2px 9px;border-radius:99px;font-size:11.5px;
        font-weight:700;letter-spacing:.03em}
  .chip.ACTIVE{background:#e8f0fe;color:#174ea6}
  .chip.CONSUMED{background:#e8f6ec;color:#146c2e}
  .chip.EXPIRED{background:#f1f3f4;color:#5b6270}
  .chip.DRAFT{background:#fef7e0;color:#8a6100}
  .chip.COMPLETED{background:#e8f6ec;color:#146c2e}
  .chip.BLOCKED{background:#fdeaea;color:#b3261e}
  .chip.DECLINED{background:#fef7e0;color:#8a6100}
  .chip.NONE{background:#f1f3f4;color:#5b6270}
  .card{border:1px solid #e3e5ea;border-radius:10px;padding:4px 18px 14px;margin:0 0 22px}
  .derived{margin-top:40px;padding-top:14px;border-top:1px solid #e3e5ea;
           color:#5b6270;font-size:12.5px}
"""


DERIVED_NOTE = "This view is derived entirely from the hash-chained ledger."


def nav(active):
    def link(href, label, key):
        css = " class=\"on\"" if key == active else ""
        return f'<a href="{href}"{css}>{label}</a>'

    return (
        "<nav>"
        + link("/mandates", "Mandates", "mandates")
        + link("/", "Ledger chains", "chains")
        + "</nav>"
    )


def page(title, body, active="chains"):
    footer = f'<p class="derived" id="derived-footer">{escape(DERIVED_NOTE)}</p>'
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset='utf-8'><title>{escape(title)}</title>"
        f"<style>{STYLE}</style></head><body>{nav(active)}{body}{footer}</body></html>"
    )


def _chip(value):
    return f'<span class="chip {escape(str(value))}">{escape(str(value))}</span>'


def load_all_ledgers():
    """Every persisted ledger, as (ledger, path) pairs. The dashboard's only input."""
    pairs = []
    if not os.path.isdir(LEDGER_DIR):
        return pairs
    for name in sorted(os.listdir(LEDGER_DIR)):
        if not name.endswith(LEDGER_SUFFIX):
            continue
        path = os.path.join(LEDGER_DIR, name)
        try:
            pairs.append((load_ledger(path), path))
        except Exception:
            continue
    return pairs


def ledger_path(ledger_id):
    return os.path.join(LEDGER_DIR, f"{ledger_id}{LEDGER_SUFFIX}")


def _pill(ok, ok_text, bad_text):
    return (
        f'<span class="pill {"ok" if ok else "bad"}">{escape(ok_text if ok else bad_text)}</span>'
    )


@app.get("/", response_class=HTMLResponse)
def index():
    summaries = list_ledgers(LEDGER_DIR)
    if not summaries:
        return page(
            "Ledger browser",
            "<h1>Evidence Ledger</h1>"
            "<p class='sub'>Hash-chained record of every agent purchase attempt.</p>"
            "<div class='empty'>No runs recorded yet. Generate one:<br><br>"
            "<code>.venv/bin/python scripts/run_demo.py happy</code><br>"
            "<code>.venv/bin/python scripts/run_demo.py blocked</code></div>",
        )

    rows = "".join(
        f"""<tr data-ledger-row data-ledger-id="{escape(s['ledger_id'])}">
              <td><a href="/ledger/{escape(s['ledger_id'])}">{escape(s['started_at'] or '—')}</a></td>
              <td>{escape((s['intent_text'] or '')[:64])}</td>
              <td>{_pill(s['gate_verdict'] == 'PASS', 'GATE PASS', 'GATE FAIL')}</td>
              <td>{escape(', '.join(s['failed_rule_ids']) or '—')}</td>
              <td class="mono">{escape(s['order_number'] or '—')}</td>
              <td>{_pill(s['visa_confirmation'] == 'SUCCESS', 'SUCCESS', 'NO CHARGE')}</td>
              <td class="num">{s['event_count']}</td>
            </tr>"""
        for s in summaries
    )
    return page(
        "Ledger browser",
        "<h1>Evidence Ledger</h1>"
        "<p class='sub'>Hash-chained record of every agent purchase attempt.</p>"
        "<table id=\"ledger-list\"><tr><th>Started</th><th>Request</th><th>Gate</th>"
        "<th>Failed rules</th><th>Order</th><th>Outcome</th><th>Events</th></tr>"
        f"{rows}</table>",
    )


@app.get("/ledger/{ledger_id}", response_class=HTMLResponse)
def view_ledger(ledger_id: str, verified: int = 0):
    path = ledger_path(ledger_id)
    if not os.path.exists(path):
        return page("Not found", "<h1>No such ledger</h1><p><a href='/'>Back</a></p>")

    ledger = load_ledger(path)
    summary = summarize(ledger, path)
    # Recomputed on every render; `verified` only controls whether the result
    # banner is shown, never what it says.
    chain = verify_chain(ledger)

    if summary["blocked"]:
        banner = (
            '<div class="banner bad" id="outcome-banner">'
            "<strong>Purchase BLOCKED by the verification gate.</strong><br>"
            f"Failed rules: {escape(', '.join(summary['failed_rule_ids']))}. "
            "No payment session was created and no credential was ever issued.</div>"
        )
    elif summary["visa_confirmation"] == "SUCCESS":
        banner = (
            '<div class="banner ok" id="outcome-banner">'
            "<strong>Purchase completed and confirmed by the card network.</strong><br>"
            f"Order {escape(summary['order_number'] or '')} · "
            f"visa_confirmation SUCCESS</div>"
        )
    else:
        banner = (
            '<div class="banner idle" id="outcome-banner">'
            "Run did not reach a confirmed outcome.</div>"
        )

    verify_banner = ""
    if verified:
        verify_banner = (
            f'<div class="banner {"ok" if chain["valid"] else "bad"}" id="verify-result">'
            f'<strong>Chain re-verified: <span id="chain-verdict">'
            f'{"VERIFIED" if chain["valid"] else "BROKEN"}</span></strong> — '
            f"{len(chain['links'])} event hashes recomputed just now"
            + (
                ""
                if chain["valid"]
                else " · first break at index "
                f"{next(l['index'] for l in chain['links'] if not l['valid'])}"
            )
            + "</div>"
        )

    link_by_index = {link["index"]: link for link in chain["links"]}
    rows = "".join(
        f"""<tr data-event-row data-index="{index}" data-event-type="{escape(event['type'])}">
              <td class="num">{index}</td>
              <td><strong>{escape(event['type'])}</strong></td>
              <td class="mono">{escape(event['ts'])}</td>
              <td class="mono">{escape(event['prev_hash'][:16])}…</td>
              <td class="mono">{escape(event['event_hash'][:16])}…</td>
              <td data-link-status>{
                  _pill(link_by_index[index]['valid'], 'VALID', 'BROKEN')
              }{
                  '' if link_by_index[index]['valid']
                  else f"<div class='payload'>{escape(link_by_index[index]['reason'] or '')}</div>"
              }</td>
            </tr>"""
        for index, event in enumerate(ledger["events"])
    )

    return page(
        f"Ledger {ledger_id}",
        f"""<h1>Evidence chain</h1>
        <p class="sub"><a href="/">&larr; All runs</a> · anchored to mandate hash
        <span class="mono">{escape(ledger['mandate_hash'])}</span></p>
        {banner}{verify_banner}
        <div class="actions">
          <form method="post" action="/ledger/{escape(ledger_id)}/verify">
            <button type="submit" id="verify-chain">Verify chain</button>
          </form>
          <a href="/ledger/{escape(ledger_id)}/export.json">
            <button type="button" class="ghost" id="export-json">Export JSON</button></a>
          <a href="/ledger/{escape(ledger_id)}/export.pdf">
            <button type="button" class="ghost" id="export-pdf">Export PDF</button></a>
        </div>
        <h2>{len(ledger['events'])} events</h2>
        <table id="chain-table"><tr><th>#</th><th>Event</th><th>Timestamp</th>
        <th>Prev hash</th><th>Event hash</th><th>Link</th></tr>{rows}</table>""",
    )


@app.post("/ledger/{ledger_id}/verify")
def verify(ledger_id: str):
    """Recompute on click. Re-reads from disk, so tampering between loads shows."""
    return RedirectResponse(f"/ledger/{ledger_id}?verified=1", status_code=303)


@app.get("/ledger/{ledger_id}/export.json")
def export_json(ledger_id: str):
    path = ledger_path(ledger_id)
    if not os.path.exists(path):
        return page("Not found", "<h1>No such ledger</h1>")

    ledger = load_ledger(path)
    evidence = build_evidence(ledger)
    evidence["narrative"] = write_narrative(evidence, None)

    os.makedirs(EXPORT_DIR, exist_ok=True)
    out = os.path.join(EXPORT_DIR, f"{ledger_id}.json")
    import json

    with open(out, "w") as handle:
        json.dump(evidence, handle, indent=2, sort_keys=True)
    return FileResponse(out, media_type="application/json", filename=f"{ledger_id}.json")


@app.get("/ledger/{ledger_id}/export.pdf")
def export_pdf(ledger_id: str):
    path = ledger_path(ledger_id)
    if not os.path.exists(path):
        return page("Not found", "<h1>No such ledger</h1>")

    packet = export_packet(
        load_ledger(path), EXPORT_DIR, basename=ledger_id, narrative_writer=None
    )
    return FileResponse(
        packet["pdf_path"], media_type="application/pdf", filename=f"{ledger_id}.pdf"
    )


# --- mandate lifecycle dashboard --------------------------------------------
#
# A projection, not a store. Every value below is derived from ledger events by
# backend/ledger/lifecycle.py on each request.


@app.get("/mandates", response_class=HTMLResponse)
def mandates_index():
    rows = project_all(load_all_ledgers(), CLOCK())
    counts = summary_counts(rows)

    if not rows:
        return page(
            "Mandates",
            "<h1>Mandates</h1>"
            "<div class='empty'>No mandates recorded yet. Generate one:<br><br>"
            "<code>.venv/bin/python scripts/run_demo.py happy</code><br>"
            "<code>.venv/bin/python scripts/run_demo.py blocked</code></div>",
            active="mandates",
        )

    def tile(key, label):
        return (
            f'<div class="tile" data-count="{escape(key)}">'
            f'<div class="n">{counts.get(key, 0)}</div>'
            f'<div class="k">{escape(label)}</div></div>'
        )

    tiles = "".join(
        [
            tile("total", "mandates"),
            tile("ACTIVE", "active"),
            tile("CONSUMED", "consumed"),
            tile("EXPIRED", "expired"),
            tile("blocked_attempts", "blocked"),
            tile("chain_broken", "chain broken"),
        ]
    )

    body_rows = "".join(
        f"""<tr data-mandate-row data-mandate-hash="{escape(row['mandate_hash'])}">
              <td class="mono"><a href="/mandates/{escape(row['mandate_hash'])}">
                {escape(row['hash_prefix'])}…</a></td>
              <td>{escape((row['merchant'] or {}).get('name') or '—')}</td>
              <td>{escape(row['items_summary'] or '—')}</td>
              <td class="mono">{escape(row['price_ceiling_total'] or '—')}
                {escape(row['currency'] or '')}</td>
              <td class="mono">{escape((row['expires_at'] or '—')[:19])}</td>
              <td data-status>{_chip(row['status'])}</td>
              <td data-last-outcome>{_chip(row['last_outcome'])}</td>
              <td class="num">{row['attempt_count']}</td>
            </tr>"""
        for row in rows
    )

    return page(
        "Mandates",
        "<h1>Mandates</h1>"
        "<p class='sub'>Lifecycle of every authorization, derived from the ledger.</p>"
        f'<div class="tiles" id="mandate-summary">{tiles}</div>'
        "<table id=\"mandate-list\"><tr><th>Mandate</th><th>Merchant</th><th>Items</th>"
        "<th>Ceiling</th><th>Window ends</th><th>Status</th><th>Last attempt</th>"
        "<th>Attempts</th></tr>"
        f"{body_rows}</table>",
        active="mandates",
    )


@app.get("/mandates/{mandate_hash}", response_class=HTMLResponse)
def mandate_detail(mandate_hash: str):
    rows = project_all(load_all_ledgers(), CLOCK())
    row = next((r for r in rows if r["mandate_hash"] == mandate_hash), None)
    if row is None:
        return page("Not found", "<h1>No such mandate</h1><p><a href='/mandates'>Back</a></p>",
                    active="mandates")

    items = "".join(
        f"<tr><td class='mono'>{escape(item['product_id'])}</td>"
        f"<td>{escape(item.get('description') or '')}</td>"
        f"<td class='mono'>{escape(item['max_unit_price'])}</td>"
        f"<td class='num'>{item['quantity']}</td></tr>"
        for item in row["items"]
    )

    constraints = f"""<div class="card" id="constraints-card">
      <h2>Authorized scope</h2>
      <table>{_rows_kv([
        ("Status", _chip(row['status'])),
        ("Last attempt", _chip(row['last_outcome'])),
        ("Request", escape(row['intent_text'] or '')),
        ("Cardholder", escape((row['user'] or {}).get('user_email') or '')),
        ("Merchant", escape((row['merchant'] or {}).get('name') or '')
            + " — <span class='mono'>"
            + escape((row['merchant'] or {}).get('url') or '') + "</span>"),
        ("Spend ceiling", escape(f"{row['price_ceiling_total']} {row['currency']}")),
        ("Window", escape(f"{row['effective_minutes']} minutes from "
                          f"{(row['created_at'] or '')[:19]}")),
        ("Window ends", escape((row['expires_at'] or '—')[:19])),
        ("Mandate hash", f"<span class='mono'>{escape(row['mandate_hash'])}</span>"),
      ])}</table>
      <table><tr><th>Product</th><th>Description</th><th>Max unit price</th>
      <th class="num">Qty</th></tr>{items}</table></div>"""

    if row["attempts"]:
        attempt_rows = "".join(
            f"""<tr data-attempt-row data-attempt="{a['attempt']}">
                  <td class="num">{a['attempt']}</td>
                  <td class="mono">{escape((a['external_order_ref'] or '')[:12])}…
                    .{a['attempt']:02d}</td>
                  <td class="mono">{escape(a['session_id'] or '—')}</td>
                  <td class="mono">{escape(a['order_number'] or '—')}</td>
                  <td>{_chip(a['outcome'])}</td>
                </tr>"""
            for a in row["attempts"]
        )
        attempts = (
            "<h2>Attempt history</h2>"
            "<table id=\"attempt-history\"><tr><th class='num'>#</th>"
            "<th>external_order_ref</th><th>Session</th><th>Order</th><th>Outcome</th></tr>"
            f"{attempt_rows}</table>"
        )
    else:
        attempts = (
            "<h2>Attempt history</h2>"
            "<table id=\"attempt-history\"><tr><th>Attempts</th></tr>"
            "<tr><td class='missing'>No payment session was ever created for this "
            "mandate. A gate block is terminal before any Prava call.</td></tr></table>"
        )

    timeline = "".join(
        f"""<tr data-timeline-row data-event-type="{escape(event['type'])}">
              <td class="num">{index}</td>
              <td><strong>{escape(event['type'])}</strong></td>
              <td class="mono">{escape(event['ts'][:23])}</td>
            </tr>"""
        for index, event in enumerate(row["events"])
    )

    chains = "".join(
        f"""<li><a href="/ledger/{escape(l['ledger_id'] or '')}">
              {escape(l['ledger_id'] or '?')}</a> — {l['event_count']} events,
              chain {'VALID' if l['chain_valid'] else 'BROKEN'}
              · <a href="/ledger/{escape(l['ledger_id'] or '')}/export.json">JSON</a>
              · <a href="/ledger/{escape(l['ledger_id'] or '')}/export.pdf">PDF</a></li>"""
        for l in row["ledgers"]
    )

    return page(
        f"Mandate {row['hash_prefix']}",
        f"""<h1>Mandate {escape(row['hash_prefix'])}…</h1>
        <p class="sub"><a href="/mandates">&larr; All mandates</a></p>
        {constraints}
        {attempts}
        <h2>Lifecycle</h2>
        <table id="lifecycle-timeline"><tr><th class="num">#</th><th>Event</th>
        <th>Timestamp</th></tr>{timeline}</table>
        <h2>Evidence</h2>
        <ul id="mandate-chains">{chains}</ul>""",
        active="mandates",
    )


def _rows_kv(pairs):
    return "".join(f"<tr><th>{escape(k)}</th><td>{v}</td></tr>" for k, v in pairs)
