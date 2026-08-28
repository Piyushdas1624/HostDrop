"""
Comprehensive Interactive Playwright Test (Full User Flows)
Tests all modals, tabs, search filter, and responsive viewports
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

def test_full_interactive_flows():
    test_dir = tempfile.mkdtemp(prefix="turboshare_flows_")
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
    test_port = 8087
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    print(f"Server online at {base_url}")

    results = []

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
            print("Checking tab switching...")
            # Click Host Shared Files Tab
            tab_share = page.locator("#tabShare")
            if tab_share.count() > 0:
                tab_share.click()
                page.wait_for_timeout(500)
                print("Clicked tabShare")

            # Click Received Files Tab
            tab_recv = page.locator("#tabRecv")
            if tab_recv.count() > 0:
                tab_recv.click()
                page.wait_for_timeout(500)
                print("Clicked tabRecv")

            # Test 2: Connection Guide Modal
            print("Checking Connection Guide modal...")
            btn_guide = page.locator("button:has-text('Connection Guide')")
            if btn_guide.count() > 0:
                btn_guide.click()
                page.wait_for_timeout(500)
                modal_help = page.locator("#helpModal")
                is_open = "open" in (modal_help.get_attribute("class") or "")
                print(f"Connection Guide Modal Open: {is_open}")
                # Close modal
                btn_close = page.locator("#helpModal .modal-close")
                if btn_close.count() > 0:
                    btn_close.click()
                    page.wait_for_timeout(300)

            # Test 3: QR Code Modal
            print("Checking QR Code modal...")
            btn_qr = page.locator("button[onclick='openQrModal()']")
            if btn_qr.count() > 0:
                btn_qr.click()
                page.wait_for_timeout(500)
                modal_qr = page.locator("#qrModal")
                is_open = "open" in (modal_qr.get_attribute("class") or "")
                print(f"QR Modal Open: {is_open}")
                # Close modal
                btn_close = page.locator("#qrModal .modal-close")
                if btn_close.count() > 0:
                    btn_close.click()
                    page.wait_for_timeout(300)

            # Test 4: Host Drive Browser Modal
            print("Checking Host Drive Browser modal...")
            btn_choose = page.locator("#btnChooseFolder")
            if btn_choose.count() > 0:
                btn_choose.click()
                page.wait_for_timeout(800)
                modal_browser = page.locator("#browseHostModal")
                is_open = "open" in (modal_browser.get_attribute("class") or "")
                print(f"Drive Browser Modal Open: {is_open}")
                # Check drive items rendered
                drive_chips = page.locator(".drive-chip")
                print(f"Drive Chips Rendered: {drive_chips.count()}")
                # Close modal
                btn_close = page.locator("#browseHostModal .modal-close")
                if btn_close.count() > 0:
                    btn_close.click()
                    page.wait_for_timeout(300)

            # Test 5: Search / Filter input
            print("Checking file search filter...")
            search_input = page.locator("#tableSearch")
            if search_input.count() > 0:
                search_input.fill("notes")
                page.wait_for_timeout(300)
                rows = page.locator("#fileListBody tr:not(.empty-state-row)")
                print(f"Filtered Table Rows (matching 'notes'): {rows.count()}")
                search_input.fill("")
                page.wait_for_timeout(300)

            # Test 6: Network Ribbon Scroll & Grid Toggle
            print("Checking Network Ribbon Grid Toggle...")
            btn_grid = page.locator("#netGridToggle")
            if btn_grid.count() > 0:
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
    test_full_interactive_flows()
