"""
Verify interactive flows with fixed JS syntax in memory
"""
import os
import sys
import time
import json
import threading
import tempfile
import shutil
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

import urllib.parse

def test_fixed_flows():
    test_dir = tempfile.mkdtemp(prefix="turboshare_fixed_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    # Seed files
    with open(os.path.join(test_recv, "project_notes.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 test notes content")
    with open(os.path.join(test_recv, "archive_data.zip"), "wb") as f:
        f.write(b"PK\x03\x04 fake zip content")
    with open(os.path.join(test_share, "shared_video.mp4"), "wb") as f:
        f.write(b"fake video mp4")

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8088
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), FixedTurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    print(f"Server online at {base_url}")

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
            page.wait_for_timeout(1000)

            # Test 1: Tab switching
            tab_share = page.locator("#tabShare")
            tab_share.click()
            page.wait_for_timeout(500)
            share_rows = page.locator("#fileListBody tr").count()
            print(f"Host Shared Files rows rendered: {share_rows}")

            tab_recv = page.locator("#tabRecv")
            tab_recv.click()
            page.wait_for_timeout(500)
            recv_rows = page.locator("#fileListBody tr").count()
            print(f"Received Files rows rendered: {recv_rows}")

            # Test 2: Connection Guide Modal
            btn_guide = page.locator("button:has-text('Connection Guide')")
            btn_guide.click()
            page.wait_for_timeout(500)
            modal_help = page.locator("#helpModal")
            is_open = "open" in (modal_help.get_attribute("class") or "")
            print(f"Connection Guide Modal Open: {is_open}")
            btn_close = page.locator("#helpModal .modal-close")
            btn_close.click()
            page.wait_for_timeout(300)

            # Test 3: QR Code Modal
            btn_qr = page.locator("button[onclick='openQrModal()']")
            btn_qr.click()
            page.wait_for_timeout(500)
            modal_qr = page.locator("#qrModal")
            is_open = "open" in (modal_qr.get_attribute("class") or "")
            print(f"QR Modal Open: {is_open}")
            btn_close = page.locator("#qrModal .modal-close")
            btn_close.click()
            page.wait_for_timeout(300)

            # Test 4: Host Drive Browser Modal
            btn_choose = page.locator("#btnChooseFolder")
            btn_choose.click()
            page.wait_for_timeout(800)
            modal_browser = page.locator("#browseHostModal")
            is_open = "open" in (modal_browser.get_attribute("class") or "")
            print(f"Drive Browser Modal Open: {is_open}")
            drive_chips = page.locator(".drive-chip")
            print(f"Drive Chips Rendered: {drive_chips.count()}")
            btn_close = page.locator("#browseHostModal .modal-close")
            btn_close.click()
            page.wait_for_timeout(300)

            # Test 5: Search / Filter input
            search_input = page.locator("#tableSearch")
            search_input.fill("notes")
            page.wait_for_timeout(300)
            rows = page.locator("#fileListBody tr:not([style*='display: none'])")
            print(f"Filtered Table Rows (matching 'notes'): {rows.count()}")
            search_input.fill("")
            page.wait_for_timeout(300)

            # Test 6: Network Ribbon Scroll & Grid Toggle
            btn_grid = page.locator("#netGridToggle")
            btn_grid.click()
            page.wait_for_timeout(300)
            is_grid = "grid-mode" in (page.locator("#netRibbon").get_attribute("class") or "")
            print(f"Network Ribbon in Grid Mode: {is_grid}")
            btn_grid.click()
            page.wait_for_timeout(300)

            print(f"\nTotal Console Logs: {len(console_logs)}")
            for log in console_logs:
                print(f"  {log}")
            print(f"Total Page Errors: {len(page_errors)}")
            for err in page_errors:
                print(f"  [ERROR] {err}")

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

if __name__ == "__main__":
    test_fixed_flows()
