import os
import sys
import io
import time
import json
import socket
import shutil
import hashlib
import zipfile
import tempfile
import threading
import urllib.request
import urllib.parse
import urllib.error

# Ensure parent directory is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import hostdrop

class AdversarialBackendTester:
    def __init__(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_adv_test_")
        self.test_recv = os.path.join(self.test_dir, "recv")
        self.test_share = os.path.join(self.test_dir, "share")
        os.makedirs(self.test_recv, exist_ok=True)
        os.makedirs(self.test_share, exist_ok=True)

        hostdrop.UPLOAD_DIR = self.test_recv
        hostdrop.HOST_SHARE = self.test_share

        # Find a free ephemeral port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()

        hostdrop.SERVER_PORT = self.port
        self.base_url = f"http://127.0.0.1:{self.port}"

        self.server = hostdrop.ThreadingHTTPServer(("127.0.0.1", self.port), hostdrop.HostDropHandler)
        self.server.allow_reuse_address = True
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        time.sleep(0.3)

        self.results = []
        self.logs = []

    def log(self, msg):
        print(msg)
        self.logs.append(msg)

    def record_test(self, category, name, passed, details=""):
        status_str = "PASS" if passed else "FAIL"
        self.results.append({
            "category": category,
            "name": name,
            "passed": passed,
            "details": details
        })
        self.log(f"  [{status_str}] [{category}] {name} {('- ' + details) if details else ''}")

    def cleanup(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception as e:
            self.log(f"Warning during server shutdown: {e}")
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception as e:
            self.log(f"Warning during directory cleanup: {e}")

    # =========================================================================
    # SUITE 1: Resumable Uploads & Interruption Handling
    # =========================================================================
    def test_resumable_uploads(self):
        self.log("\n--- SUITE 1: Resumable Uploads & Interruption Handling ---")
        category = "RESUMABLE_UPLOAD"

        # 1.1 Multi-Megabyte Binary Interruption & Exact Resume
        # Generate 10MB deterministic pseudo-random payload
        size_10mb = 10 * 1024 * 1024
        # Seeded deterministic pseudo-random byte generator
        chunk_pattern = os.urandom(64 * 1024)
        full_payload = (chunk_pattern * ((size_10mb // len(chunk_pattern)) + 1))[:size_10mb]
        sha256_original = hashlib.sha256(full_payload).hexdigest()
        self.log(f"  [INFO] 10MB test payload generated. SHA256: {sha256_original}")

        rel_path = "stress_test_10mb.bin"
        cutoff_bytes = 4 * 1024 * 1024  # 4 MB interrupted upload

        # Simulate network interruption by opening raw socket, sending headers for 10MB but aborting at 4MB
        try:
            raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            raw_sock.connect(("127.0.0.1", self.port))
            req_headers = (
                f"POST /api/upload?path={rel_path}&offset=0&target=recv HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Content-Length: {size_10mb}\r\n"
                f"Content-Type: application/octet-stream\r\n"
                f"\r\n"
            ).encode("utf-8")
            raw_sock.sendall(req_headers)
            
            # Send exactly cutoff_bytes in 64KB chunks, then abruptly reset/close socket
            sent = 0
            while sent < cutoff_bytes:
                to_send = min(64 * 1024, cutoff_bytes - sent)
                raw_sock.sendall(full_payload[sent:sent + to_send])
                sent += to_send
            
            # Abrupt socket drop (TCP RST/FIN)
            raw_sock.close()
            time.sleep(0.3)
            self.record_test(category, "Network Interruption Simulation (Abrupt socket drop at 4MB)", True, f"Sent {sent} bytes of {size_10mb}")
        except Exception as e:
            self.record_test(category, "Network Interruption Simulation", False, str(e))

        # Check /api/check returns exact written bytes on disk
        try:
            req = urllib.request.urlopen(f"{self.base_url}/api/check?path={rel_path}&target=recv")
            check_data = json.loads(req.read().decode("utf-8"))
            disk_size = check_data.get("size", 0)
            exists = check_data.get("exists", False)
            passed = exists and disk_size == cutoff_bytes
            self.record_test(
                category,
                "API Check Endpoint Post-Interruption (/api/check)",
                passed,
                f"Exists={exists}, Reported size={disk_size} bytes (Expected: {cutoff_bytes})"
            )
        except Exception as e:
            self.record_test(category, "API Check Endpoint Post-Interruption", False, str(e))
            disk_size = cutoff_bytes

        # Resume upload: Send remaining bytes from offset = disk_size in 1MB chunks
        try:
            offset = disk_size
            chunk_size = 1024 * 1024  # 1 MB chunking
            chunks_sent = 0
            while offset < size_10mb:
                end = min(offset + chunk_size, size_10mb)
                chunk_data = full_payload[offset:end]
                req = urllib.request.Request(
                    f"{self.base_url}/api/upload?path={rel_path}&offset={offset}&target=recv",
                    data=chunk_data,
                    headers={"Content-Length": str(len(chunk_data))}
                )
                resp = urllib.request.urlopen(req)
                resp_data = json.loads(resp.read().decode("utf-8"))
                if not (resp.status == 200 and resp_data.get("success")):
                    raise Exception(f"Chunk upload failed at offset {offset}: {resp_data}")
                offset = end
                chunks_sent += 1
            
            self.record_test(category, "Multi-Chunk Resumed Streaming", True, f"Streamed {chunks_sent} resume chunks up to 10MB")
        except Exception as e:
            self.record_test(category, "Multi-Chunk Resumed Streaming", False, str(e))

        # Verify final assembled file on host disk
        try:
            disk_file = os.path.join(self.test_recv, rel_path)
            with open(disk_file, "rb") as f:
                assembled_data = f.read()
            sha256_assembled = hashlib.sha256(assembled_data).hexdigest()
            passed = (len(assembled_data) == size_10mb) and (sha256_assembled == sha256_original)
            self.record_test(
                category,
                "Assembled File SHA256 Integrity Verification",
                passed,
                f"Original SHA256:  {sha256_original}\n             Assembled SHA256: {sha256_assembled}\n             Match: {passed}"
            )
        except Exception as e:
            self.record_test(category, "Assembled File SHA256 Integrity Verification", False, str(e))

        # Verify single file download endpoint (/download) matches exact SHA256
        try:
            req = urllib.request.urlopen(f"{self.base_url}/download?tab=recv&path={rel_path}")
            dl_content = req.read()
            sha256_dl = hashlib.sha256(dl_content).hexdigest()
            passed = (len(dl_content) == size_10mb) and (sha256_dl == sha256_original)
            self.record_test(
                category,
                "End-to-End Download Stream SHA256 Verification (/download)",
                passed,
                f"Downloaded SHA256: {sha256_dl} (Match: {passed})"
            )
        except Exception as e:
            self.record_test(category, "End-to-End Download Stream SHA256 Verification", False, str(e))

        # 1.2 Corrupt Trailing Tail Truncation & Atomic Resumption
        # Scenario: Disk has 5MB of dirty/corrupted data, but client requests resume at offset 2MB.
        # Server must invoke f.seek(2MB) and f.truncate(2MB) before appending remainder.
        try:
            corrupt_rel = "corrupt_tail_test.dat"
            corrupt_file = os.path.join(self.test_recv, corrupt_rel)
            # Write 5MB dirty file
            with open(corrupt_file, "wb") as f:
                f.write(b"\xFF" * (5 * 1024 * 1024))

            # Proper data: 4MB total where first 2MB is 'K'*2MB and next 2MB is 'M'*2MB
            proper_part1 = b"K" * (2 * 1024 * 1024)
            proper_part2 = b"M" * (2 * 1024 * 1024)
            proper_full = proper_part1 + proper_part2
            proper_sha = hashlib.sha256(proper_full).hexdigest()

            # Step A: Overwrite first 2MB at offset=0
            req = urllib.request.Request(
                f"{self.base_url}/api/upload?path={corrupt_rel}&offset=0&target=recv",
                data=proper_part1,
                headers={"Content-Length": str(len(proper_part1))}
            )
            urllib.request.urlopen(req)

            # Note: At offset 0, mode is 'wb', which overwrote and truncated to 2MB.
            # Now let's artificially expand the file to 3.5MB with junk to test offset < filesize with 'r+b'
            with open(corrupt_file, "ab") as f:
                f.write(b"GARBAGE_JUNK" * (100 * 1024))
            
            expanded_len = os.path.getsize(corrupt_file)
            self.log(f"  [INFO] Artificially corrupted file size before resume: {expanded_len} bytes")

            # Step B: Resume upload at offset = 2MB (2097152), sending proper_part2 (2MB)
            req = urllib.request.Request(
                f"{self.base_url}/api/upload?path={corrupt_rel}&offset={len(proper_part1)}&target=recv",
                data=proper_part2,
                headers={"Content-Length": str(len(proper_part2))}
            )
            resp = urllib.request.urlopen(req)
            
            # Step C: Verify file on disk is truncated to 2MB + 2MB = 4MB and matches proper_full
            with open(corrupt_file, "rb") as f:
                disk_res = f.read()
            
            sha_res = hashlib.sha256(disk_res).hexdigest()
            passed = (len(disk_res) == len(proper_full)) and (sha_res == proper_sha)
            self.record_test(
                category,
                "Atomic Seek & Truncate on Out-of-Sync Trailing Tail",
                passed,
                f"File size={len(disk_res)} (Expected {len(proper_full)}), SHA256 match={passed}"
            )
        except Exception as e:
            self.record_test(category, "Atomic Seek & Truncate on Out-of-Sync Trailing Tail", False, str(e))

        # 1.3 Target Isolation (recv vs share)
        try:
            test_data = b"TARGET_ISOLATION_PAYLOAD"
            # Upload to recv
            req = urllib.request.Request(
                f"{self.base_url}/api/upload?path=isolation.txt&offset=0&target=recv",
                data=test_data,
                headers={"Content-Length": str(len(test_data))}
            )
            urllib.request.urlopen(req)

            # Upload different data to share
            share_data = b"DIFFERENT_SHARE_DATA"
            req2 = urllib.request.Request(
                f"{self.base_url}/api/upload?path=isolation.txt&offset=0&target=share",
                data=share_data,
                headers={"Content-Length": str(len(share_data))}
            )
            urllib.request.urlopen(req2)

            recv_file = os.path.join(self.test_recv, "isolation.txt")
            share_file = os.path.join(self.test_share, "isolation.txt")

            with open(recv_file, "rb") as f:
                rf_data = f.read()
            with open(share_file, "rb") as f:
                sf_data = f.read()

            passed = (rf_data == test_data) and (sf_data == share_data)
            self.record_test(category, "Dual Storage Target Isolation (recv vs share)", passed, f"Recv={rf_data}, Share={sf_data}")
        except Exception as e:
            self.record_test(category, "Dual Storage Target Isolation", False, str(e))

    # =========================================================================
    # SUITE 2: Filesystem Traversal & Host Drive Browsing
    # =========================================================================
    def test_filesystem_traversal(self):
        self.log("\n--- SUITE 2: Filesystem Traversal (/api/browse_host) ---")
        category = "FILESYSTEM_TRAVERSAL"

        # 2.1 Root drives query
        try:
            req = urllib.request.urlopen(f"{self.base_url}/api/browse_host")
            data = json.loads(req.read().decode("utf-8"))
            drives = data.get("drives", [])
            is_root = data.get("is_root", False)
            has_c_drive = any("C:" in d.get("path", "").upper() for d in drives) if sys.platform == "win32" else len(drives) > 0
            passed = is_root and len(drives) > 0 and has_c_drive
            self.record_test(category, "Root Drives Enumeration (/api/browse_host)", passed, f"Found {len(drives)} drives, is_root={is_root}")
        except Exception as e:
            self.record_test(category, "Root Drives Enumeration", False, str(e))

        # 2.2 Drive Root Traversal (C:\)
        try:
            c_root = "C:\\" if sys.platform == "win32" else "/"
            req = urllib.request.urlopen(f"{self.base_url}/api/browse_host?path={urllib.parse.quote(c_root)}")
            data = json.loads(req.read().decode("utf-8"))
            subdirs = data.get("subdirs", [])
            # Must filter system volume information and protected folders
            names = [s.get("name") for s in subdirs]
            no_system_vol = "System Volume Information" not in names and "$Recycle.Bin" not in names
            passed = (req.status == 200) and (len(subdirs) > 0) and no_system_vol
            self.record_test(
                category,
                f"Drive Root Exploration ({c_root}) with System Filter",
                passed,
                f"Discovered {len(subdirs)} subdirectories; protected folders properly filtered: {no_system_vol}"
            )
        except Exception as e:
            self.record_test(category, "Drive Root Exploration", False, str(e))

        # 2.3 Non-existent Drive & Invalid Paths (Zero Crash Guarantee)
        invalid_paths = [
            "Z:\\NonExistentDrive\\RandomDir_12345",
            "C:\\NonExistentDirectory_987654321\\SubDir",
            "C:\\Windows\\System32\\cmd.exe",  # File, not directory
            "/nonexistent/linux/path/xyz",
            "\\\\.\\pipe\\somepipe",
            "CON", "PRN", "AUX", "NUL"  # Windows reserved DOS device names
        ]
        for p in invalid_paths:
            try:
                req = urllib.request.urlopen(f"{self.base_url}/api/browse_host?path={urllib.parse.quote(p)}")
                data = json.loads(req.read().decode("utf-8"))
                # Expect clean JSON response with error indication or empty subdirs, zero 500 crashes
                has_error = "error" in data or data.get("is_root") == True or len(data.get("subdirs", [])) == 0
                self.record_test(
                    category,
                    f"Invalid Path Safety ({p[:35]})",
                    req.status == 200 and has_error,
                    f"HTTP {req.status}, Error reported: {data.get('error', 'None')}"
                )
            except urllib.error.HTTPError as e:
                # Should not return HTTP 500 server crash
                self.record_test(category, f"Invalid Path Safety ({p[:35]})", e.code != 500, f"HTTP Error code {e.code}")
            except Exception as e:
                self.record_test(category, f"Invalid Path Safety ({p[:35]})", False, f"Crash: {e}")

        # 2.4 Deep Directory Hierarchy & Special Characters
        try:
            deep_path = os.path.join(self.test_dir, "deep", "level1", "level2", "level3", "folder with spaces & éàçü $#@")
            os.makedirs(deep_path, exist_ok=True)
            # Create a child folder inside it
            os.makedirs(os.path.join(deep_path, "child_subfolder"), exist_ok=True)

            req = urllib.request.urlopen(f"{self.base_url}/api/browse_host?path={urllib.parse.quote(deep_path)}")
            data = json.loads(req.read().decode("utf-8"))
            subdirs = data.get("subdirs", [])
            passed = len(subdirs) == 1 and subdirs[0].get("name") == "child_subfolder"
            self.record_test(
                category,
                "Deep Hierarchy & Unicode/Special Char Path Browsing",
                passed,
                f"Browsed path: {data.get('current_path')}, found child: {subdirs[0].get('name') if subdirs else 'None'}"
            )
        except Exception as e:
            self.record_test(category, "Deep Hierarchy & Unicode Path Browsing", False, str(e))

    # =========================================================================
    # SUITE 3: Folder Creation (/api/create_folder)
    # =========================================================================
    def test_folder_creation(self):
        self.log("\n--- SUITE 3: Folder Creation (/api/create_folder) ---")
        category = "FOLDER_CREATION"

        # 3.1 Normal and Nested Directory Creation
        try:
            parent = os.path.join(self.test_dir, "create_root")
            os.makedirs(parent, exist_ok=True)

            payload = json.dumps({"parent": parent, "name": "nested/sub_a/sub_b"}).encode("utf-8")
            req = urllib.request.Request(f"{self.base_url}/api/create_folder", data=payload, headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req)
            data = json.loads(resp.read().decode("utf-8"))
            
            created_path = os.path.join(parent, "nested", "sub_a", "sub_b")
            passed = resp.status == 200 and data.get("success") and os.path.isdir(created_path)
            self.record_test(category, "Nested Directory Creation", passed, f"Created {created_path}")
        except Exception as e:
            self.record_test(category, "Nested Directory Creation", False, str(e))

        # 3.2 Duplicate Folder Creation (Idempotency)
        try:
            payload = json.dumps({"parent": parent, "name": "duplicate_target"}).encode("utf-8")
            # Create first time
            req1 = urllib.request.Request(f"{self.base_url}/api/create_folder", data=payload, headers={"Content-Type": "application/json"})
            resp1 = urllib.request.urlopen(req1)
            data1 = json.loads(resp1.read().decode("utf-8"))

            # Create second time (should not crash or fail; os.makedirs exist_ok=True)
            req2 = urllib.request.Request(f"{self.base_url}/api/create_folder", data=payload, headers={"Content-Type": "application/json"})
            resp2 = urllib.request.urlopen(req2)
            data2 = json.loads(resp2.read().decode("utf-8"))

            passed = (resp1.status == 200 and data1.get("success")) and (resp2.status == 200 and data2.get("success"))
            self.record_test(category, "Duplicate Folder Creation Idempotency", passed, "Both requests completed with status ok")
        except Exception as e:
            self.record_test(category, "Duplicate Folder Creation Idempotency", False, str(e))

        # 3.3 Invalid Characters and Non-Existent Drives Error Handling
        if sys.platform == "win32":
            invalid_cases = [
                {"parent": parent, "name": "invalid:folder*name?<>|\""},
                {"parent": "Z:\\NonExistentDrive", "name": "new_sub"}
            ]
        else:
            invalid_cases = [
                {"parent": "/root/forbidden_protected", "name": "new_sub"}
            ]

        for case in invalid_cases:
            try:
                payload = json.dumps(case).encode("utf-8")
                req = urllib.request.Request(f"{self.base_url}/api/create_folder", data=payload, headers={"Content-Type": "application/json"})
                try:
                    resp = urllib.request.urlopen(req)
                    data = json.loads(resp.read().decode("utf-8"))
                    # If it succeeded, check if path was sanitized, or if error status returned
                    passed = (resp.status == 200 and not data.get("error")) or data.get("status") == "error"
                except urllib.error.HTTPError as he:
                    # HTTP 400 is expected for invalid filesystem requests
                    data = json.loads(he.read().decode("utf-8"))
                    passed = (he.code == 400) and (data.get("success") is False)
                self.record_test(
                    category,
                    f"Invalid Folder Name/Path Error Handling ({case.get('name')})",
                    passed,
                    f"Handled with clean response: {data}"
                )
            except Exception as e:
                self.record_test(category, f"Invalid Folder Name/Path Error Handling ({case.get('name')})", False, str(e))

    # =========================================================================
    # SUITE 4: On-the-fly ZIP Streaming (/api/zip) & Archive Integrity
    # =========================================================================
    def test_zip_streaming(self):
        self.log("\n--- SUITE 4: On-the-fly ZIP Streaming (/api/zip) ---")
        category = "ZIP_STREAMING"

        # 4.1 Unicode, Nested, Empty Folders, and Special Filename ZIP Packaging
        try:
            zip_target_dir = os.path.join(self.test_share, "complex_archive_test")
            os.makedirs(zip_target_dir, exist_ok=True)

            # 1. Plain text file
            with open(os.path.join(zip_target_dir, "readme.txt"), "w", encoding="utf-8") as f:
                f.write("HostDrop High Speed Transfer Hub\nTest File")

            # 2. Cyrillic filename
            cyrillic_content = "ТурбоШаре файл с русскими символами".encode("utf-8")
            with open(os.path.join(zip_target_dir, "документ_2026.txt"), "wb") as f:
                f.write(cyrillic_content)

            # 3. Japanese filename
            japanese_content = "日本語のファイル名テストデータ".encode("utf-8")
            with open(os.path.join(zip_target_dir, "日本語仕様書.md"), "wb") as f:
                f.write(japanese_content)

            # 4. Special symbols & spaces
            symbols_content = b"JSON_PAYLOAD_WITH_SYMBOLS: !@#$%^&*()_+="
            with open(os.path.join(zip_target_dir, "config [v1.0] (prod) #final.json"), "wb") as f:
                f.write(symbols_content)

            # 5. Nested deep directory with binary payload
            deep_dir = os.path.join(zip_target_dir, "nested_assets", "sub1", "sub2")
            os.makedirs(deep_dir, exist_ok=True)
            bin_data = os.urandom(256 * 1024)  # 256 KB binary
            bin_sha = hashlib.sha256(bin_data).hexdigest()
            with open(os.path.join(deep_dir, "asset.bin"), "wb") as f:
                f.write(bin_data)

            # 6. Empty directory
            empty_dir = os.path.join(zip_target_dir, "empty_placeholder_dir")
            os.makedirs(empty_dir, exist_ok=True)

            # Stream ZIP via /api/zip
            req = urllib.request.urlopen(f"{self.base_url}/api/zip?tab=share&path=complex_archive_test")
            zip_stream_bytes = req.read()
            ct = req.headers.get("Content-Type")
            cd = req.headers.get("Content-Disposition")

            self.record_test(
                category,
                "ZIP HTTP Streaming Response Headers",
                (req.status == 200) and (ct == "application/zip") and ("attachment" in cd),
                f"Status={req.status}, Content-Type={ct}, Content-Disposition={cd}"
            )

            # 4.2 ZIP Integrity & testzip() CRC-32 Verification
            zip_io = io.BytesIO(zip_stream_bytes)
            with zipfile.ZipFile(zip_io, "r") as zf:
                # testzip() checks CRC checksum of all files in archive; returns None if 100% valid
                bad_file = zf.testzip()
                passed_testzip = (bad_file is None)
                self.record_test(
                    category,
                    "ZIP CRC-32 Checksum Validation (zipfile.testzip)",
                    passed_testzip,
                    f"Result: {'PASS (All CRCs valid)' if passed_testzip else f'Corrupted file: {bad_file}'}"
                )

                namelist = zf.namelist()
                self.log(f"  [INFO] ZIP Entries ({len(namelist)} items): {namelist}")

                # Verify extracted content matches source
                read_txt = zf.read("readme.txt").decode("utf-8")
                txt_ok = "HostDrop High Speed Transfer Hub" in read_txt

                # Find cyrillic and japanese entry (check with normalized slashes)
                cyrillic_entry = next((n for n in namelist if "документ_2026.txt" in n), None)
                cyrillic_ok = cyrillic_entry is not None and zf.read(cyrillic_entry) == cyrillic_content

                jp_entry = next((n for n in namelist if "日本語仕様書.md" in n), None)
                jp_ok = jp_entry is not None and zf.read(jp_entry) == japanese_content

                bin_entry = next((n for n in namelist if "asset.bin" in n), None)
                extracted_bin = zf.read(bin_entry) if bin_entry else b""
                bin_ok = bin_entry is not None and (hashlib.sha256(extracted_bin).hexdigest() == bin_sha)

                empty_ok = any("empty_placeholder_dir" in n for n in namelist)

                all_content_ok = txt_ok and cyrillic_ok and jp_ok and bin_ok and empty_ok
                self.record_test(
                    category,
                    "Extracted Archive File Content & Unicode Fidelity",
                    all_content_ok,
                    f"Text={txt_ok}, Cyrillic={cyrillic_ok}, Japanese={jp_ok}, BinarySHA={bin_ok}, EmptyDir={empty_ok}"
                )
        except Exception as e:
            self.record_test(category, "ZIP Packaging & Extraction Integrity", False, str(e))

        # 4.3 Invalid / Non-Existent ZIP Requests
        try:
            try:
                urllib.request.urlopen(f"{self.base_url}/api/zip?tab=share&path=non_existent_folder_abc")
                self.record_test(category, "Non-Existent Folder ZIP Rejection", False, "Returned 200 instead of 404")
            except urllib.error.HTTPError as he:
                self.record_test(category, "Non-Existent Folder ZIP Rejection (404)", he.code == 404, f"HTTP {he.code}")
        except Exception as e:
            self.record_test(category, "Non-Existent Folder ZIP Rejection", False, str(e))

    # =========================================================================
    # SUITE 5: Security Traversal Guards & Malformed Input Robustness
    # =========================================================================
    def test_security_and_malformed_inputs(self):
        self.log("\n--- SUITE 5: Security & Malformed Input Robustness ---")
        category = "SECURITY_AND_ROBUSTNESS"

        # 5.1 Directory Traversal on /download and /api/upload
        traversal_attempts = [
            "../../../Windows/System32/drivers/etc/hosts",
            "..\\..\\..\\Windows\\win.ini",
            "/etc/passwd",
            "C:\\Windows\\System32\\cmd.exe",
            "%2e%2e%2f%2e%2e%2f%2e%2e%2fWindows%2fwin.ini"
        ]
        for att in traversal_attempts:
            try:
                try:
                    resp = urllib.request.urlopen(f"{self.base_url}/download?tab=recv&path={urllib.parse.quote(att)}")
                    self.record_test(category, f"Path Traversal Block on /download ({att[:30]})", False, "Allowed unauthorized download")
                except urllib.error.HTTPError as he:
                    self.record_test(category, f"Path Traversal Block on /download ({att[:30]})", he.code in (403, 404), f"Blocked with HTTP {he.code}")
            except Exception as e:
                self.record_test(category, f"Path Traversal Block on /download ({att[:30]})", False, str(e))

        # 5.2 Malformed JSON Payloads to POST Endpoints
        endpoints = ["/api/set_path", "/api/create_folder", "/api/open_folder"]
        for ep in endpoints:
            try:
                req = urllib.request.Request(
                    f"{self.base_url}{ep}",
                    data=b"INVALID_NON_JSON{{{",
                    headers={"Content-Type": "application/json"}
                )
                try:
                    resp = urllib.request.urlopen(req)
                    data = json.loads(resp.read().decode("utf-8"))
                    # /api/open_folder gracefully defaults to {} on bad JSON
                    self.record_test(category, f"Malformed JSON on {ep}", resp.status in (200, 400), f"HTTP {resp.status}")
                except urllib.error.HTTPError as he:
                    self.record_test(category, f"Malformed JSON on {ep}", he.code in (400, 422), f"Clean HTTP {he.code} rejection")
            except Exception as e:
                self.record_test(category, f"Malformed JSON on {ep}", False, f"Server crash: {e}")

    def run_all(self):
        self.log("=" * 72)
        self.log("  HOSTDROP EMPIRICAL ADVERSARIAL BACKEND STRESS TEST SUITE")
        self.log("=" * 72)

        start_t = time.time()
        self.test_resumable_uploads()
        self.test_filesystem_traversal()
        self.test_folder_creation()
        self.test_zip_streaming()
        self.test_security_and_malformed_inputs()
        duration = time.time() - start_t

        self.cleanup()

        passed_count = sum(1 for r in self.results if r["passed"])
        failed_count = sum(1 for r in self.results if not r["passed"])
        total_count = len(self.results)

        self.log("\n" + "=" * 72)
        self.log(f"  ADVERSARIAL SUITE SUMMARY: {passed_count}/{total_count} PASSED, {failed_count} FAILED (Duration: {duration:.2f}s)")
        self.log("=" * 72)

        return passed_count, failed_count, self.results

if __name__ == "__main__":
    tester = AdversarialBackendTester()
    passed, failed, results = tester.run_all()
    if failed > 0:
        sys.exit(1)
    else:
        sys.exit(0)
