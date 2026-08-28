"""
Live Browser QA with Playwright (Edge/Chromium)
Captures console logs, page errors, and tests user flows
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

def run_browser_qa():
    test_dir = tempfile.mkdtemp(prefix="turboshare_browser_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    # Create dummy files for UI listing
    with open(os.path.join(test_recv, "document_sample.pdf"), "wb") as f:
        f.write(b"%PDF-1.4 sample file content 1234567890")
    with open(os.path.join(test_share, "presentation_slides.pptx"), "wb") as f:
        f.write(b"sample pptx content")

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8085
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    print(f"Server running at {base_url}")

    console_messages = []
    page_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            page.on("console", lambda msg: console_messages.append({"type": msg.type, "text": msg.text, "location": msg.location}))
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            print("\nNavigating to dashboard...")
            page.goto(base_url)
            page.wait_for_timeout(1500)

            print("\n--- Initial Load Report ---")
            print(f"Page Title: {page.title()}")
            print(f"Console Messages Count: {len(console_messages)}")
            for msg in console_messages:
                print(f"  [{msg['type']}] {msg['text']}")
            print(f"Page Errors Count: {len(page_errors)}")
            for err in page_errors:
                print(f"  [ERROR] {err}")

            # Test Mobile Viewport 360px
            print("\nTesting 360px Mobile Viewport...")
            page.set_viewport_size({"width": 360, "height": 740})
            page.wait_for_timeout(500)
            
            # Check horizontal overflow
            scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
            client_width = page.evaluate("() => document.documentElement.clientWidth")
            print(f"Mobile 360px: scrollWidth={scroll_width}, clientWidth={client_width}")

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

if __name__ == "__main__":
    run_browser_qa()
