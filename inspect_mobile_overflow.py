"""
Find elements causing horizontal blowout in 360px viewport
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

def inspect_overflow():
    test_dir = tempfile.mkdtemp(prefix="turboshare_overflow_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8086
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(viewport={"width": 360, "height": 740})
            page = context.new_page()

            page.goto(base_url)
            page.wait_for_timeout(1000)

            # Find all elements wider than 360px or overflowing the viewport
            overflowing = page.evaluate("""() => {
                const results = [];
                const docWidth = document.documentElement.clientWidth;
                const all = document.querySelectorAll('*');
                all.forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > docWidth || rect.right > docWidth) {
                        results.push({
                            tag: el.tagName,
                            id: el.id,
                            className: el.className,
                            width: rect.width,
                            right: rect.right,
                            boxSizing: window.getComputedStyle(el).boxSizing,
                            minWidth: window.getComputedStyle(el).minWidth
                        });
                    }
                });
                return results;
            }""")

            print(f"Found {len(overflowing)} overflowing elements:")
            for item in overflowing[:15]:
                print(" ", item)

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

if __name__ == "__main__":
    inspect_overflow()
