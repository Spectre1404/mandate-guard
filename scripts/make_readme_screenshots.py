"""Generate the four README screenshots. Deliberately committed artifacts.

    .venv/bin/python scripts/make_readme_screenshots.py

Writes evidence/sample/screenshots/:
    gate-verdict-blocked.png   red gate table naming the failed rules
    dashboard.png              CONSUMED/COMPLETED beside ACTIVE/BLOCKED
    chain-tampered.png         verify button catching an edited ledger
    packet-page1.png           flagship evidence packet, summary page

The tamper step edits a real ledger file on disk. It restores in `try/finally`,
so an exception mid-run cannot leave forged data behind.
"""

import glob
import json
import os
import shutil
import socket
import sys
import threading
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

OUT = os.path.join(REPO_ROOT, "evidence", "sample", "screenshots")
LEDGER_DIR = os.path.join(REPO_ROOT, "evidence", "ledgers")
MAX_BYTES = 300_000


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_ledger_ui():
    import uvicorn

    from ledger_ui.app import app

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    return server, f"http://127.0.0.1:{port}"


def report(path):
    size = os.path.getsize(path)
    flag = "OK " if size <= MAX_BYTES else "BIG"
    print(f"  {flag} {os.path.basename(path):26s} {size / 1000:7.1f} KB")


def shoot_gate_verdict(playwright):
    """Section 4 of the blocked run's packet: red FAIL rows naming R3 and R5."""
    from backend.export.render import render_evidence_html

    with open(os.path.join(REPO_ROOT, "evidence", "demo", "evidence-blocked.json")) as fh:
        evidence = json.load(fh)

    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 900, "height": 900})
    page.emulate_media(media="print")
    page.set_content(render_evidence_html(evidence), wait_until="load")
    section = next(
        s for s in page.query_selector_all("section") if s.inner_text().startswith("4.")
    )
    path = os.path.join(OUT, "gate-verdict-blocked.png")
    section.screenshot(path=path)
    browser.close()
    return path


def shoot_dashboard(playwright, base):
    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 1100, "height": 620})
    page.goto(f"{base}/mandates")
    page.wait_for_selector("#mandate-list")
    path = os.path.join(OUT, "dashboard.png")
    page.screenshot(path=path)
    browser.close()
    return path


def shoot_tampered_chain(playwright, base):
    """Edit a real ledger, screenshot the BROKEN verdict, always restore."""
    candidates = [
        p
        for p in sorted(glob.glob(os.path.join(LEDGER_DIR, "*.ledger.json")))
        if any(e["type"] == "CHECKOUT_EXECUTED" for e in json.load(open(p))["events"])
    ]
    if not candidates:
        raise RuntimeError("no completed ledger to tamper with")

    target = candidates[0]
    backup = target + ".backup"
    shutil.copy(target, backup)
    try:
        data = json.load(open(target))
        for event in data["events"]:
            if event["type"] == "CHECKOUT_EXECUTED":
                event["payload"]["order_number"] = "BL-FORGED1"
        with open(target, "w") as fh:
            json.dump(data, fh)

        ledger_id = os.path.basename(target).replace(".ledger.json", "")
        browser = playwright.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 720})
        page.goto(f"{base}/ledger/{ledger_id}")
        page.click("#verify-chain")
        page.wait_for_selector("#verify-result")
        assert page.inner_text("#chain-verdict") == "BROKEN"
        path = os.path.join(OUT, "chain-tampered.png")
        page.screenshot(path=path)
        browser.close()
        return path
    finally:
        # Restore unconditionally: a half-finished run must never leave forged
        # evidence on disk.
        shutil.move(backup, target)


def shoot_packet_page(playwright):
    """Page 1 of the flagship packet, via its own HTML at print width."""
    from backend.export.render import render_evidence_html

    with open(os.path.join(REPO_ROOT, "evidence", "sample", "evidence-packet.json")) as fh:
        evidence = json.load(fh)

    browser = playwright.chromium.launch()
    page = browser.new_page(viewport={"width": 794, "height": 1010})
    page.emulate_media(media="print")
    page.set_content(render_evidence_html(evidence), wait_until="load")
    path = os.path.join(OUT, "packet-page1.png")
    page.screenshot(path=path)
    browser.close()
    return path


def main():
    os.makedirs(OUT, exist_ok=True)
    server, base = start_ledger_ui()
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            print("screenshots:")
            for shot in (
                lambda: shoot_gate_verdict(playwright),
                lambda: shoot_dashboard(playwright, base),
                lambda: shoot_tampered_chain(playwright, base),
                lambda: shoot_packet_page(playwright),
            ):
                report(shot())
    finally:
        server.should_exit = True

    remaining = [
        p for p in glob.glob(os.path.join(LEDGER_DIR, "*.backup"))
    ]
    print(f"stray backups left behind: {remaining or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
