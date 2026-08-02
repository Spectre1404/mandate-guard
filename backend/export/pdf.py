"""HTML -> PDF via Playwright print-to-PDF — SPEC §8.

Chromium's own print pipeline, driven from `set_content`, so nothing is served
over HTTP and the page is fully self-contained (the confirmation screenshot is
inlined as a data URI before it gets here).

Tracing is never enabled: this packet is built from a record that has already had
credentials stripped, but the habit is the point.
"""

import json
import os

from backend.export.evidence import build_evidence, write_narrative
from backend.export.render import render_evidence_html

PDF_OPTIONS = {
    "format": "A4",
    "print_background": True,
    "margin": {"top": "14mm", "bottom": "16mm", "left": "14mm", "right": "14mm"},
    "display_header_footer": True,
    "header_template": "<div></div>",
    "footer_template": (
        "<div style='width:100%;font:8pt -apple-system,sans-serif;color:#5b6270;"
        "padding:0 14mm;display:flex;justify-content:space-between'>"
        "<span>Mandate Guard — Evidence Packet</span>"
        "<span>Page <span class='pageNumber'></span> of <span class='totalPages'></span>"
        "</span></div>"
    ),
}


def html_to_pdf(html, path, page=None):
    """Render `html` to a PDF at `path`.

    Pass an existing Playwright `page` to reuse a browser; otherwise one is
    launched for the call.
    """
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)

    if page is not None:
        page.set_content(html, wait_until="load")
        page.pdf(path=path, **PDF_OPTIONS)
        return path

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            new_page = browser.new_page()
            new_page.set_content(html, wait_until="load")
            new_page.pdf(path=path, **PDF_OPTIONS)
        finally:
            browser.close()
    return path


def export_json_only(ledger, output_dir, basename="evidence", narrative_writer=None):
    """The packet as JSON, with no PDF render.

    The hosted demo has no browser, so it cannot print a PDF. The JSON is the
    complete record either way -- the PDF is a rendering of it.
    """
    evidence = build_evidence(ledger)
    evidence["narrative"] = write_narrative(evidence, narrative_writer)

    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, f"{basename}.json")
    with open(path, "w") as handle:
        json.dump(evidence, handle, indent=2, sort_keys=True)
    return path


def export_packet(ledger, output_dir, basename="evidence", narrative_writer=None, page=None):
    """Build the packet once and write both artifacts from it.

    JSON and PDF are rendered from the *same* assembled evidence object, so the
    two can never disagree about what happened.
    """
    evidence = build_evidence(ledger)
    evidence["narrative"] = write_narrative(evidence, narrative_writer)

    checkout = evidence.get("checkout")
    if checkout:
        from backend.export.evidence import embed_screenshot

        # Copied onto the packet rather than mutating the ledger's payload.
        evidence["checkout"] = dict(
            checkout, screenshot_data_uri=embed_screenshot(checkout.get("screenshot_path"))
        )

    os.makedirs(output_dir, exist_ok=True)
    json_path = os.path.join(output_dir, f"{basename}.json")
    pdf_path = os.path.join(output_dir, f"{basename}.pdf")

    # The JSON is the chain verbatim: drop the inlined image, which is a rendering
    # detail and would balloon the file.
    serializable = dict(evidence)
    if serializable.get("checkout"):
        serializable["checkout"] = {
            key: value
            for key, value in serializable["checkout"].items()
            if key != "screenshot_data_uri"
        }
    with open(json_path, "w") as handle:
        json.dump(serializable, handle, indent=2, sort_keys=True)

    html_to_pdf(render_evidence_html(evidence), pdf_path, page=page)

    return {"json_path": json_path, "pdf_path": pdf_path, "evidence": evidence}
