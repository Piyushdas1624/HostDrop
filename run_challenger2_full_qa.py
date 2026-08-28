"""
Challenger 2 — Comprehensive Live QA & Adversarial Test Runner
Tests live TurboShare server on port 8080 (or fallback 8089)
Produces empirical test logs, HTTP status codes, payloads, and failure traces.
"""
import os
import sys
import io
import time
import json
import socket
import shutil
import tempfile
import urllib.request
import urllib.parse
import zipfile
import threading
import subprocess
import re
from playwright.sync_api import sync_playwright

import turboshare

class QALogger:
    def __init__(self):
        self.log_entries = []
        self.passed_count = 0
        self.failed_count = 0

    def log(self, category, test_name, status, details="", payload=""):
        entry = {
            "category": category,
            "test_name": test_name,
            "status": status,
            "details": details,
            "payload": payload
        }
        self.log_entries.append(entry)
        if status == "PASS":
            self.passed_count += 1
            print(f"  [PASS] [{category}] {test_name}")
        else:
            self.failed_count += 1
            print(f"  [FAIL] [{category}] {test_name} | {details}")

def run_qa_suite():
    logger = QALogger()
    print("=" * 76)
    print("  TURBOSHARE LIVE QA & ADVERSARIAL CHALLENGE — EMPIRICAL VERIFICATION")
    print("=" * 76)

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 1: STATIC & SYNTACTIC CODE AUDIT
    # ──────────────────────────────────────────────────────────────────────────
    print("\n--- SECTION 1: JavaScript & CSS Syntactic / Semantic Audit ---")
    
    with open("turboshare.py", "r", encoding="utf-8") as f:
        ts_code = f.read()

    rendered_html = turboshare.render_page(8080)

    # 1.1 Extract & validate JavaScript syntax with Node.js
    script_m = re.search(r"<script>(.*?)</script>", rendered_html, re.DOTALL)
    if script_m:
        js_code = script_m.group(1)
        temp_js = os.path.join(tempfile.gettempdir(), "turboshare_eval.js")
        with open(temp_js, "w", encoding="utf-8") as tf:
            tf.write(js_code)
        
        proc = subprocess.run(["node", "-c", temp_js], capture_output=True, text=True)
        if proc.returncode == 0:
            logger.log("JS Syntax", "Node.js Syntax Evaluation (node -c)", "PASS", "0 syntax errors")
        else:
            logger.log("JS Syntax", "Node.js Syntax Evaluation (node -c)", "FAIL", 
                       f"SyntaxError detected: {proc.stderr.strip()}")
        if os.path.exists(temp_js):
            os.remove(temp_js)
    else:
        logger.log("JS Syntax", "Extract inline <script>", "FAIL", "No <script> tag found")

    # 1.2 DOM ID References Check
    dom_id_matches = re.findall(r"document\.getElementById\(['\"]([^'\"]+)['\"]\)", js_code)
    qs_matches = re.findall(r"querySelector\(['\"]#([^'\"]+)['\"]\)", js_code)
    all_queried_ids = set(dom_id_matches + qs_matches)
    html_ids = set(re.findall(r'id=["\']([^"\']+)["\']', rendered_html))
    missing_ids = [elem_id for elem_id in all_queried_ids if elem_id not in html_ids]
    if not missing_ids:
        logger.log("DOM Refs", "DOM Element ID Cross-Reference Audit", "PASS", f"All {len(all_queried_ids)} queried IDs exist in HTML")
    else:
        logger.log("DOM Refs", "DOM Element ID Cross-Reference Audit", "FAIL", f"Missing IDs in HTML: {missing_ids}")

    # 1.3 CSS Design Tokens & Media Queries
    style_m = re.search(r"<style>(.*?)</style>", rendered_html, re.DOTALL)
    if style_m:
        css_code = style_m.group(1)
        has_canvas = "--canvas: #090a0c;" in css_code
        has_tabular = "font-variant-numeric: tabular-nums" in css_code
        has_100dvh = "min-height: 100dvh" in css_code
        logger.log("CSS Tokens", "Obsidian Dark Theme Token (#090a0c)", "PASS" if has_canvas else "FAIL")
        logger.log("CSS Tokens", "Tabular Numerics CSS (zero layout jitter)", "PASS" if has_tabular else "FAIL")
        logger.log("CSS Tokens", "Mobile 100dvh Viewport Height", "PASS" if has_100dvh else "FAIL")

        media_queries = re.findall(r"@media\s*\([^\)]+\)", css_code)
        has_break = any("860px" in mq for mq in media_queries)
        logger.log("CSS Media", "Responsive @media queries defined (<860px)", "PASS" if has_break else "FAIL", f"{len(media_queries)} queries found")
    else:
        logger.log("CSS Tokens", "Extract inline <style>", "FAIL", "No <style> tag found")

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 2: LIVE BACKEND HTTP REST ENDPOINT AUDIT
    # ──────────────────────────────────────────────────────────────────────────
    print("\n--- SECTION 2: Live Backend REST API Endpoint Verification ---")
    
    test_dir = tempfile.mkdtemp(prefix="turboshare_live_qa_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8080
    turboshare.SERVER_PORT = test_port

    try:
        server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)
    except OSError:
        test_port = 8090
        turboshare.SERVER_PORT = test_port
        server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)

    server.allow_reuse_address = True
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    print(f"  Live Test Server Bound at: {base_url}")

    try:
        # 2.1 Root /
        req = urllib.request.urlopen(f"{base_url}/")
        root_html = req.read().decode("utf-8")
        logger.log("HTTP /", "GET / Root Dashboard Status 200", "PASS" if req.status == 200 else "FAIL")
        logger.log("HTTP /", "GET / Contains SVG Icons & No Cartoon Emojis", 
                   "PASS" if "<svg" in root_html and not any(e in root_html for e in ["⚡", "📂", "📱", "❓"]) else "FAIL")

        # 2.2 /api/interfaces
        req = urllib.request.urlopen(f"{base_url}/api/interfaces")
        ifaces = json.loads(req.read().decode("utf-8"))
        logger.log("HTTP API", "GET /api/interfaces returns valid interfaces array", 
                   "PASS" if isinstance(ifaces.get("interfaces"), list) and len(ifaces["interfaces"]) > 0 else "FAIL",
                   payload=str(ifaces))

        # 2.3 /api/qr
        req = urllib.request.urlopen(f"{base_url}/api/qr?url={urllib.parse.quote(base_url)}")
        ct = req.headers.get("Content-Type", "")
        qr_len = len(req.read())
        logger.log("HTTP API", "GET /api/qr returns valid QR image stream", 
                   "PASS" if ("image/png" in ct or "image/svg+xml" in ct) and qr_len > 50 else "FAIL",
                   details=f"Content-Type: {ct}, Size: {qr_len} bytes")

        # 2.4 /api/browse_host
        req = urllib.request.urlopen(f"{base_url}/api/browse_host")
        roots_data = json.loads(req.read().decode("utf-8"))
        has_drives = "drives" in roots_data and len(roots_data["drives"]) > 0
        logger.log("HTTP API", "GET /api/browse_host lists host physical drive roots",
                   "PASS" if has_drives else "FAIL",
                   payload=f"Drives count: {len(roots_data.get('drives', []))}")

        # Directory traversal within test_dir
        req = urllib.request.urlopen(f"{base_url}/api/browse_host?path={urllib.parse.quote(test_dir)}")
        dir_data = json.loads(req.read().decode("utf-8"))
        logger.log("HTTP API", "GET /api/browse_host?path=<dir> resolves subdirectories",
                   "PASS" if len(dir_data.get("subdirs", [])) == 2 else "FAIL",
                   details=f"Found subdirs: {[s['name'] for s in dir_data.get('subdirs', [])]}")

        # 2.5 /api/create_folder
        c_body = json.dumps({"parent": test_recv, "name": "empirically_created_dir"}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/create_folder", data=c_body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        c_res = json.loads(resp.read().decode("utf-8"))
        disk_exists = os.path.isdir(os.path.join(test_recv, "empirically_created_dir"))
        logger.log("HTTP API", "POST /api/create_folder creates directory on host disk",
                   "PASS" if c_res.get("success") and disk_exists else "FAIL")

        # 2.6 /api/set_path
        new_target = os.path.join(test_recv, "empirically_created_dir")
        s_body = json.dumps({"target": "recv", "path": new_target}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/set_path", data=s_body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        s_res = json.loads(resp.read().decode("utf-8"))
        logger.log("HTTP API", "POST /api/set_path updates active storage directory",
                   "PASS" if s_res.get("status") == "ok" and turboshare.UPLOAD_DIR == new_target else "FAIL")
        turboshare.UPLOAD_DIR = test_recv  # Restore

        # 2.7 /api/open_folder
        o_body = json.dumps({"target": "recv"}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/open_folder", data=o_body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        o_res = json.loads(resp.read().decode("utf-8"))
        logger.log("HTTP API", "POST /api/open_folder executes with is_local client detection",
                   "PASS" if o_res.get("success") and o_res.get("is_local") is True else "FAIL",
                   payload=str(o_res))

        # 2.8 /api/check & Resumable /api/upload
        # Upload chunk 1 (64KB)
        chunk1 = b"X" * (64 * 1024)
        chunk2 = b"Y" * (64 * 1024)
        req = urllib.request.Request(
            f"{base_url}/api/upload?path=resumable_artifact.bin&offset=0&target=recv",
            data=chunk1,
            headers={"Content-Length": str(len(chunk1))}
        )
        resp = urllib.request.urlopen(req)
        u1_res = json.loads(resp.read().decode("utf-8"))
        logger.log("HTTP API", "POST /api/upload initial chunk (64KB)",
                   "PASS" if u1_res.get("bytes") == 64 * 1024 else "FAIL")

        # Check byte offset
        req = urllib.request.urlopen(f"{base_url}/api/check?path=resumable_artifact.bin&target=recv")
        chk_res = json.loads(req.read().decode("utf-8"))
        logger.log("HTTP API", "GET /api/check validates written byte offset (64KB)",
                   "PASS" if chk_res.get("exists") and chk_res.get("size") == 64 * 1024 else "FAIL")

        # Resume chunk 2 (64KB at offset 64KB)
        req = urllib.request.Request(
            f"{base_url}/api/upload?path=resumable_artifact.bin&offset={64 * 1024}&target=recv",
            data=chunk2,
            headers={"Content-Length": str(len(chunk2))}
        )
        resp = urllib.request.urlopen(req)
        u2_res = json.loads(resp.read().decode("utf-8"))
        logger.log("HTTP API", "POST /api/upload resumed chunk 2 (64KB)",
                   "PASS" if u2_res.get("bytes") == 64 * 1024 else "FAIL")

        # Validate whole file integrity
        saved_file = os.path.join(test_recv, "resumable_artifact.bin")
        with open(saved_file, "rb") as f:
            whole_data = f.read()
        logger.log("HTTP API", "Resumed File Bit-Exact Integrity (128KB total)",
                   "PASS" if whole_data == (chunk1 + chunk2) else "FAIL")

        # 2.9 /download single file
        req = urllib.request.urlopen(f"{base_url}/download?tab=recv&path=resumable_artifact.bin")
        dl_data = req.read()
        logger.log("HTTP API", "GET /download streams saved file bit-for-bit",
                   "PASS" if dl_data == whole_data and req.status == 200 else "FAIL")

        # 2.10 /api/zip directory export
        req = urllib.request.urlopen(f"{base_url}/api/zip?tab=recv&path=")
        zip_raw = req.read()
        zip_buf = io.BytesIO(zip_raw)
        is_valid_zip = False
        try:
            with zipfile.ZipFile(zip_buf, "r") as zf:
                is_valid_zip = "resumable_artifact.bin" in zf.namelist()
        except Exception:
            is_valid_zip = False
        logger.log("HTTP API", "GET /api/zip packages folder into valid ZIP stream",
                   "PASS" if is_valid_zip else "FAIL")

    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

    # ──────────────────────────────────────────────────────────────────────────
    # SECTION 3: LIVE BROWSER DEVTOOLS & RESPONSIVE LAYOUT AUDIT
    # ──────────────────────────────────────────────────────────────────────────
    print("\n--- SECTION 3: Live Browser DevTools Console & Viewport Layout Audit ---")

    test_dir = tempfile.mkdtemp(prefix="turboshare_browser_live_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)
    with open(os.path.join(test_recv, "demo_doc.txt"), "w") as f: f.write("demo")

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8105
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True)
            
            # Desktop 1280x800 test
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()

            browser_console_errors = []
            page_uncaught_errors = []
            page.on("console", lambda msg: browser_console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda err: page_uncaught_errors.append(str(err)))

            page.goto(base_url)
            page.wait_for_timeout(1000)

            if len(page_uncaught_errors) == 0 and len(browser_console_errors) == 0:
                logger.log("Browser QA", "Chrome DevTools Console Audit (Desktop 1280px)", "PASS", "0 uncaught errors")
            else:
                logger.log("Browser QA", "Chrome DevTools Console Audit (Desktop 1280px)", "FAIL", 
                           f"Errors: {page_uncaught_errors or browser_console_errors}")

            # Mobile Viewport 360px x 740px
            page.set_viewport_size({"width": 360, "height": 740})
            page.wait_for_timeout(500)

            scroll_w = page.evaluate("() => document.documentElement.scrollWidth")
            client_w = page.evaluate("() => document.documentElement.clientWidth")
            
            if scroll_w <= client_w:
                logger.log("Browser QA", "Mobile 360px Viewport Horizontal Overflow Check", "PASS", f"scrollWidth={scroll_w}px <= clientWidth={client_w}px")
            else:
                logger.log("Browser QA", "Mobile 360px Viewport Horizontal Overflow Check", "FAIL", 
                           f"Horizontal blowout detected: scrollWidth={scroll_w}px exceeds clientWidth={client_w}px by {scroll_w - client_w}px")

            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

    print("\n" + "=" * 76)
    print(f"  TOTAL ASSERTIONS : {logger.passed_count + logger.failed_count}")
    print(f"  PASSED           : {logger.passed_count}")
    print(f"  FAILED           : {logger.failed_count}")
    print("=" * 76)

    return logger

if __name__ == "__main__":
    run_qa_suite()
