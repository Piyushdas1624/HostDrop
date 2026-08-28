#!/usr/bin/env python3
"""
TurboShare Host Folder Navigator Modal — Challenger 1 Adversarial Stress Test Suite
===================================================================================
Author: Challenger 1 (Automated Test & Backend Stress Verifier)
Target: Host Folder Navigation Modal Backend & Edge Cases
Covers:
  - Category A: Non-Existent, Malformed & Device Reserved Paths
  - Category B: Root Drive Hopping, Metric Precision & Multi-Drive Schema
  - Category C: Permission Errors, System Volume Isolation & Protected Directories
  - Category D: Deep Path Hierarchies (12+ levels) & Ancestor Chain Reversal
  - Category E: Folder Creation with Unicode, Accents, Complex Symbols & Invalid Characters
  - Category F: Path Setting (/api/set_path) & Real-Time Validation (/api/validate_path)
  - Category G: Client-side Breadcrumb & Path Parsing Simulation Algorithm
"""

import sys
import os
import time
import json
import socket
import urllib.request
import urllib.parse
import urllib.error
import threading
import tempfile
import shutil
import unittest
from http.server import HTTPServer

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
import turboshare

class ChallengerModalStressTests(unittest.TestCase):
    server = None
    server_thread = None
    port = None
    base_url = None
    temp_dir = None

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="turboshare_modal_stress_")
        cls.port = 19890
        for p in range(19890, 19990):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.bind(('127.0.0.1', p))
                s.close()
                cls.port = p
                break
            except OSError:
                continue

        cls.base_url = f"http://127.0.0.1:{cls.port}"
        
        # Configure turboshare state
        turboshare.UPLOAD_DIR = os.path.join(cls.temp_dir, "inbox")
        turboshare.HOST_SHARE = os.path.join(cls.temp_dir, "library")
        os.makedirs(turboshare.UPLOAD_DIR, exist_ok=True)
        os.makedirs(turboshare.HOST_SHARE, exist_ok=True)

        class SilentHandler(turboshare.TurboShareHandler):
            def log_message(self, format, *args):
                pass  # Suppress request spam

        cls.server = HTTPServer(('127.0.0.1', cls.port), SilentHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.5)

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        if cls.temp_dir and os.path.exists(cls.temp_dir):
            shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _get(self, path):
        url = f"{self.base_url}{path}"
        req = urllib.request.Request(url)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                return resp.status, resp.headers, data
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def _post_json(self, path, payload):
        url = f"{self.base_url}{path}"
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.headers, json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode('utf-8'))
            except Exception:
                body = {}
            return e.code, e.headers, body

    # ─────────────────────────────────────────────────────────────────────────
    # CATEGORY A: Non-Existent, Malformed & Device Reserved Paths
    # ─────────────────────────────────────────────────────────────────────────

    def test_A01_browse_empty_path_returns_roots(self):
        status, _, body = self._get("/api/browse_host?path=")
        self.assertEqual(status, 200)
        data = json.loads(body.decode('utf-8'))
        self.assertTrue(data.get("is_root"))
        self.assertEqual(data.get("current_path"), "")
        self.assertIsInstance(data.get("drives"), list)
        self.assertGreater(len(data.get("drives")), 0)

    def test_A02_browse_whitespace_and_quotes(self):
        for raw in ["   ", "%20%20", "%22%22", "%27%27"]:
            status, _, body = self._get(f"/api/browse_host?path={raw}")
            self.assertEqual(status, 200)
            data = json.loads(body.decode('utf-8'))
            self.assertTrue(data.get("is_root"))

    def test_A03_browse_nonexistent_drive(self):
        status, _, body = self._get("/api/browse_host?path=Z:%5CNonExistentDrive_Challenger999")
        self.assertEqual(status, 200)
        data = json.loads(body.decode('utf-8'))
        self.assertFalse(data.get("is_root"))
        self.assertIn("error", data)
        self.assertIn("Directory does not exist", data["error"])
        self.assertEqual(data.get("subdirs"), [])

    def test_A04_browse_nonexistent_subfolder_on_valid_drive(self):
        drive = "C:\\"
        non_existent = os.path.join(drive, "Challenger_NonExistent_Dir_999888777")
        enc = urllib.parse.quote(non_existent)
        status, _, body = self._get(f"/api/browse_host?path={enc}")
        self.assertEqual(status, 200)
        data = json.loads(body.decode('utf-8'))
        self.assertIn("error", data)
        self.assertEqual(data.get("subdirs"), [])

    def test_A05_browse_windows_device_reserved_names(self):
        for dev in ["CON", "PRN", "AUX", "NUL", "CON.txt"]:
            enc = urllib.parse.quote(dev)
            status, _, body = self._get(f"/api/browse_host?path={enc}")
            self.assertEqual(status, 200)
            data = json.loads(body.decode('utf-8'))
            # Either handled as root or invalid path with clean error, never server crash
            self.assertIsInstance(data.get("drives"), list)

    def test_A06_browse_relative_paths(self):
        for rel in [".", "..", "../..", "~"]:
            enc = urllib.parse.quote(rel)
            status, _, body = self._get(f"/api/browse_host?path={enc}")
            self.assertEqual(status, 200)
            data = json.loads(body.decode('utf-8'))
            self.assertIsInstance(data, dict)
            # Must return resolved path, drives, subdirs without throwing uncaught 500
            self.assertTrue("subdirs" in data or "drives" in data)

    def test_A07_browse_slashes_combinations(self):
        for path_variant in ["C:/", "C:////", "C:\\", "C:\\\\"]:
            enc = urllib.parse.quote(path_variant)
            status, _, body = self._get(f"/api/browse_host?path={enc}")
            self.assertEqual(status, 200)
            data = json.loads(body.decode('utf-8'))
            self.assertIsInstance(data.get("subdirs"), list)
            self.assertIsInstance(data.get("drives"), list)

    # ─────────────────────────────────────────────────────────────────────────
    # CATEGORY B: Root Drive Hopping, Metric Precision & Multi-Drive Schema
    # ─────────────────────────────────────────────────────────────────────────

    def test_B01_drive_enumeration_complete_schema(self):
        status, _, body = self._get("/api/browse_host?path=roots")
        self.assertEqual(status, 200)
        data = json.loads(body.decode('utf-8'))
        drives = data.get("drives", [])
        self.assertGreater(len(drives), 0)

        required_keys = ["path", "name", "letter", "label", "free_gb", "total_gb", "used_gb", "used_pct", "used_percent", "is_system"]
        for d in drives:
            for k in required_keys:
                self.assertIn(k, d, f"Missing key '{k}' in drive item: {d}")
            # Type validations
            self.assertIsInstance(d["used_pct"], int)
            self.assertIsInstance(d["used_percent"], (int, float))
            self.assertIsInstance(d["is_system"], bool)
            self.assertGreaterEqual(d["used_percent"], 0.0)
            self.assertLessEqual(d["used_percent"], 100.0)

    def test_B02_system_drive_identified(self):
        drives = turboshare.get_host_drives()
        if sys.platform == "win32":
            c_drives = [d for d in drives if d.get("letter", "").upper() == "C"]
            if c_drives:
                self.assertTrue(c_drives[0]["is_system"])
                self.assertIn("OS (C:)", c_drives[0]["label"])

    def test_B03_root_drive_hopping_consistency(self):
        drives = turboshare.get_host_drives()
        for d in drives:
            enc = urllib.parse.quote(d["path"])
            status, _, body = self._get(f"/api/browse_host?path={enc}")
            self.assertEqual(status, 200)
            data = json.loads(body.decode('utf-8'))
            self.assertEqual(data.get("error"), None)
            self.assertIsInstance(data.get("subdirs"), list)
            # Root drive parent path should be empty string (ready for root hop)
            self.assertEqual(data.get("parent_path"), "")
            # Storage metrics must be populated
            self.assertIn("free_gb", data)
            self.assertIn("total_gb", data)
            self.assertIn("used_percent", data)

    # ─────────────────────────────────────────────────────────────────────────
    # CATEGORY C: Permission Errors, System Volume Isolation & Protected Directories
    # ─────────────────────────────────────────────────────────────────────────

    def test_C01_system_protected_folders_filtered_on_c_drive(self):
        if sys.platform == "win32" and os.path.exists("C:\\"):
            status, _, body = self._get("/api/browse_host?path=C%3A%5C")
            self.assertEqual(status, 200)
            data = json.loads(body.decode('utf-8'))
            subdir_names = [s["name"] for s in data.get("subdirs", [])]
            
            # System Volume Information and $Recycle.Bin must be excluded from UI
            self.assertNotIn("System Volume Information", subdir_names)
            self.assertNotIn("Recovery", subdir_names)
            for name in subdir_names:
                self.assertFalse(name.startswith("$"), f"Found un-filtered system folder: {name}")

    def test_C02_permission_denied_graceful_handling(self):
        if sys.platform == "win32" and os.path.exists("C:\\System Volume Information"):
            enc = urllib.parse.quote("C:\\System Volume Information")
            status, _, body = self._get(f"/api/browse_host?path={enc}")
            self.assertEqual(status, 200)
            data = json.loads(body.decode('utf-8'))
            # Must return error or empty subdirs, not crash
            self.assertTrue("error" in data or data.get("subdirs") == [])

    def test_C03_create_folder_in_protected_system_dir_rejected(self):
        if sys.platform == "win32":
            forbidden_parent = "C:\\Windows\\System32"
            status, _, resp = self._post_json("/api/create_folder", {
                "parent": forbidden_parent,
                "name": "challenger_forbidden_test_dir_123"
            })
            # Should either return HTTP 400 or JSON error
            if status == 200:
                self.assertFalse(resp.get("success", True) and resp.get("status") == "ok")
            else:
                self.assertEqual(status, 400)
                self.assertFalse(resp.get("success", False))

    # ─────────────────────────────────────────────────────────────────────────
    # CATEGORY D: Deep Path Hierarchies & Ancestor Chain Reversal
    # ─────────────────────────────────────────────────────────────────────────

    def test_D01_deep_path_hierarchy_navigation(self):
        # Create 12 nested folders
        current = os.path.join(self.temp_dir, "deep_root")
        chain = [current]
        for i in range(1, 13):
            current = os.path.join(current, f"level_{i:02d}")
            os.makedirs(current, exist_ok=True)
            chain.append(current)

        # Browse into deepest level
        deepest = chain[-1]
        enc = urllib.parse.quote(deepest)
        status, _, body = self._get(f"/api/browse_host?path={enc}")
        self.assertEqual(status, 200)
        data = json.loads(body.decode('utf-8'))
        self.assertEqual(os.path.normpath(data.get("current_path")), os.path.normpath(deepest))
        self.assertEqual(os.path.normpath(data.get("parent_path")), os.path.normpath(chain[-2]))

        # Trace back up to root using parent_path
        curr_p = deepest
        levels_traversed = 0
        while curr_p and curr_p != self.temp_dir:
            enc = urllib.parse.quote(curr_p)
            status, _, b = self._get(f"/api/browse_host?path={enc}")
            self.assertEqual(status, 200)
            d = json.loads(b.decode('utf-8'))
            curr_p = d.get("parent_path")
            levels_traversed += 1
            if levels_traversed > 20:
                break
        self.assertGreaterEqual(levels_traversed, 12)

    # ─────────────────────────────────────────────────────────────────────────
    # CATEGORY E: Folder Creation with Unicode, Accents, Complex Symbols & Invalid Characters
    # ─────────────────────────────────────────────────────────────────────────

    def test_E01_create_folder_unicode_and_symbols(self):
        test_names = [
            "日本語フォルダ_テスト_2026",
            "测试目录_TurboShare",
            "Пакет_Документов_Host",
            "Dossier_Élégant & Café 100%",
            "Special [Brackets] & (Parentheses) + #Hash $Dollar @At"
        ]

        parent_dir = os.path.join(self.temp_dir, "unicode_tests")
        os.makedirs(parent_dir, exist_ok=True)

        for name in test_names:
            status, _, resp = self._post_json("/api/create_folder", {
                "parent": parent_dir,
                "name": name
            })
            self.assertEqual(status, 200)
            self.assertTrue(resp.get("success") or resp.get("status") == "ok")
            created_path = resp.get("path")
            self.assertTrue(os.path.exists(created_path), f"Folder was not created: {created_path}")
            self.assertTrue(os.path.isdir(created_path))

            # Browse parent and verify name appears in subdirs
            enc_parent = urllib.parse.quote(parent_dir)
            s, _, b = self._get(f"/api/browse_host?path={enc_parent}")
            self.assertEqual(s, 200)
            d = json.loads(b.decode('utf-8'))
            found = any(s_item["name"] == name for s_item in d.get("subdirs", []))
            self.assertTrue(found, f"Created folder '{name}' not found in subdirs list")

    def test_E02_create_folder_idempotency(self):
        target_dir = os.path.join(self.temp_dir, "idempotent_folder")
        for _ in range(2):
            status, _, resp = self._post_json("/api/create_folder", {
                "path": target_dir
            })
            self.assertEqual(status, 200)
            self.assertTrue(resp.get("success") or resp.get("status") == "ok")
            self.assertTrue(os.path.exists(target_dir))

    def test_E03_create_folder_invalid_windows_characters(self):
        invalid_names = [
            'folder*asterisk',
            'folder?question',
            'folder:colon',
            'folder"quote',
            'folder<less',
            'folder>greater',
            'folder|pipe'
        ]
        parent_dir = os.path.join(self.temp_dir, "invalid_char_tests")
        os.makedirs(parent_dir, exist_ok=True)

        for name in invalid_names:
            status, _, resp = self._post_json("/api/create_folder", {
                "parent": parent_dir,
                "name": name
            })
            if sys.platform == "win32":
                # Must reject with error status, not create corrupt directory or crash
                self.assertFalse(resp.get("success", False) and resp.get("status") == "ok")

    def test_E04_create_folder_empty_and_missing_payload(self):
        status, _, resp = self._post_json("/api/create_folder", {})
        self.assertIn(status, [200, 400])
        # Even if 200 or 400, server must not crash

    # ─────────────────────────────────────────────────────────────────────────
    # CATEGORY F: Path Setting & Real-Time Validation
    # ─────────────────────────────────────────────────────────────────────────

    def test_F01_set_path_recv_and_share_isolation(self):
        recv_path = os.path.join(self.temp_dir, "new_inbox")
        share_path = os.path.join(self.temp_dir, "new_library")

        # Set recv path
        status, _, resp = self._post_json("/api/set_path", {"path": recv_path, "type": "recv"})
        self.assertEqual(status, 200)
        self.assertEqual(resp.get("status"), "ok")
        self.assertEqual(os.path.normpath(turboshare.UPLOAD_DIR), os.path.normpath(recv_path))

        # Set share path
        status, _, resp = self._post_json("/api/set_path", {"path": share_path, "type": "share"})
        self.assertEqual(status, 200)
        self.assertEqual(resp.get("status"), "ok")
        self.assertEqual(os.path.normpath(turboshare.HOST_SHARE), os.path.normpath(share_path))

    def test_F02_validate_path_endpoint(self):
        valid_dir = os.path.join(self.temp_dir, "validate_me")
        os.makedirs(valid_dir, exist_ok=True)

        enc_valid = urllib.parse.quote(valid_dir)
        status, _, body = self._get(f"/api/validate_path?path={enc_valid}")
        self.assertEqual(status, 200)
        data = json.loads(body.decode('utf-8'))
        self.assertTrue(data.get("valid"))
        self.assertTrue(data.get("exists"))
        self.assertTrue(data.get("writable"))

        # Non-existent
        enc_invalid = urllib.parse.quote(os.path.join(self.temp_dir, "does_not_exist_456"))
        status, _, body = self._get(f"/api/validate_path?path={enc_invalid}")
        self.assertEqual(status, 200)
        data = json.loads(body.decode('utf-8'))
        self.assertFalse(data.get("valid"))
        self.assertFalse(data.get("exists"))

    # ─────────────────────────────────────────────────────────────────────────
    # CATEGORY G: Breadcrumb & Path Parsing Simulation Algorithm
    # ─────────────────────────────────────────────────────────────────────────

    def test_G01_breadcrumb_path_parsing_windows(self):
        def parse_segments_sim(full_path):
            if not full_path:
                return []
            normalized = full_path.replace("\\", "/")
            is_windows = bool(normalized and len(normalized) > 1 and normalized[1] == ':')
            segments = []
            parts = [p for p in normalized.split('/') if p]
            if is_windows and parts:
                drive_letter = parts[0]
                accumulated = drive_letter + "\\"
                segments.append({"name": drive_letter + "\\", "path": accumulated})
                for i in range(1, len(parts)):
                    accumulated = accumulated + ("" if accumulated.endswith("\\") else "\\") + parts[i]
                    segments.append({"name": parts[i], "path": accumulated})
            else:
                accumulated = ""
                segments.append({"name": "/", "path": "/"})
                for i in range(len(parts)):
                    accumulated += "/" + parts[i]
                    segments.append({"name": parts[i], "path": accumulated})
            return segments

        test_path = "C:\\Users\\piklu\\Documents\\turboshare\\backend\\routes"
        segs = parse_segments_sim(test_path)
        self.assertEqual(len(segs), 7)
        self.assertEqual(segs[0]["name"], "C:\\")
        self.assertEqual(segs[0]["path"], "C:\\")
        self.assertEqual(segs[1]["name"], "Users")
        self.assertEqual(segs[1]["path"], "C:\\Users")
        self.assertEqual(segs[-1]["name"], "routes")
        self.assertEqual(segs[-1]["path"], "C:\\Users\\piklu\\Documents\\turboshare\\backend\\routes")


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ChallengerModalStressTests)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(f"\n==================================================================")
    print(f"CHALLENGER 1 STRESS TEST RESULTS: {result.testsRun - len(result.failures) - len(result.errors)}/{result.testsRun} PASSED, {len(result.failures)} FAILED, {len(result.errors)} ERRORS")
    print(f"==================================================================")
    sys.exit(0 if result.wasSuccessful() else 1)
