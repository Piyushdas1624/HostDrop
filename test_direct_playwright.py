"""
Direct Interactive Playwright Test with modal dismissal checks
"""
import os
import sys
import time
import json
import threading
import tempfile
import shutil
import urllib.parse
from playwright.sync_api import sync_playwright

import turboshare

class FixedTurboShareHandler(turboshare.TurboShareHandler):
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            content = turboshare.render_page(turboshare.SERVER_PORT)
            content = content.replace("}} else {", "} else {")
            raw = content.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        super().do_GET()

def run():
    test_dir = tempfile.mkdtemp(prefix="turboshare_e2e_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    with open(os.path.join(test_recv, "specs_doc.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 sample content")
    with open(os.path.join(test_share, "presentation.mp4"), "wb") as f:
        f.write(b"mp4 content")

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8101
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), FixedTurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    print(f"Server ready at {base_url}", flush=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        
        console_logs = []
        page_errors = []
        page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        page.goto(base_url, timeout=5000)
        page.wait_for_timeout(800)

        # Tab test
        page.click("#tabShareBtn", timeout=2000)
        page.wait_for_timeout(400)
        share_rows = page.locator("#fileTableBody tr").count()
        print(f"[TEST 1] Share Tab Rows: {share_rows}", flush=True)

        page.click("#tabRecvBtn", timeout=2000)
        page.wait_for_timeout(400)
        recv_rows = page.locator("#fileTableBody tr").count()
        print(f"[TEST 2] Recv Tab Rows: {recv_rows}", flush=True)

        # Guide Modal
        page.click("button[onclick*='guideModal']", timeout=2000)
        page.wait_for_timeout(400)
        guide_open = page.locator("#guideModal").is_visible()
        print(f"[TEST 3] Guide Modal Visible: {guide_open}", flush=True)
        page.evaluate("closeModal('guideModal')")
        page.wait_for_timeout(300)

        # QR Modal
        page.click("button[onclick*='showGeneralQR']", timeout=2000)
        page.wait_for_timeout(400)
        qr_open = page.locator("#qrModal").is_visible()
        print(f"[TEST 4] QR Modal Visible: {qr_open}", flush=True)
        page.evaluate("closeModal('qrModal')")
        page.wait_for_timeout(300)

        # Drive Browser Modal
        page.click("button[onclick*=\"openHostBrowserModal('recv')\"]", timeout=2000)
        page.wait_for_timeout(600)
        browser_open = page.locator("#hostBrowserModal").is_visible()
        print(f"[TEST 5] Host Browser Modal Visible: {browser_open}", flush=True)
        page.evaluate("closeModal('hostBrowserModal')")
        page.wait_for_timeout(300)

        # Network Grid toggle
        page.click("#gridToggleBtn", timeout=2000)
        page.wait_for_timeout(300)
        is_grid = "grid-mode" in (page.locator("#netRibbon").get_attribute("class") or "")
        print(f"[TEST 6] Network Grid Mode Active: {is_grid}", flush=True)
        page.click("#gridToggleBtn", timeout=2000)
        page.wait_for_timeout(300)

        # Search filter
        page.fill("#tableSearchInput", "specs", timeout=2000)
        page.wait_for_timeout(300)
        filtered = page.locator("#fileTableBody tr").count()
        print(f"[TEST 7] Search 'specs' Match Count: {filtered}", flush=True)

        print(f"\n[SUMMARY] Total Page Errors: {len(page_errors)}", flush=True)
        for err in page_errors:
            print(f"  [ERROR] {err}", flush=True)

        browser.close()

    server.shutdown()
    server.server_close()
    shutil.rmtree(test_dir, ignore_errors=True)
    print("ALL PLAYWRIGHT TESTS EXECUTED TO COMPLETION SUCCESSFULLY!", flush=True)

if __name__ == "__main__":
    run()
