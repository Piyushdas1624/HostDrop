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
import hashlib
import concurrent.futures
import re

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import turboshare

def sha256(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def sha256_file(filepath):
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

def run_adversarial_stress_tests():
    print("=" * 70)
    print("  TURBOSHARE ADVERSARIAL FUNCTIONAL, API & SECURITY STRESS TEST SUITE")
    print("  Specialist: Challenger 2")
    print("=" * 70)

    test_root = tempfile.mkdtemp(prefix="turboshare_adv_test_")
    recv_dir = os.path.join(test_root, "inbox_recv")
    share_dir = os.path.join(test_root, "library_share")
    os.makedirs(recv_dir, exist_ok=True)
    os.makedirs(share_dir, exist_ok=True)

    turboshare.UPLOAD_DIR = recv_dir
    turboshare.HOST_SHARE = share_dir
    test_port = 8991
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("127.0.0.1", test_port), turboshare.TurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    passed = 0
    failed = 0
    findings = []

    def record_result(cond, name, details=""):
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  [PASS] {name}")
        else:
            failed += 1
            print(f"  [FAIL] {name} - {details}")
            findings.append((name, details))

    try:
        # -------------------------------------------------------------
        # SECTION 1: EMOJI REMOVAL & SVG COMPLIANCE ADVERSARIAL SCAN
        # -------------------------------------------------------------
        print("\n>>> SECTION 1: EMOJI REMOVAL & SVG COMPLIANCE ADVERSARIAL SCAN")
        req = urllib.request.urlopen(f"{base_url}/")
        rendered_html = req.read().decode("utf-8")

        emoji_pattern = re.compile(
            r'[\U0001F300-\U0001F5FF]|'  # Symbols & Pictographs
            r'[\U0001F600-\U0001F64F]|'  # Emoticons
            r'[\U0001F680-\U0001F6FF]|'  # Transport & Map
            r'[\U0001F700-\U0001F77F]|'  # Alchemical
            r'[\U0001F780-\U0001F7FF]|'  # Geometric Shapes Ext
            r'[\U0001F800-\U0001F8FF]|'  # Supplemental Arrows-C
            r'[\U0001F900-\U0001F9FF]|'  # Supplemental Symbols and Pictographs
            r'[\U0001FA00-\U0001FA6F]|'  # Chess Symbols
            r'[\U0001FA70-\U0001FAFF]|'  # Symbols and Pictographs Ext-A
            r'[\U00002702-\U000027B0]|'  # Dingbats
            r'[\U00002600-\U000026FF]|'  # Misc symbols (e.g. ⚡, ☕, ✈, ⚠)
            r'[\U00002300-\U000023FF]|'  # Misc Technical (e.g. ⌚, ⌛)
            r'[\U00002B50-\U00002B55]'   # Stars
        )
        found_emojis = emoji_pattern.findall(rendered_html)
        record_result(
            len(found_emojis) == 0,
            "100% Zero Emojis in Rendered HTML",
            f"Found {len(found_emojis)} emojis"
        )

        svg_count = rendered_html.count("<svg")
        record_result(
            svg_count >= 10,
            f"SVG Icon System Active ({svg_count} SVG elements found)",
            f"Only found {svg_count} SVGs"
        )

        # -------------------------------------------------------------
        # SECTION 2: OBSOLETE DEVELOPER JARGON ADVERSARIAL SCAN
        # -------------------------------------------------------------
        print("\n>>> SECTION 2: OBSOLETE DEVELOPER JARGON ADVERSARIAL SCAN")
        forbidden_phrases = [
            "received files storage",
            "host shared folder",
            "host shared files"
        ]
        html_lower = rendered_html.lower()
        for phrase in forbidden_phrases:
            present = phrase in html_lower
            record_result(
                not present,
                f"Absence of obsolete jargon: '{phrase}'",
                f"Forbidden phrase '{phrase}' was found in rendered HTML"
            )

        approved_tokens = [
            "Inbox",
            "Library",
            "Send Files to PC",
            "Where Sent Files Go",
            "Files sent from your phone or other computers",
            "Pick a folder on your PC",
            "Files transferred to this computer from connected devices",
            "Files shared by this computer available for you to download",
            "Host Computer",
            "Connected to PC"
        ]
        for token in approved_tokens:
            present = token.lower() in html_lower
            record_result(
                present,
                f"Presence of approved terminology: '{token}'",
                f"Required token '{token}' not found in rendered HTML"
            )

        # -------------------------------------------------------------
        # SECTION 3: DIRECTORY TRAVERSAL & SECURITY HARDENING
        # -------------------------------------------------------------
        print("\n>>> SECTION 3: DIRECTORY TRAVERSAL & SECURITY HARDENING")
        attack_paths = [
            "../",
            "../../",
            "../../../Windows/System32/calc.exe",
            "..\\..\\..\\Windows\\System32\\cmd.exe",
            "....//....//....//etc/passwd",
            "C:\\Windows\\System32\\drivers\\etc\\hosts",
            "C:/Windows/System32/drivers/etc/hosts",
            "\\\\127.0.0.1\\c$\\Windows",
            "\\\\localhost\\c$\\Windows",
            "\\Windows\\System32",
            "/etc/shadow",
            "/",
            "..",
            "subdir/../../secret.txt",
            "nested/../../../../Windows",
            "legit_name/../../../autoexec.bat",
            "..%2f..%2f..%2fwin.ini"
        ]

        all_blocked = True
        failed_attack = None
        for ap in attack_paths:
            res = turboshare.safe_path(recv_dir, ap)
            if res is not None:
                recv_abs = os.path.abspath(recv_dir)
                res_abs = os.path.abspath(res)
                if not res_abs.startswith(recv_abs) or res_abs == os.path.dirname(recv_abs):
                    all_blocked = False
                    failed_attack = ap
                    break
        record_result(all_blocked, "safe_path() blocks all relative/absolute path traversal attacks", f"Failed on {failed_attack}")

        # Live API Endpoint Traversal Stress Test
        # 1. Download traversal
        try:
            url = f"{base_url}/download?tab=recv&path=../../Windows/win.ini"
            req = urllib.request.Request(url)
            urllib.request.urlopen(req)
            record_result(False, "GET /download prevents directory traversal", "Returned HTTP 200 on traversal path")
        except urllib.error.HTTPError as e:
            record_result(e.code in (403, 404), f"GET /download prevents directory traversal (HTTP {e.code})")

        # 2. Check traversal
        req = urllib.request.urlopen(f"{base_url}/api/check?target=recv&path=../../Windows/System32/cmd.exe")
        check_res = json.loads(req.read().decode("utf-8"))
        record_result(check_res.get("exists") == False and check_res.get("size") == 0, "GET /api/check blocks directory traversal (exists=False)")

        # 3. Upload traversal attempt
        evil_data = b"malicious_payload_content"
        try:
            req = urllib.request.Request(
                f"{base_url}/api/upload?target=recv&path=../../evil_startup.bat&offset=0",
                data=evil_data,
                headers={"Content-Length": str(len(evil_data))}
            )
            urllib.request.urlopen(req)
            record_result(False, "POST /api/upload prevents directory traversal write", "Returned HTTP 200")
        except urllib.error.HTTPError as e:
            record_result(e.code in (400, 403, 404), f"POST /api/upload rejects directory traversal (HTTP {e.code})")
        
        evil_file = os.path.join(test_root, "evil_startup.bat")
        record_result(not os.path.exists(evil_file), "Malicious payload not created on disk")

        # 4. ZIP traversal attempt
        try:
            req = urllib.request.urlopen(f"{base_url}/api/zip?tab=recv&path=../../Windows")
            record_result(False, "GET /api/zip blocks directory traversal", "Returned HTTP 200")
        except urllib.error.HTTPError as e:
            record_result(e.code in (403, 404), f"GET /api/zip blocks directory traversal (HTTP {e.code})")

        # 5. List traversal attempt
        req = urllib.request.urlopen(f"{base_url}/api/list?tab=recv&path=../../Windows")
        list_res = json.loads(req.read().decode("utf-8"))
        record_result(list_res.get("items") == [] or "Windows" not in [i["name"] for i in list_res.get("items", [])], "GET /api/list blocks directory traversal")

        # -------------------------------------------------------------
        # SECTION 4: RESUMABLE CHUNKED UPLOAD & RECOVERY STRESS TEST
        # -------------------------------------------------------------
        print("\n>>> SECTION 4: RESUMABLE CHUNKED UPLOAD & RECOVERY STRESS TEST")
        test_payload_size = 2 * 1024 * 1024  # 2 MB
        test_data = bytes((i * 37 + 13) % 256 for i in range(test_payload_size))
        expected_hash = sha256(test_data)

        chunk_size = 200 * 1024  # 200 KB
        total_chunks = (test_payload_size + chunk_size - 1) // chunk_size

        filename = "stress_test_binary_2mb.bin"

        # Upload first 4 chunks (0 to 800 KB)
        for i in range(4):
            start = i * chunk_size
            end = min(start + chunk_size, test_payload_size)
            chunk = test_data[start:end]
            req = urllib.request.Request(
                f"{base_url}/api/upload?target=recv&path={filename}&offset={start}",
                data=chunk,
                headers={"Content-Length": str(len(chunk))}
            )
            resp = urllib.request.urlopen(req)
            res_json = json.loads(resp.read().decode("utf-8"))
            assert res_json.get("success") == True

        # Query /api/check to simulate client resume probe
        req = urllib.request.urlopen(f"{base_url}/api/check?target=recv&path={filename}")
        check_data = json.loads(req.read().decode("utf-8"))
        record_result(
            check_data.get("exists") == True and check_data.get("size") == 800 * 1024,
            "Chunked Upload Checkpoint: /api/check accurately reports 800 KB written",
            f"Reported {check_data}"
        )

        # Resume from 800 KB to completion
        for i in range(4, total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, test_payload_size)
            chunk = test_data[start:end]
            req = urllib.request.Request(
                f"{base_url}/api/upload?target=recv&path={filename}&offset={start}",
                data=chunk,
                headers={"Content-Length": str(len(chunk))}
            )
            resp = urllib.request.urlopen(req)
            res_json = json.loads(resp.read().decode("utf-8"))
            assert res_json.get("success") == True

        final_file_path = os.path.join(recv_dir, filename)
        disk_size = os.path.getsize(final_file_path)
        disk_hash = sha256_file(final_file_path)

        record_result(disk_size == test_payload_size, f"Resumed 2MB file size exact ({disk_size} bytes)")
        record_result(disk_hash == expected_hash, f"Resumed 2MB file SHA-256 exact bit-for-bit integrity ({disk_hash[:12]}...)")

        # Rewind & Truncation on Stale/Mismatched Offset
        rewind_chunk = b"X" * (100 * 1024)
        req = urllib.request.Request(
            f"{base_url}/api/upload?target=recv&path={filename}&offset={500 * 1024}",
            data=rewind_chunk,
            headers={"Content-Length": str(len(rewind_chunk))}
        )
        resp = urllib.request.urlopen(req)
        record_result(resp.status == 200, "Upload handles rewind offset with file truncate")
        rewound_size = os.path.getsize(final_file_path)
        record_result(rewound_size == 600 * 1024, f"File correctly truncated and rewritten to 600 KB (was {rewound_size} bytes)")

        # Complex Latin Filenames, Spaces, Symbols, Deep Paths
        complex_files = [
            ("Vacation Photo #1 & 2 (High-Res) + Final.jpg", b"photo_bytes_sample_12345"),
            ("nested/sub1/sub2/sub3/deep_document.pdf", b"pdf_nested_content_abcdef"),
            ("archive [2026] v1.0.tar.gz", b"sample_archive_content_789"),
            ("zero_byte_empty_file.empty", b"")
        ]

        for fname, fcontent in complex_files:
            encoded_name = urllib.parse.quote(fname)
            req = urllib.request.Request(
                f"{base_url}/api/upload?target=recv&path={encoded_name}&offset=0",
                data=fcontent,
                headers={"Content-Length": str(len(fcontent))}
            )
            resp = urllib.request.urlopen(req)
            r_json = json.loads(resp.read().decode("utf-8"))
            record_result(r_json.get("success") == True, f"Upload complex filename: '{fname}'")

            dl_req = urllib.request.urlopen(f"{base_url}/download?tab=recv&path={encoded_name}")
            dl_data = dl_req.read()
            record_result(dl_data == fcontent, f"Download verification matches for '{fname}'")

        # -------------------------------------------------------------
        # SECTION 5: CONCURRENT MULTI-CLIENT UPLOADS
        # -------------------------------------------------------------
        print("\n>>> SECTION 5: CONCURRENT MULTI-CLIENT UPLOADS")
        def upload_worker(client_id):
            c_fname = f"concurrent_client_{client_id}.bin"
            c_data = bytes([client_id % 256] * (128 * 1024)) # 128 KB
            req = urllib.request.Request(
                f"{base_url}/api/upload?target=recv&path={c_fname}&offset=0",
                data=c_data,
                headers={"Content-Length": str(len(c_data))}
            )
            resp = urllib.request.urlopen(req)
            return json.loads(resp.read().decode("utf-8")), c_fname, c_data

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = [executor.submit(upload_worker, i) for i in range(6)]
            concurrent_success = True
            for fut in concurrent.futures.as_completed(futures):
                res, c_fn, c_dt = fut.result()
                if not res.get("success"):
                    concurrent_success = False
                c_disk = os.path.join(recv_dir, c_fn)
                if not os.path.exists(c_disk) or open(c_disk, "rb").read() != c_dt:
                    concurrent_success = False

        record_result(concurrent_success, "6 Concurrent client chunk uploads complete with 0 collisions and bit-exact integrity")

        # -------------------------------------------------------------
        # SECTION 6: STREAMING ZIP DOWNLOAD & NESTED HIERARCHY
        # -------------------------------------------------------------
        print("\n>>> SECTION 6: STREAMING ZIP DOWNLOAD & NESTED HIERARCHY STRESS TEST")
        zip_test_dir = os.path.join(share_dir, "Project_Turboshare_Archive")
        os.makedirs(os.path.join(zip_test_dir, "docs", "empty_subfolder_a"), exist_ok=True)
        os.makedirs(os.path.join(zip_test_dir, "assets", "nested_level_1", "empty_subfolder_b"), exist_ok=True)
        os.makedirs(os.path.join(zip_test_dir, "empty_top_level_folder"), exist_ok=True)

        with open(os.path.join(zip_test_dir, "readme.txt"), "w", encoding="utf-8") as f:
            f.write("Root readme file content.")
        with open(os.path.join(zip_test_dir, "docs", "spec.json"), "w", encoding="utf-8") as f:
            f.write('{"version": "2.0", "status": "production"}')
        with open(os.path.join(zip_test_dir, "assets", "nested_level_1", "logo.bin"), "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4096)

        zip_url = f"{base_url}/api/zip?tab=share&path={urllib.parse.quote('Project_Turboshare_Archive')}"
        zip_req = urllib.request.urlopen(zip_url)
        record_result(zip_req.status == 200, "GET /api/zip returns HTTP 200")
        record_result(zip_req.headers.get("Content-Type") == "application/zip", "ZIP header Content-Type is application/zip")

        zip_bytes = zip_req.read()
        record_result(len(zip_bytes) > 500, f"ZIP payload streamed successfully ({len(zip_bytes)} bytes)")

        zip_buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(zip_buf, "r") as zf:
            corrupt = zf.testzip()
            record_result(corrupt is None, "ZIP archive passes internal CRC32/format checksum test")

            all_entries = zf.namelist()
            normalized_entries = [e.replace("\\", "/") for e in all_entries]

            has_readme = any("readme.txt" in e for e in normalized_entries)
            has_spec = any("spec.json" in e for e in normalized_entries)
            has_logo = any("logo.bin" in e for e in normalized_entries)
            record_result(has_readme and has_spec and has_logo, "ZIP contains all nested files across subfolders")

            has_empty_a = any("empty_subfolder_a/" in e or "empty_subfolder_a" in e for e in normalized_entries)
            has_empty_b = any("empty_subfolder_b/" in e or "empty_subfolder_b" in e for e in normalized_entries)
            has_empty_top = any("empty_top_level_folder/" in e or "empty_top_level_folder" in e for e in normalized_entries)
            record_result(has_empty_a and has_empty_b and has_empty_top, "ZIP preserves all 3 empty directories with directory entries")

            readme_entry = next(e for e in all_entries if "readme.txt" in e)
            record_result(zf.read(readme_entry).decode("utf-8") == "Root readme file content.", "ZIP nested file content bit-exact")

        # ZIP of entire Library root
        zip_root_req = urllib.request.urlopen(f"{base_url}/api/zip?tab=share&path=")
        record_result(zip_root_req.status == 200, "GET /api/zip on entire Library root returns HTTP 200")
        root_zip_bytes = zip_root_req.read()
        with zipfile.ZipFile(io.BytesIO(root_zip_bytes), "r") as zf:
            record_result(zf.testzip() is None, "Library root ZIP archive passes integrity check")

        # ZIP on non-existent folder
        try:
            urllib.request.urlopen(f"{base_url}/api/zip?tab=share&path=non_existent_folder_xyz")
            record_result(False, "GET /api/zip on non-existent folder returns 404", "Returned 200")
        except urllib.error.HTTPError as e:
            record_result(e.code == 404, f"GET /api/zip on non-existent folder returns 404 (HTTP {e.code})")

        # -------------------------------------------------------------
        # SECTION 7: API COMPATIBILITY & STATE MANAGEMENT
        # -------------------------------------------------------------
        print("\n>>> SECTION 7: API COMPATIBILITY & STATE MANAGEMENT")
        new_test_inbox = os.path.join(test_root, "switched_inbox")
        set_req = urllib.request.Request(
            f"{base_url}/api/set_path",
            data=json.dumps({"target": "recv", "path": new_test_inbox}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        set_resp = urllib.request.urlopen(set_req)
        set_json = json.loads(set_resp.read().decode("utf-8"))
        record_result(set_json.get("status") == "ok" and os.path.exists(new_test_inbox), "POST /api/set_path creates and switches inbox directory")
        record_result(turboshare.UPLOAD_DIR == new_test_inbox, "Server internal state synchronized")

        turboshare.UPLOAD_DIR = recv_dir

        cf_req = urllib.request.Request(
            f"{base_url}/api/create_folder",
            data=json.dumps({"parent": share_dir, "name": "Created_Via_API"}).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        cf_resp = urllib.request.urlopen(cf_req)
        cf_json = json.loads(cf_resp.read().decode("utf-8"))
        record_result(cf_json.get("success") == True and os.path.exists(os.path.join(share_dir, "Created_Via_API")), "POST /api/create_folder creates target directory")

        vp_req = urllib.request.urlopen(f"{base_url}/api/validate_path?path={urllib.parse.quote(share_dir)}")
        vp_json = json.loads(vp_req.read().decode("utf-8"))
        record_result(vp_json.get("valid") == True and vp_json.get("writable") == True, "GET /api/validate_path validates existing writable path")

        vp_bad_req = urllib.request.urlopen(f"{base_url}/api/validate_path?path={urllib.parse.quote(os.path.join(test_root, 'does_not_exist'))}")
        vp_bad_json = json.loads(vp_bad_req.read().decode("utf-8"))
        record_result(vp_bad_json.get("valid") == False, "GET /api/validate_path flags non-existent path")

        # -------------------------------------------------------------
        # SECTION 8: NON-ASCII / UNICODE FILENAME STRESS TEST (VULNERABILITY PROBE)
        # -------------------------------------------------------------
        print("\n>>> SECTION 8: NON-ASCII / UNICODE FILENAME STRESS TEST")
        unicode_fname = "日本語ファイル_テスト.txt"
        unicode_content = "こんにちは TurboShare 世界".encode("utf-8")
        
        # 1. Upload non-ASCII filename
        try:
            req = urllib.request.Request(
                f"{base_url}/api/upload?target=recv&path={urllib.parse.quote(unicode_fname)}&offset=0",
                data=unicode_content,
                headers={"Content-Length": str(len(unicode_content))}
            )
            resp = urllib.request.urlopen(req)
            r_json = json.loads(resp.read().decode("utf-8"))
            record_result(r_json.get("success") == True, f"Upload non-ASCII filename: '{unicode_fname}'")
        except Exception as e:
            record_result(False, f"Upload non-ASCII filename: '{unicode_fname}'", str(e))

        # 2. Download non-ASCII filename
        try:
            dl_req = urllib.request.urlopen(f"{base_url}/download?tab=recv&path={urllib.parse.quote(unicode_fname)}")
            dl_data = dl_req.read()
            record_result(dl_data == unicode_content, f"Download non-ASCII filename: '{unicode_fname}'")
        except Exception as e:
            record_result(
                False,
                f"Download non-ASCII filename: '{unicode_fname}'",
                f"HTTP Header Latin-1 Encoding Crash in send_header: {e}"
            )

        # 3. ZIP folder with non-ASCII name
        unicode_folder = os.path.join(share_dir, "папка_проекта_2026")
        os.makedirs(unicode_folder, exist_ok=True)
        with open(os.path.join(unicode_folder, "test.txt"), "w") as f:
            f.write("content")
        try:
            zip_u_req = urllib.request.urlopen(f"{base_url}/api/zip?tab=share&path={urllib.parse.quote('папка_проекта_2026')}")
            zip_u_bytes = zip_u_req.read()
            record_result(len(zip_u_bytes) > 0, "GET /api/zip with non-ASCII folder name")
        except Exception as e:
            record_result(
                False,
                "GET /api/zip with non-ASCII folder name",
                f"HTTP Header Latin-1 Encoding Crash in send_header: {e}"
            )

    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_root, ignore_errors=True)

    print("\n" + "=" * 70)
    print(f"  TOTAL ADVERSARIAL STRESS TEST RESULTS: {passed} PASSED, {failed} FAILED")
    print("=" * 70)

    if failed > 0:
        print(f"\n[FAILURES DETECTED]: {failed} tests failed.")
        for name, details in findings:
            print(f"  - {name}: {details}")
        return False
    else:
        print("\n[SUCCESS] ALL ADVERSARIAL STRESS TESTS PASSED WITH ZERO FAILURES!")
        return True

if __name__ == "__main__":
    success = run_adversarial_stress_tests()
    sys.exit(0 if success else 1)
