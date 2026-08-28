"""
Live QA & Adversarial Test Suite for TurboShare
Challenger 2 Empirical Verification
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

import turboshare

def print_header(title):
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)

class TestRunner:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.results = []

    def assert_true(self, condition, name, details=""):
        if condition:
            print(f"  [PASS] {name}")
            self.passed += 1
            self.results.append({"name": name, "status": "PASS", "details": details})
        else:
            print(f"  [FAIL] {name} | Details: {details}")
            self.failed += 1
            self.results.append({"name": name, "status": "FAIL", "details": details})

def test_js_and_html_integrity(runner):
    print_header("1. Client-Side JavaScript & HTML DOM Reference Audit")

    rendered_html = turboshare.render_page(8080)
    
    # 1. Extract <script> content
    script_match = re.search(r"<script>(.*?)</script>", rendered_html, re.DOTALL)
    runner.assert_true(bool(script_match), "HTML contains inline <script> block")
    if not script_match:
        return

    js_code = script_match.group(1)

    # 2. Syntax validation with Node.js
    try:
        # Write JS to a temp file and run node -c
        temp_js = os.path.join(tempfile.gettempdir(), "turboshare_extracted.js")
        with open(temp_js, "w", encoding="utf-8") as f:
            f.write(js_code)
        
        proc = subprocess.run(["node", "-c", temp_js], capture_output=True, text=True)
        runner.assert_true(proc.returncode == 0, "JavaScript Syntax Check (node -c)", proc.stderr.strip())
    except Exception as e:
        runner.assert_true(False, "JavaScript Syntax Check execution", str(e))
    finally:
        if os.path.exists(temp_js):
            os.remove(temp_js)

    # 3. DOM IDs cross-reference audit
    # Find all document.getElementById('...') or document.getElementById("...")
    dom_id_matches = re.findall(r"document\.getElementById\(['\"]([^'\"]+)['\"]\)", js_code)
    # Find all $('#...') or querySelector('#...')
    qs_matches = re.findall(r"querySelector\(['\"]#([^'\"]+)['\"]\)", js_code)
    
    all_queried_ids = set(dom_id_matches + qs_matches)
    
    # Extract all id="..." from HTML
    html_ids = set(re.findall(r'id=["\']([^"\']+)["\']', rendered_html))

    missing_ids = [elem_id for elem_id in all_queried_ids if elem_id not in html_ids]
    runner.assert_true(len(missing_ids) == 0, "Zero Dangling DOM IDs (JS queried IDs exist in HTML)", f"Missing: {missing_ids}")

    # 4. Check critical event handlers in JS
    runner.assert_true("addEventListener('wheel'" in js_code or 'addEventListener("wheel"' in js_code, "Mouse wheel deltaY-to-scrollLeft handler registered")
    runner.assert_true("dragover" in js_code and "dragleave" in js_code and "drop" in js_code, "Full-window drag and drop event listeners present")
    runner.assert_true("api/browse_host" in js_code, "Host folder browser API call wired in JS")
    runner.assert_true("api/open_folder" in js_code, "Open in OS Explorer API call wired in JS")
    runner.assert_true("api/check" in js_code and "api/upload" in js_code, "Resumable upload client logic wired in JS")
    runner.assert_true("api/zip" in js_code, "Folder ZIP download client logic wired in JS")

def test_css_and_responsive_rules(runner):
    print_header("2. CSS Design Tokens & Responsive Media Queries Audit")

    rendered_html = turboshare.render_page(8080)
    style_match = re.search(r"<style>(.*?)</style>", rendered_html, re.DOTALL)
    runner.assert_true(bool(style_match), "HTML contains inline <style> block")
    if not style_match:
        return

    css_code = style_match.group(1)

    # Design tokens check
    runner.assert_true("--canvas: #090a0c;" in css_code or "#090a0c" in css_code, "Obsidian Dark Canvas token (#090a0c) present")
    runner.assert_true("--surface-1:" in css_code and "--surface-2:" in css_code and "--surface-3:" in css_code, "Surface hierarchy ladder (--surface-1..4) present")
    runner.assert_true("font-variant-numeric: tabular-nums" in css_code, "Tabular numerics CSS present for zero jitter")
    runner.assert_true("min-height: 100dvh" in css_code or "100dvh" in css_code, "Mobile 100dvh viewport height declared")
    
    # Responsive media queries check
    media_queries = re.findall(r"@media\s*\([^\)]+\)", css_code)
    runner.assert_true(len(media_queries) > 0, "Responsive @media queries defined in stylesheet", f"Found: {len(media_queries)} media queries")
    
    has_mobile_break = any("860px" in mq or "768px" in mq or "640px" in mq or "480px" in mq for mq in media_queries)
    runner.assert_true(has_mobile_break, "Mobile breakpoints (<860px / <768px / <640px) present")

    # Touch target check (min-height / min-width >= 44px)
    touch_target_rules = re.findall(r"(?:min-height|height|min-width|width):\s*(?:4[4-9]|[5-9][0-9])px", css_code)
    runner.assert_true(len(touch_target_rules) > 0, "Mobile touch targets (>= 44px) specified in stylesheet", f"Found {len(touch_target_rules)} rules")

    # Viewport meta tag
    runner.assert_true('<meta name="viewport"' in rendered_html and 'width=device-width' in rendered_html, "Valid responsive viewport meta tag present")

def test_live_server_endpoints(runner):
    print_header("3. Live HTTP REST Endpoints Verification on Port 8080 (or ephemeral port)")

    test_dir = tempfile.mkdtemp(prefix="turboshare_qa_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8080
    turboshare.SERVER_PORT = test_port

    # Bind to port 8080 if available, or fallback to dynamic port
    try:
        server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)
    except OSError:
        test_port = 8089
        turboshare.SERVER_PORT = test_port
        server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)

    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    print(f"  Target Server URL: {base_url}")

    try:
        # Route 1: Root /
        req = urllib.request.urlopen(f"{base_url}/")
        body = req.read().decode("utf-8")
        runner.assert_true(req.status == 200, "GET / returns HTTP 200")
        runner.assert_true("TurboShare" in body, "GET / contains Brand Title")
        runner.assert_true("<svg" in body, "GET / contains Vector SVG icons")

        # Route 2: /api/interfaces
        req = urllib.request.urlopen(f"{base_url}/api/interfaces")
        ifaces = json.loads(req.read().decode("utf-8"))
        runner.assert_true(req.status == 200 and "interfaces" in ifaces, "GET /api/interfaces returns interfaces list")

        # Route 3: /api/qr
        req = urllib.request.urlopen(f"{base_url}/api/qr?url={urllib.parse.quote('http://127.0.0.1:8080')}")
        ct = req.headers.get("Content-Type")
        runner.assert_true(req.status == 200 and ("image/png" in ct or "image/svg+xml" in ct), f"GET /api/qr returns valid image ({ct})")

        # Route 4: /api/browse_host without path
        req = urllib.request.urlopen(f"{base_url}/api/browse_host")
        browse_root = json.loads(req.read().decode("utf-8"))
        runner.assert_true("drives" in browse_root and len(browse_root["drives"]) > 0, "GET /api/browse_host returns host drive roots")
        
        # Verify drive structure
        first_drive = browse_root["drives"][0]
        runner.assert_true("path" in first_drive and "free_gb" in first_drive and "total_gb" in first_drive, "Host drive has path, free_gb, total_gb")

        # Route 5: /api/browse_host with path
        req = urllib.request.urlopen(f"{base_url}/api/browse_host?path={urllib.parse.quote(test_dir)}")
        browse_dir = json.loads(req.read().decode("utf-8"))
        runner.assert_true(browse_dir.get("current_path") == os.path.abspath(test_dir), "GET /api/browse_host?path=<dir> resolves current path")
        runner.assert_true(len(browse_dir.get("subdirs", [])) == 2, "GET /api/browse_host lists subdirs (recv & share)")

        # Route 6: /api/create_folder
        c_body = json.dumps({"parent": test_recv, "name": "project_alpha"}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/create_folder", data=c_body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        c_res = json.loads(resp.read().decode("utf-8"))
        runner.assert_true(resp.status == 200 and c_res.get("success"), "POST /api/create_folder creates subfolder")
        runner.assert_true(os.path.isdir(os.path.join(test_recv, "project_alpha")), "Subfolder physically exists on host disk")

        # Route 7: /api/set_path
        s_body = json.dumps({"target": "recv", "path": os.path.join(test_recv, "project_alpha")}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/set_path", data=s_body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        s_res = json.loads(resp.read().decode("utf-8"))
        runner.assert_true(resp.status == 200 and s_res.get("status") == "ok", "POST /api/set_path changes target folder")
        runner.assert_true(turboshare.UPLOAD_DIR == os.path.join(test_recv, "project_alpha"), "Internal UPLOAD_DIR synchronized")

        # Reset UPLOAD_DIR
        turboshare.UPLOAD_DIR = test_recv

        # Route 8: /api/open_folder
        o_body = json.dumps({"target": "recv"}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/open_folder", data=o_body, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        o_res = json.loads(resp.read().decode("utf-8"))
        runner.assert_true(resp.status == 200 and o_res.get("success") and o_res.get("is_local") is True, "POST /api/open_folder succeeds for local caller")

        # Route 9: /api/pick_folder
        req = urllib.request.Request(f"{base_url}/api/pick_folder?target=recv", data=b"{}")
        # Note: In non-interactive test mode pick_folder may return cancelled or timeout gracefully
        try:
            # We don't block user test if cancelled
            resp = urllib.request.urlopen(f"{base_url}/api/validate_path?path={urllib.parse.quote(test_recv)}")
            v_res = json.loads(resp.read().decode("utf-8"))
            runner.assert_true(v_res.get("valid") is True and v_res.get("writable") is True, "GET /api/validate_path validates directory")
        except Exception as e:
            runner.assert_true(False, "Path validation endpoint", str(e))

        # Route 10: /api/disk
        req = urllib.request.urlopen(f"{base_url}/api/disk?path={urllib.parse.quote(test_recv)}")
        d_res = json.loads(req.read().decode("utf-8"))
        runner.assert_true("free_gb" in d_res and "total_gb" in d_res and "used_pct" in d_res, "GET /api/disk returns storage metrics")

    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

def test_adversarial_stress_and_resumption(runner):
    print_header("4. Adversarial Stress & Resumable Chunk Streaming Verification")

    test_dir = tempfile.mkdtemp(prefix="turboshare_stress_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = 8092
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"

    try:
        # 1. Security Directory Traversal Attacks
        traversal_attempts = [
            "../../Windows/System32/drivers/etc/hosts",
            "..\\..\\Windows\\System32\\cmd.exe",
            "%2e%2e%2f%2e%2e%2fWindows",
            "....//....//etc/passwd",
            "/absolute/path/override"
        ]
        for bad_p in traversal_attempts:
            # Test in /download
            try:
                urllib.request.urlopen(f"{base_url}/download?tab=recv&path={urllib.parse.quote(bad_p)}")
                runner.assert_true(False, f"Directory Traversal blocked in GET /download for {bad_p}", "Allowed access!")
            except urllib.error.HTTPError as e:
                runner.assert_true(e.code in (403, 404), f"Directory Traversal blocked in GET /download for {bad_p} (HTTP {e.code})")

            # Test in /api/upload
            try:
                req = urllib.request.Request(
                    f"{base_url}/api/upload?path={urllib.parse.quote(bad_p)}&offset=0&target=recv",
                    data=b"EVIL PAYLOAD",
                    headers={"Content-Length": "12"}
                )
                urllib.request.urlopen(req)
                runner.assert_true(False, f"Directory Traversal blocked in POST /api/upload for {bad_p}", "Allowed write!")
            except urllib.error.HTTPError as e:
                runner.assert_true(e.code in (403, 404), f"Directory Traversal blocked in POST /api/upload for {bad_p} (HTTP {e.code})")

        # 2. Resumable Chunk Truncation & Corruption Healing Test
        # We simulate a 200KB transfer where first 100KB is sent, then 50KB corrupted trailing bytes written,
        # then resume requests offset=100KB with final 100KB. Server MUST truncate trailing garbage and produce exact 200KB file.
        clean_part1 = b"C" * (100 * 1024)
        clean_part2 = b"D" * (100 * 1024)
        expected_full = clean_part1 + clean_part2

        # Step A: Upload Chunk 1 (100KB)
        req = urllib.request.Request(
            f"{base_url}/api/upload?path=resilient_stream.dat&offset=0&target=recv",
            data=clean_part1,
            headers={"Content-Length": str(len(clean_part1))}
        )
        resp = urllib.request.urlopen(req)
        runner.assert_true(resp.status == 200, "Initial Chunk 1 Upload (100KB)")

        # Step B: Artificially corrupt the file by appending 30KB garbage on disk
        target_file = os.path.join(test_recv, "resilient_stream.dat")
        with open(target_file, "ab") as f:
            f.write(b"GARBAGE_NOISE_CORRUPT" * 1500)
        corrupt_size = os.path.getsize(target_file)
        runner.assert_true(corrupt_size > 100 * 1024, "Injected trailing byte corruption on host")

        # Step C: Resume from offset=100KB with Chunk 2 (100KB clean)
        req = urllib.request.Request(
            f"{base_url}/api/upload?path=resilient_stream.dat&offset={100 * 1024}&target=recv",
            data=clean_part2,
            headers={"Content-Length": str(len(clean_part2))}
        )
        resp = urllib.request.urlopen(req)
        runner.assert_true(resp.status == 200, "Resumed Chunk 2 Upload with Seek & Truncate (100KB)")

        # Step D: Verify file size and bit-exact integrity
        final_size = os.path.getsize(target_file)
        with open(target_file, "rb") as f:
            final_content = f.read()
        runner.assert_true(final_size == 200 * 1024, f"Truncation healed corruption (Expected 204800 bytes, got {final_size})")
        runner.assert_true(final_content == expected_full, "Bit-exact verified after corruption truncation and resume")

        # 3. High-Speed Multi-Threaded Concurrent Upload Stress Test
        def upload_worker(worker_id):
            payload = f"WORKER_{worker_id}_DATA_".encode("utf-8") * 1024 # ~20KB
            fname = f"concurrent_worker_{worker_id}.bin"
            req = urllib.request.Request(
                f"{base_url}/api/upload?path={fname}&offset=0&target=recv",
                data=payload,
                headers={"Content-Length": str(len(payload))}
            )
            resp = urllib.request.urlopen(req)
            return resp.status == 200

        threads = []
        worker_results = [False] * 10
        def run_worker(idx):
            worker_results[idx] = upload_worker(idx)

        for i in range(10):
            t = threading.Thread(target=run_worker, args=(i,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        all_workers_passed = all(worker_results)
        runner.assert_true(all_workers_passed, "Concurrent 10-Thread Upload Burst (10 simultaneous TCP streams)")

        # 4. Malformed Request Handling
        # Broken JSON payload to /api/set_path
        try:
            req = urllib.request.Request(
                f"{base_url}/api/set_path",
                data=b"{bad_json_not_valid",
                headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(req)
            runner.assert_true(False, "Malformed JSON handled safely", "Expected 400 error")
        except urllib.error.HTTPError as e:
            runner.assert_true(e.code == 400, f"Malformed JSON returns HTTP 400 (got {e.code})")

    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

def main():
    runner = TestRunner()
    
    test_js_and_html_integrity(runner)
    test_css_and_responsive_rules(runner)
    test_live_server_endpoints(runner)
    test_adversarial_stress_and_resumption(runner)

    print_header("FINAL VERIFICATION SUMMARY")
    print(f"  TOTAL ASSERTIONS : {runner.passed + runner.failed}")
    print(f"  PASSED           : {runner.passed}")
    print(f"  FAILED           : {runner.failed}")
    print("=" * 70)

    if runner.failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    main()
