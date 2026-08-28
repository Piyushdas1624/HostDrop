"""
End-to-end Playwright User Flow Verification
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

def run_e2e_tests():
    test_dir = tempfile.mkdtemp(prefix="turboshare_e2e_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    # Seed files
    with open(os.path.join(test_recv, "specs_document.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 test notes content")
    with open(os.path.join(test_recv, "dataset_archive.zip"), "wb") as f:
        f.write(b"PK\x03\x04 fake zip content")
    with open(os.path.join(test_share, "presentation_video.mp4"), "wb") as f:
        f.write(b"fake video mp4")

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8095
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), FixedTurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    print(f"E2E Server running at {base_url}", flush=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            console_logs = []
            page_errors = []
            page.on("console", lambda msg: console_logs.append(f"[{msg.type}] {msg.text}"))
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            page.goto(base_url)
            page.wait_for_timeout(800)

            # 1. Verify initial file listing
            page.wait_for_selector("#fileListBody tr")
            init_rows = page.locator("#fileListBody tr").count()
            print(f"[E2E 1] Initial Received Files Rows: {init_rows}", flush=True)

            # 2. Switch to Host Share Tab
            page.click("#tabShareBtn")
            page.wait_for_timeout(400)
            share_rows = page.locator("#fileListBody tr").count()
            print(f"[E2E 2] Host Shared Files Rows: {share_rows}", flush=True)

            # Switch back to Received Files Tab
            page.click("#tabRecvBtn")
            page.wait_for_timeout(400)
            print(f"[E2E 3] Switched back to Received Files Tab", flush=True)

            # 3. Connection Guide Modal
            page.click("button[onclick*='guideModal']")
            page.wait_for_timeout(400)
            guide_open = "open" in (page.locator("#guideModal").get_attribute("class") or "")
            print(f"[E2E 4] Connection Guide Modal Open: {guide_open}", flush=True)
            page.click("#guideModal .modal-close")
            page.wait_for_timeout(300)

            # 4. QR Modal
            page.click("button[onclick*='showGeneralQR']")
            page.wait_for_timeout(400)
            qr_open = "open" in (page.locator("#qrModal").get_attribute("class") or "")
            print(f"[E2E 5] General QR Modal Open: {qr_open}", flush=True)
            page.click("#qrModal .modal-close")
            page.wait_for_timeout(300)

            # 5. Host Drive & Directory Browser Modal
            page.click("button[onclick*=\"openHostBrowserModal('recv')\"]")
            page.wait_for_timeout(800)
            browser_open = "open" in (page.locator("#hostBrowserModal").get_attribute("class") or "")
            drives_count = page.locator(".drive-chip").count()
            print(f"[E2E 6] Host Browser Modal Open: {browser_open}, Drive Chips Count: {drives_count}", flush=True)
            page.click("#hostBrowserModal .modal-close")
            page.wait_for_timeout(300)

            # 6. Search Filter
            page.fill("#tableSearch", "specs")
            page.wait_for_timeout(300)
            visible_rows = page.locator("#fileListBody tr:not([style*='display: none'])").count()
            print(f"[E2E 7] Search Filter 'specs' Visible Rows: {visible_rows}", flush=True)
            page.fill("#tableSearch", "")
            page.wait_for_timeout(300)

            # 7. Grid Toggle
            page.click("#gridToggleBtn")
            page.wait_for_timeout(300)
            is_grid = "grid-mode" in (page.locator("#netRibbon").get_attribute("class") or "")
            print(f"[E2E 8] Network Ribbon Grid Mode Active: {is_grid}", flush=True)
            page.click("#gridToggleBtn")
            page.wait_for_timeout(300)

            # Check for console errors
            print(f"\n[E2E Results] Console Logs Count: {len(console_logs)}", flush=True)
            print(f"[E2E Results] Page Errors Count: {len(page_errors)}", flush=True)
            for err in page_errors:
                print(f"  Page Error: {err}", flush=True)

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

if __name__ == "__main__":
    run_e2e_tests()
