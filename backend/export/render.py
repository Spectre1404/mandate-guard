"""Evidence packet -> printable HTML — SPEC §8.

Server-rendered, self-contained (the screenshot is inlined as a data URI), and
styled for A4 print. Section order is the order a dispute analyst reads in:
what was authorized, what was proposed, what verification said, what the network
was told, what actually happened, and whether the record can be trusted.

The AI narrative sits at the end under an explicit label, visually separated, so
nobody mistakes generated prose for the record itself.
"""

from html import escape

PRINT_CSS = """
  @page { size: A4; margin: 14mm; }
  * { box-sizing: border-box; }
  body { font: 11pt/1.45 -apple-system, 'Segoe UI', Helvetica, sans-serif; color: #16181d; }
  h1 { font-size: 20pt; margin: 0 0 2mm; }
  h2 { font-size: 12pt; margin: 8mm 0 2mm; padding-bottom: 1.5mm;
       border-bottom: 1.5px solid #16181d; page-break-after: avoid; }
  .sub { color: #5b6270; margin: 0 0 6mm; font-size: 10pt; }
  .cat { color: #5b6270; font-size: 8.5pt; font-style: italic; margin: 0 0 2mm; }
  table { width: 100%; border-collapse: collapse; margin: 0 0 3mm; font-size: 10pt; }
  th, td { text-align: left; padding: 1.6mm 2mm; border-bottom: 1px solid #e3e5ea;
           vertical-align: top; }
  th { color: #5b6270; font-weight: 600; width: 38%; }
  th.w-rule { width: 11mm; } th.w-name { width: 34mm; } th.w-res { width: 15mm; }
  /* Without this the detail column inherits th{width:38%} and squeezes values
     into needless wrapping. */
  th.w-detail { width: auto; }
  td.num, th.num { font-variant-numeric: tabular-nums; }
  .mono { font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 8.5pt;
          word-break: break-all; }
  .pass { color: #146c2e; font-weight: 700; }
  .fail { color: #b3261e; font-weight: 700; }
  .banner { padding: 3mm 4mm; border-radius: 2mm; margin: 0 0 5mm; font-size: 10.5pt; }
  .banner.ok { background: #e8f6ec; border: 1px solid #146c2e; }
  .banner.bad { background: #fdeaea; border: 1px solid #b3261e; }
  .narrative { background: #f6f7f9; border: 1px solid #d6d9e0; border-radius: 2mm;
               padding: 4mm 5mm; page-break-inside: avoid; }
  .narrative h2 { border: 0; margin-top: 0; }
  .narrative .tag { display: inline-block; background: #16181d; color: #fff;
                    font-size: 8pt; padding: 0.8mm 2mm; border-radius: 1mm;
                    letter-spacing: 0.04em; margin-bottom: 2mm; }
  img.shot { width: 100%; border: 1px solid #d6d9e0; border-radius: 2mm; margin-top: 2mm; }
  td.cmp { padding: 1.6mm 2mm; }
  .cmp-row { display: flex; gap: 2.5mm; margin: 0.4mm 0; }
  .cmp .lbl { flex: 0 0 18mm; min-width: 0; color: #5b6270; font-size: 7.5pt;
              text-transform: uppercase; letter-spacing: 0.04em; padding-top: 0.3mm; }
  /* 18mm fits the word EXPECTED. A narrower basis lets the wider label expand past
     it (min-width:auto), which shifts Expected's values out of line with Actual's. */
  /* break-word breaks only a word that cannot fit at all -- never inside a price. */
  .cmp .vals { font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 8.5pt;
               word-break: normal; overflow-wrap: break-word; hyphens: none; }
  .missing { color: #8a9099; font-style: italic; }
  section { page-break-inside: avoid; }
  .foot { margin-top: 8mm; padding-top: 3mm; border-top: 1px solid #e3e5ea;
          color: #5b6270; font-size: 8.5pt; }
"""


def _t(value):
    return escape(str(value)) if value is not None else ""


def _rows(pairs):
    return "".join(
        f"<tr><th>{_t(label)}</th><td>{value}</td></tr>" for label, value in pairs if value != ""
    )


def _missing(what):
    return f'<p class="missing">Not present in this record: {escape(what)}.</p>'


def _mono(value):
    return f'<span class="mono">{_t(value)}</span>' if value else ""


# Every value that means "this is fine". Kept explicit because the failure mode is
# silent and ugly: an unlisted positive word renders red, so the packet would claim
# success in the colour of failure.
PASS_VALUES = {"PASS", "SUCCESS", "VALID", "VERIFIED", "APPROVED", True}


def _verdict(value, ok=None):
    """Colour a verdict. Pass `ok` explicitly whenever the wording is not a known value."""
    passed = (value in PASS_VALUES) if ok is None else bool(ok)
    return f'<span class="{"pass" if passed else "fail"}">{_t(value)}</span>'


def _chain_verdict(valid):
    """Booleans must read as a verdict, not as Python. 'True' is not evidence language."""
    return _verdict("VERIFIED" if valid else "BROKEN", ok=valid)


def _value_lines(value):
    """Break a rule's expected/actual into short atoms, one per line.

    The gate reports lists (`['BL-HOUSE-12<=14.00', ...]`) and dicts. Rendering
    those as one long Python repr forced mid-token wrapping -- and in an evidence
    document a price that wraps as '12.5 / 0' is not acceptable at any width. One
    atom per line keeps every line short enough that it never needs to break.
    """
    if isinstance(value, dict):
        return [f"{key}: {value[key]}" for key in value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value] or ["(none)"]
    return [str(value)]


def _cmp_cell(expected, actual):
    """Expected and actual stacked as labelled lines in one wide cell.

    Two narrow columns were the root of the wrapping problem. One wide cell with
    stacked labels gives each value the full width of the table, and `.vals` uses
    `overflow-wrap: break-word` (which only breaks a word that cannot fit at all)
    rather than `word-break: break-all` (which breaks anywhere, including inside a
    number).
    """
    def block(label, value):
        lines = "".join(f"<div>{_t(line)}</div>" for line in _value_lines(value))
        return (
            f'<div class="cmp-row"><span class="lbl">{label}</span>'
            f'<div class="vals">{lines}</div></div>'
        )

    return (
        f'<td class="cmp">{block("Expected", expected)}{block("Actual", actual)}</td>'
    )


def render_evidence_html(evidence):
    cover = evidence["cover"]
    cats = evidence["categories"]
    blocked = evidence["outcome"] == "BLOCKED_AT_GATE"

    banner_class = "bad" if blocked or not cover["chain_valid"] else "ok"
    banner_text = {
        "BLOCKED_AT_GATE": "Purchase BLOCKED by the verification gate. "
        "No payment session was created and no credential was ever issued.",
        "COMPLETED": "Purchase completed and confirmed by the card network.",
        "INCOMPLETE": "Purchase did not reach a confirmed outcome.",
    }[evidence["outcome"]]

    parts = [
        f"<h1>Mandate Guard — Evidence Packet</h1>",
        f'<p class="sub">Authorization record for an autonomous agent purchase · '
        f'packet v{_t(evidence["packet_version"])}</p>',
        f'<div class="banner {banner_class}">{escape(banner_text)}</div>',
        _cover_section(cover, cats),
        _mandate_section(evidence.get("mandate"), cats),
        _proposal_section(evidence.get("proposal"), cats),
        _gate_section(evidence.get("gate"), evidence.get("gate_blocked"), cats),
        _session_section(evidence.get("session"), cats),
        _credentials_section(evidence.get("credentials"), cats),
        _precheck_section(evidence.get("precheck"), cats),
        _checkout_section(evidence.get("checkout"), cats),
        _report_section(evidence.get("report"), evidence.get("consumed"), cats),
        _chain_section(evidence["chain"], cats),
        _narrative_section(evidence),
        '<p class="foot">Credential handling: full payment tokens and dynamic CVVs are '
        "held in memory only and never persisted, logged, or included in this packet. "
        "This record stores the token's last four digits, its expiry, and the "
        "transaction reference only.</p>",
    ]
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Mandate Guard — Evidence Packet</title>"
        f"<style>{PRINT_CSS}</style></head><body>" + "".join(parts) + "</body></html>"
    )


def _cover_section(cover, cats):
    merchant = cover.get("merchant") or {}
    user = cover.get("user") or {}
    return f"""<section><h2>1. Summary</h2>
      <table>{_rows([
        ("Outcome", _verdict(cover.get("visa_confirmation") or "—")),
        ("Request", _t(cover.get("intent_text"))),
        ("Cardholder", _t(user.get("user_email"))),
        ("Merchant", _t(merchant.get("name"))),
        ("Amount", f'{_t(cover.get("amount_authorized") or "—")} {_t(cover.get("currency"))}'),
        ("Order number", _mono(cover.get("order_number")) or "—"),
        ("Mandate hash", _mono(cover.get("mandate_hash"))),
        ("Ledger events", _t(cover.get("event_count"))),
        ("Chain verified", _chain_verdict(cover.get("chain_valid"))),
      ])}</table></section>"""


def _mandate_section(mandate, cats):
    if not mandate:
        return f"<section><h2>2. Mandate</h2>{_missing('mandate')}</section>"
    # A partial mandate must degrade to "not present", never crash: an evidence
    # tool that 500s on an odd record is useless exactly when it is needed.
    constraints = mandate.get("constraints")
    if not constraints:
        return (
            f"<section><h2>2. Mandate</h2>{_missing('mandate constraints')}</section>"
        )
    items = "".join(
        f"<tr><td>{_t(i['product_id'])}</td><td>{_t(i['description'])}</td>"
        f"<td class='num'>{_t(i['max_unit_price'])}</td><td class='num'>{_t(i['quantity'])}</td></tr>"
        for i in constraints["items"]
    )
    return f"""<section><h2>2. Mandate — what the user authorized</h2>
      <p class="cat">{escape(cats['mandate'])}</p>
      <table>{_rows([
        ("Mandate ID", _mono(mandate.get("mandate_id"))),
        ("Status", _t(mandate.get("status"))),
        ("Confirmed at", _t(mandate.get("confirmed_at"))),
        ("Created at", _t(mandate.get("created_at"))),
        ("Valid for", f'{_t(constraints["effective_minutes"])} minutes from creation'),
        ("Merchant", f'{_t(constraints["merchant"]["name"])} — {_mono(constraints["merchant"]["url"])}'),
        ("Spend ceiling", f'{_t(constraints["price_ceiling_total"])} {_t(constraints["currency"])}'),
        ("Substitutions", _t(constraints["substitution_policy"])),
      ])}</table>
      <table><tr><th>Product</th><th>Description</th><th class="num">Max unit price</th>
      <th class="num">Qty</th></tr>{items}</table></section>"""


def _proposal_section(proposal, cats):
    if not proposal:
        return f"<section><h2>3. Agent proposal</h2>{_missing('agent proposal')}</section>"
    lines = "".join(
        f"<tr><td>{_t(l['product_id'])}</td><td>{_t(l['description'])}</td>"
        f"<td class='num'>{_t(l['unit_price'])}</td><td class='num'>{_t(l['quantity'])}</td></tr>"
        for l in proposal["line_items"]
    )
    meta = proposal.get("agent_meta") or {}
    return f"""<section><h2>3. Agent proposal — what the agent wanted to buy</h2>
      <p class="cat">{escape(cats['proposal'])}</p>
      <table><tr><th>Product</th><th>Description</th><th class="num">Unit price</th>
      <th class="num">Qty</th></tr>{lines}</table>
      <table>{_rows([
        ("Proposed total", _t(proposal.get("proposed_total"))),
        ("Agent model", _t(meta.get("model"))),
        ("Agent rationale", _t(meta.get("rationale"))),
      ])}</table></section>"""


def _gate_section(gate, blocked, cats):
    if not gate:
        return f"<section><h2>4. Verification gate</h2>{_missing('gate verdict')}</section>"
    rows = "".join(
        f"<tr><td>{_t(r['rule_id'])}</td><td>{_t(r['name'])}</td>"
        f"<td>{_verdict('PASS' if r['pass'] else 'FAIL', ok=r['pass'])}</td>"
        f"{_cmp_cell(r['expected'], r['actual'])}</tr>"
        for r in gate["results"]
    )
    blocked_note = (
        f'<div class="banner bad">Gate verdict FAIL — {escape(str(blocked.get("reason")))}.</div>'
        if blocked
        else ""
    )
    return f"""<section><h2>4. Verification gate — was the agent allowed to buy this?</h2>
      <p class="cat">{escape(cats['gate'])}</p>
      <p>Verdict: {_verdict(gate['verdict'])}</p>{blocked_note}
      <table><tr><th class="w-rule">Rule</th><th class="w-name">Name</th>
      <th class="w-res">Result</th><th class="w-detail">Verification detail</th></tr>
      {rows}</table></section>"""


def _session_section(session, cats):
    if not session:
        return (
            "<section><h2>5. Payment session</h2>"
            '<p class="missing">No payment session was created. '
            "On a gate FAIL this is the expected state: nothing was ever authorized.</p></section>"
        )
    return f"""<section><h2>5. Payment session — what the network authorized</h2>
      <p class="cat">{escape(cats['session'])}</p>
      <table>{_rows([
        ("Session ID", _mono(session.get("session_id"))),
        ("Order ID", _mono(session.get("order_id"))),
        ("Expires at", _t(session.get("expires_at"))),
        ("External order ref", _mono(session.get("external_order_ref"))),
        ("Attempt", _t(session.get("attempt"))),
      ])}</table>
      <p class="cat">The external order reference is the mandate hash plus the attempt
      number, tying the network's record of this payment to the evidence chain above.</p>
      </section>"""


def _credentials_section(credentials, cats):
    if not credentials:
        return (
            "<section><h2>6. Credentials</h2>"
            '<p class="missing">No credentials were issued.</p></section>'
        )
    return f"""<section><h2>6. Credential issuance — masked</h2>
      <p class="cat">{escape(cats['credentials'])}</p>
      <table>{_rows([
        ("Transaction reference", _mono(credentials.get("txn_ref_id"))),
        ("Card token", f'•••• •••• •••• {_t(credentials.get("token_last4"))}'),
        ("Expiry", f'{_t(credentials.get("expiry_month"))}/{_t(credentials.get("expiry_year"))}'),
      ])}</table></section>"""


def _precheck_section(precheck, cats):
    if not precheck:
        return ""
    rows = "".join(
        f"<tr><td>{_t(r['rule_id'])}</td><td>{_t(r['name'])}</td>"
        f"<td>{_verdict('PASS' if r['pass'] else 'FAIL', ok=r['pass'])}</td>"
        f"{_cmp_cell(r['expected'], r['actual'])}</tr>"
        for r in precheck["results"]
    )
    disclosure = precheck.get("origin_disclosure") or {}
    origin_rows = _rows(
        [
            ("Canonical merchant URL", _mono(disclosure.get("canonical_merchant_url"))),
            ("Declared serving origin", _mono(disclosure.get("declared_origin")) or "—"),
            ("Observed page host", _mono(disclosure.get("observed_host"))),
        ]
    )
    return f"""<section><h2>7. Point-of-sale re-verification</h2>
      <p class="cat">{escape(cats['precheck'])}</p>
      <p>Verdict: {_verdict(precheck['verdict'])}</p>
      <table><tr><th class="w-rule">Check</th><th class="w-name">Name</th>
      <th class="w-res">Result</th><th class="w-detail">Verification detail</th></tr>
      {rows}</table>
      <table>{origin_rows}</table></section>"""


def _checkout_section(checkout, cats):
    if not checkout:
        return (
            "<section><h2>8. Checkout proof</h2>"
            '<p class="missing">No checkout was executed.</p></section>'
        )
    shot = checkout.get("screenshot_data_uri")
    image = f'<img class="shot" src="{shot}" alt="Order confirmation page">' if shot else (
        '<p class="missing">Screenshot file not available at export time; its SHA-256 is '
        "recorded above.</p>"
    )
    return f"""<section><h2>8. Checkout proof</h2>
      <p class="cat">{escape(cats['checkout'])}</p>
      <table>{_rows([
        ("Order number", _mono(checkout.get("order_number"))),
        ("Authorization code", _mono(checkout.get("authorization_code"))),
        ("Processor response", _mono(checkout.get("response_code"))),
        ("Confirmation URL", _mono(checkout.get("confirmation_url"))),
        ("Screenshot SHA-256", _mono(checkout.get("screenshot_sha256"))),
      ])}</table>{image}</section>"""


def _report_section(report, consumed, cats):
    if not report:
        return (
            "<section><h2>9. Outcome reported</h2>"
            '<p class="missing">No outcome was reported to the network.</p></section>'
        )
    return f"""<section><h2>9. Outcome reported to the card network</h2>
      <p class="cat">{escape(cats['report'])}</p>
      <table>{_rows([
        ("Transaction status", _verdict(report.get("txn_status"))),
        ("Visa confirmation", _verdict(report.get("visa_confirmation"))),
        ("Authorization code", _mono(report.get("authorization_code"))),
        ("Response code", _mono(report.get("response_code"))),
        ("Mandate consumed", "Yes" if consumed else "No"),
      ])}</table></section>"""


def _chain_section(chain, cats):
    rows = "".join(
        f"<tr><td class='num'>{_t(link['index'])}</td><td>{_t(link['type'])}</td>"
        f"<td>{_verdict('VALID' if link['valid'] else 'BROKEN', ok=link['valid'])}</td>"
        f"<td>{_t(link.get('reason') or '')}</td></tr>"
        for link in chain["links"]
    )
    return f"""<section><h2>10. Chain verification</h2>
      <p class="cat">{escape(cats['chain'])}</p>
      <p>Every event hash recomputed at export time: {_chain_verdict(chain['valid'])}</p>
      <table><tr><th class="num">#</th><th>Event</th><th>Link</th><th>Note</th></tr>
      {rows}</table></section>"""


def _narrative_section(evidence):
    return f"""<section class="narrative">
      <span class="tag">AI-GENERATED</span>
      <h2>{escape(evidence['narrative_label'])}</h2>
      <p>{escape(str(evidence.get('narrative') or '')).replace(chr(10), '</p><p>')}</p>
      </section>"""
