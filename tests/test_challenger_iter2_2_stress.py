#!/usr/bin/env python3
"""
HostDrop Adversarial Empirical Stress Test Suite — Challenger Iteration 2-2
Focus Areas:
1. NTFS Path Traversal Defenses in safe_path():
   - 8.3 short name escapes (PROGRA~1, DOCUME~1, mixed case, nested, traversal combinations)
   - Extended device & UNC namespaces (\\\\?\\UNC, \\\\?\\Volume, \\\\.\\PhysicalDrive0, \\\\?\\GLOBALROOT, etc.)
   - Null bytes (raw \\0, single-encoded %00, double-encoded %2500, multi-encoded up to 6x, null in base_dir)
   - Alternate Data Streams (::$DATA, :stream, ::$INDEX_ALLOCATION, single/double/triple encoded colons)
   - Edge cases: trailing dots/spaces, deep recursion, mixed slashes, legitimate subpaths
2. Rate Limiting and Exponential Tarpitting on /api/auth:
   - 5 failed attempts enforce increasing delays D(k) = min(base * 2^(k-1), max)
   - 6th attempt locks out with HTTP 429 and Retry-After header
   - GET (?key=...) and POST (JSON body) verification
   - Multi-threaded concurrent attacks
   - IP isolation & counter reset on valid authentication
   - Header spoofing resistance on direct connections
"""

import io
import json
import os
import secrets
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
import urllib.error
import socket

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import auth
import hostdrop


class MockHTTPHandler:
    """Mock HTTP Handler for route testing."""

    def __init__(self, method="GET", path="/", headers=None, client_ip="127.0.0.1", body=None):
        self.command = method
        self.path = path
        self.headers = headers or {}
        self.client_address = (client_ip, 49152)
        self.response_status = None
        self.response_headers = {}
        self.wfile = io.BytesIO()
        self.rfile = io.BytesIO(body if body else b"")

    def send_response(self, code):
        self.response_status = code

    def send_header(self, keyword, value):
        self.response_headers[keyword] = str(value)

    def end_headers(self):
        pass

    def get_body(self):
        return self.wfile.getvalue()

    def get_json(self):
        raw = self.get_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def is_physical_localhost(self):
        return auth.is_physical_localhost(self)


class TestNTFSPathTraversalAdversarial(unittest.TestCase):
    """Adversarial stress-testing of safe_path() against NTFS traversal & escape vectors."""

    def setUp(self):
        self.temp_root = tempfile.mkdtemp(prefix="ts_challenger_safe_path_")
        self.base_dir = os.path.join(self.temp_root, "sandbox")
        os.makedirs(self.base_dir, exist_ok=True)

        # Create valid internal files for comparison
        self.valid_file = os.path.join(self.base_dir, "valid.txt")
        with open(self.valid_file, "w", encoding="utf-8") as f:
            f.write("safe content")

        self.nested_dir = os.path.join(self.base_dir, "nested", "subfolder")
        os.makedirs(self.nested_dir, exist_ok=True)
        self.nested_file = os.path.join(self.nested_dir, "data.json")
        with open(self.nested_file, "w", encoding="utf-8") as f:
            f.write("{}")

    def tearDown(self):
        shutil.rmtree(self.temp_root, ignore_errors=True)

    # ──────────────────────────────────────────────────────────────────────────
    # 1. 8.3 Short Name Traversal Attacks
    # ──────────────────────────────────────────────────────────────────────────
    def test_ntfs_8dot3_short_names_directory_escape(self):
        """Stress-test 8.3 short name patterns attempting to escape the sandbox."""
        vectors = [
            "PROGRA~1",
            "progra~1",
            "Progra~1",
            "DOCUME~1",
            "docume~1",
            "..\\PROGRA~1",
            "../PROGRA~1",
            "..\\DOCUME~1",
            "../DOCUME~1",
            "../../PROGRA~1",
            "..\\..\\PROGRA~1",
            "sub/../../PROGRA~1",
            "sub/../../DOCUME~1",
            "nested/subfolder/../../../PROGRA~1",
            "nested\\subfolder\\..\\..\\..\\PROGRA~1",
            "nested/subfolder/../../../../PROGRA~1",
            "nested/subfolder/../../../../WINDOWS/SYSTEM~1",
            "C:/PROGRA~1",
            "C:\\PROGRA~1",
            "C:PROGRA~1",
            "c:progra~1",
            "D:/PROGRA~1",
            "\\PROGRA~1",
            "/PROGRA~1",
            "\\\\?\\C:\\PROGRA~1",
            "//?/C:/PROGRA~1",
            "folder/../../PROGRA~1/Windows/System32",
            "%2e%2e%2fPROGRA~1",
            "%2e%2e%5cPROGRA~1",
            "..%2fPROGRA~1",
            "..%5cPROGRA~1",
            "%252e%252e%252fPROGRA~1",
            "%252e%252e%255cPROGRA~1",
        ]

        for vec in vectors:
            with self.subTest(vector=vec):
                res = hostdrop.safe_path(self.base_dir, vec)
                # If the vector resolves, it MUST be strictly contained inside base_dir
                if res is not None:
                    real_base = os.path.realpath(os.path.abspath(self.base_dir))
                    self.assertEqual(
                        os.path.commonpath([real_base, res]),
                        real_base,
                        f"8.3 vector {vec!r} escaped base_dir! Resolved to: {res}"
                    )
                    # For absolute or escape vectors, safe_path MUST return None
                    if any(escape_tok in vec for escape_tok in ["..", ":", "\\PROGRA", "/PROGRA", "\\?"]):
                        self.assertIsNone(res, f"Expected None for escape vector {vec!r}, got: {res}")

    def test_ntfs_8dot3_legitimate_internal_folder(self):
        """Verify an 8.3-like filename strictly INSIDE base_dir resolves safely and does not false-positive."""
        internal_83 = os.path.join(self.base_dir, "MYDOCU~1")
        os.makedirs(internal_83, exist_ok=True)
        sample_file = os.path.join(internal_83, "test.txt")
        with open(sample_file, "w", encoding="utf-8") as f:
            f.write("internal 8.3 file")

        res = hostdrop.safe_path(self.base_dir, "MYDOCU~1/test.txt")
        self.assertIsNotNone(res)
        real_base = os.path.realpath(os.path.abspath(self.base_dir))
        self.assertEqual(os.path.commonpath([real_base, res]), real_base)

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Extended Win32 & Device Namespaces
    # ──────────────────────────────────────────────────────────────────────────
    def test_extended_win32_and_device_namespaces(self):
        """Stress-test extended device namespaces (\\\\?\\, \\\\.\\, \\??\\, UNC)."""
        device_vectors = [
            r"\\?\UNC\server\share\file.txt",
            r"\\?\UNC\127.0.0.1\c$\Windows\System32",
            r"\\?\Volume{12345678-1234-1234-1234-123456789abc}\test",
            r"\\?\Volume{b1a2c3d4-0000-0000-0000-000000000000}\Windows",
            r"\\.\PhysicalDrive0",
            r"\\.\PhysicalDrive1",
            r"\\.\Harddisk0Partition1",
            r"\\.\NUL",
            r"\\.\CON",
            r"\\.\AUX",
            r"\\.\COM1",
            r"\\.\COM2",
            r"\\.\LPT1",
            r"\\?\GLOBALROOT\Device\Harddisk0\Partition1\boot.ini",
            r"\\?\GLOBALROOT\Device\HarddiskVolume1\Windows",
            r"\??\C:\Windows",
            r"\??\D:\file.txt",
            r"//?/C:/Windows",
            r"//./PhysicalDrive0",
            r"//?/UNC/server/share",
            r"\\localhost\c$\Windows",
            r"\\127.0.0.1\c$\Windows",
            r"\\?\c:\Windows\System32\cmd.exe",
            r"\\?\C:\Program Files",
            # Encoded forms
            "%5c%5c%3f%5cC%3a%5cWindows",
            "%5c%5c.%5cPhysicalDrive0",
            "%2f%2f%3f%2fC%3a%2fWindows",
            "%255c%255c%253f%255cC%253a%255cWindows",
            "%252f%252f%253f%252fC%253a%252fWindows",
            # Trailing dots/spaces on devices
            r"\\.\PhysicalDrive0 ",
            r"\\.\PhysicalDrive0.",
            r"\\.\NUL ",
        ]

        for vec in device_vectors:
            with self.subTest(vector=vec):
                res = hostdrop.safe_path(self.base_dir, vec)
                self.assertIsNone(res, f"Device namespace vector {vec!r} was not rejected! Returned: {res}")

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Null Byte Injections (Raw, Encoded, Multi-Encoded, In Base)
    # ──────────────────────────────────────────────────────────────────────────
    def test_null_byte_rejection_comprehensive(self):
        """Stress-test null bytes in all forms and nesting depths."""
        null_vectors = [
            # Raw null bytes
            "\0",
            "valid.txt\0",
            "\0valid.txt",
            "nested/\0/subfolder",
            "nested/subfolder/\0",
            "\0/../../etc/passwd",
            "valid.txt\0.png",
            "valid.txt\0.exe",
            # Single-encoded %00
            "%00",
            "%00valid.txt",
            "valid.txt%00",
            "valid.txt%00.png",
            "nested/%00/subfolder",
            "%00/../../etc/passwd",
            "%00/../../PROGRA~1",
            "nested/subfolder/../../%00",
            # Double-encoded %2500
            "%2500",
            "valid.txt%2500",
            "valid.txt%2500.exe",
            "%2500/../../etc/passwd",
            # Triple-encoded %252500
            "%252500",
            "valid.txt%252500",
            # Quadruple-encoded %25252500
            "%25252500",
            "valid.txt%25252500",
            # Quintuple-encoded %2525252500
            "%2525252500",
            # Sextuple-encoded %252525252500
            "%252525252500",
            # Mixed cases
            "%00.PNG",
            "%2500.PNG",
            "VALID.TXT%00",
            # Other control bytes
            "valid.txt\r\n",
            "valid.txt%0d%0a",
        ]

        for vec in null_vectors:
            with self.subTest(vector=vec):
                res = hostdrop.safe_path(self.base_dir, vec)
                # Any null-byte variant MUST return None or not resolve outside base
                if "\0" in vec or "%00" in vec.lower() or "%2500" in vec.lower() or "%252500" in vec.lower():
                    self.assertIsNone(res, f"Null byte vector {vec!r} was not rejected! Returned: {res}")
                elif res is not None:
                    real_base = os.path.realpath(os.path.abspath(self.base_dir))
                    self.assertEqual(os.path.commonpath([real_base, res]), real_base)

    def test_null_byte_in_base_dir(self):
        """Verify safe_path rejects poisoned base_dir with null bytes."""
        poisoned_bases = [
            f"{self.base_dir}\0",
            f"\0{self.base_dir}",
            f"{self.base_dir}\0/subdir",
        ]
        for p_base in poisoned_bases:
            with self.subTest(base=p_base):
                res = hostdrop.safe_path(p_base, "valid.txt")
                self.assertIsNone(res, f"Poisoned base {p_base!r} did not return None! Returned: {res}")

    # ──────────────────────────────────────────────────────────────────────────
    # 4. Alternate Data Streams (ADS)
    # ──────────────────────────────────────────────────────────────────────────
    def test_alternate_data_streams_rejection(self):
        """Stress-test Alternate Data Stream (ADS) syntax and multi-encoded colons."""
        ads_vectors = [
            "valid.txt::$DATA",
            "valid.txt:$DATA",
            "valid.txt:stream",
            "valid.txt:hidden",
            "valid.txt:streamname:$DATA",
            "valid.txt::$INDEX_ALLOCATION",
            "folder::$INDEX_ALLOCATION",
            "nested::$INDEX_ALLOCATION",
            "::$DATA",
            ":stream",
            ":$DATA",
            # Single-encoded colons (%3a)
            "valid.txt%3a%3a$DATA",
            "valid.txt%3astream",
            "valid.txt%3a%3astream",
            "folder%3a%3a$INDEX_ALLOCATION",
            "%3a%3a$DATA",
            # Double-encoded colons (%253a)
            "valid.txt%253a%253a$DATA",
            "valid.txt%253astream",
            "%253a%253a$DATA",
            # Triple-encoded colons (%25253a)
            "valid.txt%25253a%25253a$DATA",
            # Mixed slash and ADS
            "nested/subfolder/../../valid.txt::$DATA",
            "nested/subfolder/../../valid.txt:stream",
            "../valid.txt::$DATA",
            "..:stream",
        ]

        for vec in ads_vectors:
            with self.subTest(vector=vec):
                res = hostdrop.safe_path(self.base_dir, vec)
                self.assertIsNone(res, f"ADS vector {vec!r} was not rejected! Returned: {res}")

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Deep Traversal, Mixed Slashes & Edge Cases
    # ──────────────────────────────────────────────────────────────────────────
    def test_deep_and_mixed_traversals(self):
        """Stress-test deep recursion, multi-dot, mixed slashes, and reserved device names."""
        escape_must_be_none = [
            # Deep traversal attempts
            "../" * 50 + "Windows/System32/calc.exe",
            "..\\" * 50 + "Windows\\System32\\calc.exe",
            "a/b/c/" + "../../" * 20 + "etc/passwd",
            # Mixed slashes
            "../..\\../..\\../Windows/System32",
            "..//..\\\\..//..\\\\PROGRA~1",
            "../PROGRA~1",
            "..\\DOCUME~1",
            "sub/../../PROGRA~1",
            ".../../../PROGRA~1",
            # Multi-dot escapes
            ".. ",
            " ..",
            "..",
            # Windows reserved device names
            "CON",
            "PRN",
            "AUX",
            "NUL",
            "COM1",
            "COM9",
            "LPT1",
            "LPT9",
            "con.txt",
            "aux.json",
            "nul.pdf",
            "COM1.txt",
            "nested/CON",
            "nested/subfolder/PRN.txt",
            "con .txt",
            "aux .json",
            # Whitespace-only and None
            "   ",
            "\t\n",
            None,
        ]

        for vec in escape_must_be_none:
            with self.subTest(escape_vector=vec):
                res = hostdrop.safe_path(self.base_dir, vec)
                self.assertIsNone(res, f"Escape vector {vec!r} MUST return None, got: {res}")

        containment_vectors = [
            "",
            ".",
            " . ",
            "...",
            "....",
            ".....",
            ".../",
            "...\\",
            "sub/.../file.txt",
        ]

        for vec in containment_vectors:
            with self.subTest(containment_vector=vec):
                res = hostdrop.safe_path(self.base_dir, vec)
                if res is not None:
                    real_base = os.path.realpath(os.path.abspath(self.base_dir))
                    self.assertEqual(
                        os.path.commonpath([real_base, res]),
                        real_base,
                        f"Vector {vec!r} escaped base directory! Resolved to: {res}"
                    )

    def test_legitimate_paths_preserved(self):
        """Verify legitimate paths, subdirectories, and Unicode filenames remain accessible."""
        valid_paths = [
            "valid.txt",
            "nested/subfolder/data.json",
            "nested\\subfolder\\data.json",
            "nested",
            "nested/subfolder",
            ".",
        ]
        for vp in valid_paths:
            res = hostdrop.safe_path(self.base_dir, vp)
            self.assertIsNotNone(res, f"Legitimate path {vp!r} was falsely rejected!")
            real_base = os.path.realpath(os.path.abspath(self.base_dir))
            self.assertEqual(os.path.commonpath([real_base, res]), real_base)



class TestRateLimitingAndExponentialTarpitting(unittest.TestCase):
    """Adversarial stress-testing of rate limiting and exponential tarpitting on /api/auth."""

    def setUp(self):
        # Create an isolated SlidingWindowTarpitLimiter with small base delay for precise timing tests
        self.fast_limiter = auth.SlidingWindowTarpitLimiter(
            window_sec=900,
            max_failures=5,
            base_delay=0.002,   # 2ms base
            max_delay=0.032,    # 32ms cap
        )
        self.client_ip = f"198.51.100.{secrets.randbelow(200) + 10}"

    # ──────────────────────────────────────────────────────────────────────────
    # 1. Mathematical Progression of Exponential Delay
    # ──────────────────────────────────────────────────────────────────────────
    def test_exponential_delay_progression(self):
        """Verify exact mathematical progression: D(k) = min(base * 2^(k-1), max)."""
        expected_delays = [
            0.002 * (2 ** 0),  # 0.002s (attempt 1)
            0.002 * (2 ** 1),  # 0.004s (attempt 2)
            0.002 * (2 ** 2),  # 0.008s (attempt 3)
            0.002 * (2 ** 3),  # 0.016s (attempt 4)
            0.002 * (2 ** 4),  # 0.032s (attempt 5 - capped at max_delay)
        ]

        for idx, expected in enumerate(expected_delays, 1):
            delay, count = self.fast_limiter.record_failure(self.client_ip)
            self.assertEqual(count, idx, f"Attempt {idx} failure count mismatch")
            self.assertAlmostEqual(delay, expected, places=5, msg=f"Attempt {idx} delay mismatch")

    # ──────────────────────────────────────────────────────────────────────────
    # 2. Live Route Enforcement on /api/auth (GET & POST)
    # ──────────────────────────────────────────────────────────────────────────
    def test_live_route_api_auth_get_lockout_sequence(self):
        """Verify GET /api/auth: 5 failed attempts return 401, 6th attempt returns 429 with Retry-After."""
        old_limiter = auth.GLOBAL_RATE_LIMITER
        auth.GLOBAL_RATE_LIMITER = self.fast_limiter

        try:
            ip = f"203.0.113.{secrets.randbelow(200) + 10}"

            # Attempts 1 through 5: Must return HTTP 401 Unauthorized
            for attempt in range(1, 6):
                handler = MockHTTPHandler(
                    method="GET",
                    path=f"/api/auth?key=invalid_key_{attempt}",
                    client_ip=ip
                )
                parsed = urllib.parse.urlparse(handler.path)
                qs = urllib.parse.parse_qs(parsed.query)

                handled = auth.handle_auth_routes(handler, parsed.path, qs)
                self.assertTrue(handled)
                self.assertEqual(
                    handler.response_status, 401,
                    f"Attempt {attempt} must return HTTP 401, got {handler.response_status}"
                )
                body = handler.get_json()
                self.assertFalse(body.get("success", True))
                self.assertEqual(body.get("error"), "invalid_access_key")

            # Attempt 6: Must return HTTP 429 Too Many Requests (Lockout)
            handler_6 = MockHTTPHandler(
                method="GET",
                path="/api/auth?key=invalid_key_6",
                client_ip=ip
            )
            parsed_6 = urllib.parse.urlparse(handler_6.path)
            qs_6 = urllib.parse.parse_qs(parsed_6.query)

            handled_6 = auth.handle_auth_routes(handler_6, parsed_6.path, qs_6)
            self.assertTrue(handled_6)
            self.assertEqual(
                handler_6.response_status, 429,
                f"Attempt 6 must return HTTP 429 Lockout, got {handler_6.response_status}"
            )

            body_6 = handler_6.get_json()
            self.assertEqual(body_6.get("error"), "rate_limited")
            self.assertIn("retry_after", body_6)
            self.assertGreater(body_6["retry_after"], 0)

            # Check HTTP Retry-After header
            self.assertIn("Retry-After", handler_6.response_headers)
            self.assertEqual(int(handler_6.response_headers["Retry-After"]), int(body_6["retry_after"]))

            # Attempt 7 & 8: Must continue to return HTTP 429
            for follow_up in [7, 8]:
                h_follow = MockHTTPHandler(
                    method="GET",
                    path=f"/api/auth?key=invalid_key_{follow_up}",
                    client_ip=ip
                )
                p_f = urllib.parse.urlparse(h_follow.path)
                q_f = urllib.parse.parse_qs(p_f.query)
                auth.handle_auth_routes(h_follow, p_f.path, q_f)
                self.assertEqual(h_follow.response_status, 429)

        finally:
            auth.GLOBAL_RATE_LIMITER = old_limiter

    def test_live_route_api_auth_post_body_lockout_sequence(self):
        """Verify POST /api/auth with JSON body: 5 failures return 401, 6th returns 429."""
        old_limiter = auth.GLOBAL_RATE_LIMITER
        auth.GLOBAL_RATE_LIMITER = self.fast_limiter

        try:
            ip = f"203.0.113.{secrets.randbelow(200) + 10}"

            # 5 failed attempts via POST JSON body
            for attempt in range(1, 6):
                handler = MockHTTPHandler(
                    method="POST",
                    path="/api/auth",
                    client_ip=ip
                )
                body_data = {"key": f"bad_post_key_{attempt}"}
                handled = auth.handle_auth_routes(handler, "/api/auth", {}, body_data=body_data)
                self.assertTrue(handled)
                self.assertEqual(handler.response_status, 401)

            # 6th attempt must lockout
            handler_6 = MockHTTPHandler(
                method="POST",
                path="/api/auth",
                client_ip=ip
            )
            body_data_6 = {"key": "bad_post_key_6"}
            handled_6 = auth.handle_auth_routes(handler_6, "/api/auth", {}, body_data=body_data_6)
            self.assertTrue(handled_6)
            self.assertEqual(handler_6.response_status, 429)
            body_6 = handler_6.get_json()
            self.assertEqual(body_6.get("error"), "rate_limited")
            self.assertGreater(body_6.get("retry_after", 0), 0)

        finally:
            auth.GLOBAL_RATE_LIMITER = old_limiter

    # ──────────────────────────────────────────────────────────────────────────
    # 3. Counter Reset on Valid Authentication
    # ──────────────────────────────────────────────────────────────────────────
    def test_counter_reset_on_valid_authentication(self):
        """Verify that after 4 failed attempts, a successful login completely resets the counter."""
        old_limiter = auth.GLOBAL_RATE_LIMITER
        auth.GLOBAL_RATE_LIMITER = self.fast_limiter

        try:
            ip = f"203.0.113.{secrets.randbelow(200) + 10}"
            valid_key = auth.get_access_key()

            # Record 4 failures (one short of lockout)
            for i in range(4):
                h = MockHTTPHandler(method="GET", path=f"/api/auth?key=bad_{i}", client_ip=ip)
                parsed = urllib.parse.urlparse(h.path)
                auth.handle_auth_routes(h, parsed.path, urllib.parse.parse_qs(parsed.query))
                self.assertEqual(h.response_status, 401)

            # Check limiter state: allowed, delay > 0
            allowed, cur_delay = self.fast_limiter.check_rate_limit(ip)
            self.assertTrue(allowed)
            self.assertGreater(cur_delay, 0.0)

            # Now perform a valid login with real access key
            h_success = MockHTTPHandler(method="GET", path=f"/api/auth?key={valid_key}", client_ip=ip)
            parsed_s = urllib.parse.urlparse(h_success.path)
            auth.handle_auth_routes(h_success, parsed_s.path, urllib.parse.parse_qs(parsed_s.query))
            self.assertEqual(h_success.response_status, 303, "Valid login must return HTTP 303 PRG")

            # Limiter must be reset
            allowed_after, delay_after = self.fast_limiter.check_rate_limit(ip)
            self.assertTrue(allowed_after)
            self.assertEqual(delay_after, 0.0, "Delay must be reset to 0.0 after valid authentication")

            # Next 4 failed attempts should NOT lock out
            for i in range(4):
                h = MockHTTPHandler(method="GET", path=f"/api/auth?key=new_bad_{i}", client_ip=ip)
                parsed = urllib.parse.urlparse(h.path)
                auth.handle_auth_routes(h, parsed.path, urllib.parse.parse_qs(parsed.query))
                self.assertEqual(h.response_status, 401, "Must allow 4 attempts after reset")

        finally:
            auth.GLOBAL_RATE_LIMITER = old_limiter

    # ──────────────────────────────────────────────────────────────────────────
    # 4. IP Isolation Under Attack
    # ──────────────────────────────────────────────────────────────────────────
    def test_ip_isolation_attacker_does_not_block_victim(self):
        """Verify that locking out an attacker IP does not impair a distinct IP."""
        attacker_ip = "198.51.100.111"
        victim_ip = "198.51.100.222"

        # Attacker fails 5 times
        for _ in range(5):
            self.fast_limiter.record_failure(attacker_ip)

        # Attacker is locked out
        is_allowed_atk, _ = self.fast_limiter.check_rate_limit(attacker_ip)
        self.assertFalse(is_allowed_atk, "Attacker must be locked out")

        # Victim IP has 0 failures and must be allowed with 0 delay
        is_allowed_vic, delay_vic = self.fast_limiter.check_rate_limit(victim_ip)
        self.assertTrue(is_allowed_vic, "Victim IP must remain allowed")
        self.assertEqual(delay_vic, 0.0, "Victim IP delay must be 0.0")

    # ──────────────────────────────────────────────────────────────────────────
    # 5. Header Spoofing Resistance on Direct Connections
    # ──────────────────────────────────────────────────────────────────────────
    def test_header_spoofing_resistance_on_direct_connection(self):
        """Verify direct non-loopback connections cannot evade rate limiting by rotating proxy headers."""
        old_limiter = auth.GLOBAL_RATE_LIMITER
        auth.GLOBAL_RATE_LIMITER = self.fast_limiter

        try:
            # Physical client IP on LAN
            real_lan_ip = "192.168.1.150"

            # Attacker tries to rotate X-Forwarded-For / CF-Connecting-IP on each attempt
            for i in range(5):
                fake_ip = f"10.0.0.{i+1}"
                headers = {
                    "X-Forwarded-For": fake_ip,
                    "CF-Connecting-IP": fake_ip,
                    "X-Real-IP": fake_ip,
                    "Forwarded": f"for={fake_ip}",
                }
                h = MockHTTPHandler(
                    method="GET",
                    path=f"/api/auth?key=bad_{i}",
                    headers=headers,
                    client_ip=real_lan_ip
                )
                parsed = urllib.parse.urlparse(h.path)
                auth.handle_auth_routes(h, parsed.path, urllib.parse.parse_qs(parsed.query))
                self.assertEqual(h.response_status, 401)

            # 6th attempt with yet another fake IP
            headers_6 = {
                "X-Forwarded-For": "10.0.0.99",
                "CF-Connecting-IP": "10.0.0.99",
            }
            h6 = MockHTTPHandler(
                method="GET",
                path="/api/auth?key=bad_6",
                headers=headers_6,
                client_ip=real_lan_ip
            )
            parsed6 = urllib.parse.urlparse(h6.path)
            auth.handle_auth_routes(h6, parsed6.path, urllib.parse.parse_qs(parsed6.query))

            self.assertEqual(
                h6.response_status, 429,
                "Direct non-loopback connection MUST NOT evade rate limiting by spoofing proxy headers"
            )

        finally:
            auth.GLOBAL_RATE_LIMITER = old_limiter

    # ──────────────────────────────────────────────────────────────────────────
    # 6. High-Concurrency Stress Harness
    # ──────────────────────────────────────────────────────────────────────────
    def test_high_concurrency_race_condition_stress(self):
        """Simulate concurrent flood from same IP across 10 threads."""
        ip = f"203.0.113.{secrets.randbelow(200) + 10}"
        threads = []
        errors = []

        def flood_worker():
            try:
                for _ in range(5):
                    self.fast_limiter.record_failure(ip)
            except Exception as e:
                errors.append(e)

        for _ in range(10):
            t = threading.Thread(target=flood_worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent threads raised errors: {errors}")

        # Total failures recorded should be 50
        with self.fast_limiter.lock:
            total_recorded = len(self.fast_limiter.failure_history.get(ip, []))
        self.assertEqual(total_recorded, 50, f"Expected 50 failures recorded, got {total_recorded}")

        # IP must definitely be locked out
        is_allowed, remaining = self.fast_limiter.check_rate_limit(ip)
        self.assertFalse(is_allowed, "IP must be locked out after 50 concurrent failures")
        self.assertGreater(remaining, 0.0)


class TestLiveServerAdversarialHarness(unittest.TestCase):
    """End-to-end live HTTP server penetration tests on live TCP socket."""

    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.mkdtemp(prefix="ts_live_adv_challenger_")
        cls.upload_dir = os.path.join(cls.temp_dir, "uploads")
        cls.share_dir = os.path.join(cls.temp_dir, "shared")
        os.makedirs(cls.upload_dir, exist_ok=True)
        os.makedirs(cls.share_dir, exist_ok=True)

        # Create a valid test file inside upload_dir
        cls.valid_filename = "classified_doc.txt"
        cls.test_file = os.path.join(cls.upload_dir, cls.valid_filename)
        cls.expected_content = "CONFIDENTIAL HOSTDROP TEST DATA 42"
        with open(cls.test_file, "w", encoding="utf-8") as f:
            f.write(cls.expected_content)

        hostdrop.UPLOAD_DIR = cls.upload_dir
        hostdrop.HOST_SHARE = cls.share_dir
        hostdrop.REQUIRE_AUTH_ON_LAN = True

        # Bind ephemeral port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()

        cls.server = hostdrop.create_server("127.0.0.1", cls.port)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        hostdrop.REQUIRE_AUTH_ON_LAN = False
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.temp_dir, ignore_errors=True)

    def _get_auth_cookie(self):
        """Helper to obtain a valid session cookie."""
        access_key = auth.get_access_key()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_303(self, req, fp, code, msg, headers):
                return fp

        opener = urllib.request.build_opener(NoRedirect)
        login_url = f"{self.base_url}/api/auth?key={access_key}"
        resp = opener.open(login_url)
        cookie_header = resp.headers.get("Set-Cookie", "")
        token = cookie_header.split(";")[0]
        return token

    def test_live_download_path_traversal_rejection(self):
        """Verify live /download route rejects all traversal vectors and safely serves valid files."""
        auth_cookie = self._get_auth_cookie()

        # Adversarial traversal attempts
        traversal_attempts = [
            "../../PROGRA~1",
            "..%5c..%5cPROGRA~1",
            "..%2f..%2fPROGRA~1",
            "../PROGRA~1",
            "..\\DOCUME~1",
            "\\\\?\\C:\\Windows\\win.ini",
            "%5c%5c%3f%5cC%3a%5cWindows",
            f"{self.valid_filename}::$DATA",
            f"{self.valid_filename}:stream",
            f"{self.valid_filename}%00.png",
            f"{self.valid_filename}%2500",
            r"\\.\PhysicalDrive0",
            "/etc/passwd",
            "C:\\Windows\\System32\\calc.exe",
        ]

        for trav in traversal_attempts:
            with self.subTest(traversal=trav):
                encoded_path = urllib.parse.quote(trav)
                url = f"{self.base_url}/download?tab=recv&path={encoded_path}"
                req = urllib.request.Request(url, headers={"Cookie": auth_cookie})
                try:
                    resp = urllib.request.urlopen(req)
                    self.fail(f"Traversal URL {url} unexpectedly succeeded with status {resp.status}!")
                except urllib.error.HTTPError as e:
                    # Must be 404 Not Found (or 400 Bad Request) — never 200 OK
                    self.assertIn(
                        e.code, [404, 400],
                        f"Expected 404/400 for traversal {trav!r}, got HTTP {e.code}"
                    )

        # Confirm legitimate file download DOES succeed with exact content
        valid_url = f"{self.base_url}/download?tab=recv&path={self.valid_filename}"
        valid_req = urllib.request.Request(valid_url, headers={"Cookie": auth_cookie})
        valid_resp = urllib.request.urlopen(valid_req)
        self.assertEqual(valid_resp.status, 200)
        content = valid_resp.read().decode("utf-8")
        self.assertEqual(content, self.expected_content)

    def test_live_check_api_path_traversal_rejection(self):
        """Verify live /api/check route returns exists: false for all traversal vectors."""
        auth_cookie = self._get_auth_cookie()

        traversal_attempts = [
            "../../PROGRA~1",
            "..\\..\\DOCUME~1",
            f"{self.valid_filename}::$DATA",
            f"{self.valid_filename}:stream",
            "\\\\?\\UNC\\127.0.0.1\\c$\\Windows",
            "CON.txt",
        ]

        for trav in traversal_attempts:
            with self.subTest(traversal=trav):
                encoded = urllib.parse.quote(trav)
                url = f"{self.base_url}/api/check?path={encoded}"
                req = urllib.request.Request(url, headers={"Cookie": auth_cookie})
                resp = urllib.request.urlopen(req)
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertFalse(
                    data.get("exists", True),
                    f"Traversal path {trav!r} falsely reported exists=True!"
                )

        # Confirm legitimate file check returns exists: true with correct size
        valid_url = f"{self.base_url}/api/check?path={self.valid_filename}"
        valid_req = urllib.request.Request(valid_url, headers={"Cookie": auth_cookie})
        valid_resp = urllib.request.urlopen(valid_req)
        self.assertEqual(valid_resp.status, 200)
        valid_data = json.loads(valid_resp.read().decode("utf-8"))
        self.assertTrue(valid_data.get("exists"))
        self.assertEqual(valid_data.get("size"), len(self.expected_content))


if __name__ == "__main__":
    unittest.main(verbosity=2)

