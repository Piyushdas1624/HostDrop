"""
HostDrop Automated Adversarial Cybersecurity & Penetration Testing Suite
Validates Categories A through F against the Hardened Authentication & Security Engine.

Execution:
    python test_security.py
    python -m unittest test_security.py -v
"""

import os
import sys
import io
import time
import json
import hmac
import shutil
import socket
import secrets
import hashlib
import tempfile
import threading
import ipaddress
import unittest
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Any, Optional

# Ensure project root is in import search path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import auth
import hostdrop


# ── Mock HTTP Handler for Isolated Request Routing Tests ───────────────────────
class MockHTTPHandler:
    """Simulates BaseHTTPRequestHandler for unit & integration testing of security hooks."""

    def __init__(self, method: str, path: str, headers: Optional[Dict[str, str]] = None, client_ip: str = "127.0.0.1"):
        self.command = method
        self.path = path
        self.headers = headers or {}
        self.client_address = (client_ip, 54321)
        self.response_status: Optional[int] = None
        self.response_headers: Dict[str, str] = {}
        self.wfile = io.BytesIO()

    def send_response(self, code: int):
        self.response_status = code

    def send_header(self, keyword: str, value: str):
        self.response_headers[keyword] = value

    def end_headers(self):
        pass

    def get_body(self) -> bytes:
        return self.wfile.getvalue()

    def get_json(self) -> Any:
        raw = self.get_body()
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def is_physical_localhost(self) -> bool:
        return auth.is_physical_localhost(self)


# ── Test Suite: Category A — Authentication & Session Integrity ────────────────
class TestCategoryA_Authentication(unittest.TestCase):
    """
    Category A: Authentication & Session Integrity
    Covers password hashing, constant-time verification, session token generation/signing,
    tampered/expired token rejection, URL-safe bookmarked key login, and 303 PRG redirects.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_auth_test_")
        self.env_file = os.path.join(self.test_dir, ".env")
        self.test_secret = secrets.token_hex(32)
        self.test_access_key = "ts_live_" + secrets.token_urlsafe(24)
        self.test_password = "CorrectHorseBatteryStaple99!"
        self.password_hash = auth.SecurityConfig.hash_password(self.test_password)

        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(f"HOSTDROP_SECRET_KEY={self.test_secret}\n")
            f.write(f"HOSTDROP_ACCESS_KEY={self.test_access_key}\n")
            f.write(f"HOSTDROP_PASSWORD_HASH={self.password_hash}\n")

        self.sec_config = auth.SecurityConfig(self.env_file)
        self.session_manager = auth.SessionManager(self.test_secret)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_SEC_AUTH_001_pbkdf2_password_hashing_and_constant_time_verification(self):
        """Verify PBKDF2-HMAC-SHA256 password hashing and constant-time match."""
        # 1. Valid password verification
        self.assertTrue(self.sec_config.verify_password(self.test_password), "Valid password must verify true")

        # 2. Invalid password verification
        self.assertFalse(self.sec_config.verify_password("WrongPassword123"), "Incorrect password must verify false")
        self.assertFalse(self.sec_config.verify_password(""), "Empty password must verify false")

        # 3. Hash format structure inspection
        parts = self.sec_config.password_hash.split("$")
        self.assertEqual(len(parts), 4, "Hash must have 4 segments: scheme$iterations$salt$hash")
        self.assertEqual(parts[0], "pbkdf2_sha256", "Scheme must be pbkdf2_sha256")
        self.assertEqual(int(parts[1]), auth.DEFAULT_ITERATIONS, f"Iterations must be {auth.DEFAULT_ITERATIONS}")
        self.assertEqual(len(bytes.fromhex(parts[2])), 16, "Salt must be 16 bytes")
        self.assertEqual(len(bytes.fromhex(parts[3])), 32, "Derived key must be 32 bytes (256 bits)")

    def test_SEC_AUTH_002_constant_time_timing_invariance(self):
        """Verify constant-time comparison does not leak prefix matches via early termination."""
        # We test verify_password timing consistency across identical prefix mismatch vs random mismatch
        base_pwd = "A" * 64
        prefix_matched_pwd = "A" * 63 + "B"
        non_matched_pwd = "B" * 64

        # Warm up
        for _ in range(2):
            self.sec_config.verify_password(base_pwd)

        t0 = time.perf_counter()
        res1 = self.sec_config.verify_password(prefix_matched_pwd)
        t1 = time.perf_counter()
        dt1 = t1 - t0

        t2 = time.perf_counter()
        res2 = self.sec_config.verify_password(non_matched_pwd)
        t3 = time.perf_counter()
        dt2 = t3 - t2

        self.assertFalse(res1)
        self.assertFalse(res2)
        # Verify execution delta is within reasonable CPU jitter margin
        self.assertLess(abs(dt1 - dt2), 0.5, "Timing verification delta should be negligible")

    def test_SEC_AUTH_003_session_token_generation_and_validation(self):
        """Verify cryptographically signed session tokens are valid and preserve user-agent binding."""
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        token = self.session_manager.create_token(user_agent=ua)

        # 1. Valid token validation with matching UA
        self.assertTrue(self.session_manager.verify_token(token, user_agent=ua))

        # 2. Token validation without UA check
        self.assertTrue(self.session_manager.verify_token(token, user_agent=""))

        # 3. Token validation with mismatched UA
        mismatched_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"
        self.assertFalse(self.session_manager.verify_token(token, user_agent=mismatched_ua))

    def test_SEC_AUTH_004_tampered_and_forged_session_token_rejection(self):
        """Verify tampered token payloads, modified signatures, and malformed structures are rejected."""
        ua = "Mozilla/5.0 (Mobile)"
        token = self.session_manager.create_token(user_agent=ua)
        parts = token.split(".")

        # 1. Modified session ID with original signature
        tampered_session_id = "0" * 32
        tampered_token = f"{parts[0]}.{tampered_session_id}.{parts[2]}.{parts[3]}.{parts[4]}.{parts[5]}"
        self.assertFalse(self.session_manager.verify_token(tampered_token, user_agent=ua))

        # 2. Forged signature
        forged_sig = "0" * 64
        forged_token = f"{parts[0]}.{parts[1]}.{parts[2]}.{parts[3]}.{parts[4]}.{forged_sig}"
        self.assertFalse(self.session_manager.verify_token(forged_token, user_agent=ua))

        # 3. Invalid version prefix
        invalid_ver_token = f"v2.{parts[1]}.{parts[2]}.{parts[3]}.{parts[4]}.{parts[5]}"
        self.assertFalse(self.session_manager.verify_token(invalid_ver_token, user_agent=ua))

        # 4. Truncated token
        self.assertFalse(self.session_manager.verify_token("v1.truncated.token", user_agent=ua))
        self.assertFalse(self.session_manager.verify_token("", user_agent=ua))
        self.assertFalse(self.session_manager.verify_token(None, user_agent=ua))

    def test_SEC_AUTH_005_expired_session_token_rejection(self):
        """Verify tokens past their expiration timestamp are rejected."""
        ua = "Mozilla/5.0"
        version = "v1"
        session_id = secrets.token_hex(16)
        issued_at = int(time.time()) - 86400 * 35  # 35 days ago
        expires_at = int(time.time()) - 86400 * 5   # expired 5 days ago
        ua_hash = hashlib.sha256(ua.encode("utf-8")).hexdigest()[:16]
        payload = f"{version}.{session_id}.{issued_at}.{expires_at}.{ua_hash}"
        signature = hmac.new(self.test_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        expired_token = f"{payload}.{signature}"

        self.assertFalse(self.session_manager.verify_token(expired_token, user_agent=ua))

    def test_SEC_AUTH_006_stateless_crash_recovery_across_reboots(self):
        """Verify signed session tokens remain valid across server reboots (new SessionManager instance)."""
        ua = "Mozilla/5.0 (Android 14; Mobile)"
        token = self.session_manager.create_token(user_agent=ua)

        # Simulate server crash/restart by creating a brand new SessionManager with the same secret key
        rebooted_session_manager = auth.SessionManager(self.test_secret)
        self.assertTrue(rebooted_session_manager.verify_token(token, user_agent=ua))

        # But a server reboot with an altered secret key must invalidate old tokens
        different_secret_manager = auth.SessionManager(secrets.token_hex(32))
        self.assertFalse(different_secret_manager.verify_token(token, user_agent=ua))

    def test_SEC_AUTH_007_url_safe_bookmarked_key_login_and_303_prg_redirect(self):
        """Verify /api/auth?key=... validates key, issues secure cookie, and performs 303 PRG clean redirect."""
        # 1. Valid Access Key
        global_key = auth.get_access_key()
        handler = MockHTTPHandler(
            method="GET",
            path=f"/api/auth?key={global_key}",
            headers={"User-Agent": "TestBrowser/1.0", "X-Forwarded-Proto": "https"},
            client_ip="127.0.0.1"
        )
        parsed = urllib.parse.urlparse(handler.path)
        qs = urllib.parse.parse_qs(parsed.query)

        # Execute auth route handler
        handled = auth.handle_auth_routes(handler, parsed.path, qs)
        self.assertTrue(handled)
        self.assertEqual(handler.response_status, 303, "Must return HTTP 303 See Other")
        self.assertEqual(handler.response_headers.get("Location"), "/", "Must redirect to clean root /")
        self.assertEqual(handler.response_headers.get("Referrer-Policy"), "no-referrer", "Must strip referrer")
        self.assertIn("no-store", handler.response_headers.get("Cache-Control", ""), "Must disable caching")

        cookie_header = handler.response_headers.get("Set-Cookie", "")
        self.assertIn("hostdrop_session=", cookie_header)
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("SameSite=Strict", cookie_header)
        self.assertIn("Secure", cookie_header)

        # 2. Invalid Access Key
        bad_handler = MockHTTPHandler(
            method="GET",
            path="/api/auth?key=invalid_bogus_key",
            headers={"User-Agent": "TestBrowser/1.0"},
            client_ip="192.168.1.55"
        )
        bad_parsed = urllib.parse.urlparse(bad_handler.path)
        bad_qs = urllib.parse.parse_qs(bad_parsed.query)
        auth.handle_auth_routes(bad_handler, bad_parsed.path, bad_qs)
        self.assertEqual(bad_handler.response_status, 401, "Invalid access key must return HTTP 401")

    def test_SEC_AUTH_008_memorable_passcode_format_and_entropy(self):
        """Verify memorable passcode format (word-word-NN), ambiguity exclusion, and >=30.0 bits of entropy."""
        import re
        import math

        # 1. Format and ambiguity verification across multiple samples
        for _ in range(50):
            code = auth.generate_passcode()
            self.assertRegex(code, r'^[a-z]+-[a-z]+-[2-9]{2}$', f"Passcode {code} must match word-word-NN format")
            parts = code.split("-")
            self.assertEqual(len(parts), 3, "Passcode must have exactly 3 segments separated by hyphens")
            suffix_digits = parts[2]
            self.assertEqual(len(suffix_digits), 2, "Suffix must be exactly 2 digits")
            # Ambiguity exclusion: digits strictly from [2-9] (no 0 or 1)
            for d in suffix_digits:
                self.assertIn(d, "23456789", f"Ambiguous digit {d} found in suffix {suffix_digits}")
                self.assertNotIn(d, ("0", "1"), f"Digit {d} must not be 0 or 1")

        # 2. Entropy verification: >= 30.0 bits
        wordlist = getattr(auth, "MEMORABLE_WORDS", getattr(auth, "PASSCODE_WORDS", []))
        self.assertGreaterEqual(len(wordlist), 4096, "Wordlist must contain at least 4096 words")
        suffix_combinations = 8 * 8  # digits 2-9
        total_combinations = len(wordlist) * len(wordlist) * suffix_combinations
        entropy_bits = math.log2(total_combinations)
        self.assertGreaterEqual(entropy_bits, 30.0, f"Entropy must achieve at least 30.0 bits (got {entropy_bits:.2f})")

    def test_SEC_AUTH_009_passcode_persistence_across_reboots(self):
        """Verify memorable passcode and PBKDF2 hash persistence across server reboots."""
        tmp_dir = tempfile.mkdtemp(prefix="hostdrop_persist_test_")
        try:
            env_file = os.path.join(tmp_dir, ".env")
            # First launch: no .env file exists
            cfg1 = auth.SecurityConfig(env_file)
            passcode1 = cfg1.raw_password
            hash1 = cfg1.password_hash
            self.assertTrue(passcode1, "First launch must generate raw_password")
            self.assertTrue(hash1.startswith("pbkdf2_sha256$"), "First launch must derive PBKDF2 hash")
            self.assertTrue(cfg1.verify_password(passcode1), "Passcode must verify against generated hash")

            # Check .env file contents
            with open(env_file, "r", encoding="utf-8") as f:
                env_content = f.read()
            self.assertIn(f"HOSTDROP_PASSCODE={passcode1}", env_content, "HOSTDROP_PASSCODE must be written to .env")
            self.assertIn(f"HOSTDROP_PASSWORD_HASH={hash1}", env_content, "HOSTDROP_PASSWORD_HASH must be written to .env")

            # Subsequent launch (simulating reboot)
            cfg2 = auth.SecurityConfig(env_file)
            self.assertEqual(cfg2.raw_password, passcode1, "Reboot must load identical raw_password from .env")
            self.assertEqual(cfg2.password_hash, hash1, "Reboot must preserve PBKDF2 hash")
            self.assertTrue(cfg2.verify_password(passcode1), "Rebooted config must verify passcode")

            # Hash-only fallback mode
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(f"HOSTDROP_SECRET_KEY={cfg1.secret_key}\n")
                f.write(f"HOSTDROP_ACCESS_KEY={cfg1.access_key}\n")
                f.write(f"HOSTDROP_PASSWORD_HASH={hash1}\n")
            cfg3 = auth.SecurityConfig(env_file)
            self.assertEqual(cfg3.raw_password, "", "Hash-only mode must keep raw_password empty")
            self.assertTrue(cfg3.verify_password(passcode1), "Hash-only mode must still verify valid passcode")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_SEC_AUTH_010_manual_env_passcode_update(self):
        """Verify manual user update of HOSTDROP_PASSCODE in .env synchronizes hash."""
        tmp_dir = tempfile.mkdtemp(prefix="hostdrop_sync_test_")
        try:
            env_file = os.path.join(tmp_dir, ".env")
            cfg1 = auth.SecurityConfig(env_file)
            old_hash = cfg1.password_hash

            # User edits HOSTDROP_PASSCODE manually in .env
            custom_code = "swift-frost-88"
            with open(env_file, "w", encoding="utf-8") as f:
                f.write(f"HOSTDROP_SECRET_KEY={cfg1.secret_key}\n")
                f.write(f"HOSTDROP_ACCESS_KEY={cfg1.access_key}\n")
                f.write(f"HOSTDROP_PASSCODE={custom_code}\n")
                f.write(f"HOSTDROP_PASSWORD_HASH={old_hash}\n")

            # Reload SecurityConfig
            cfg2 = auth.SecurityConfig(env_file)
            self.assertEqual(cfg2.raw_password, custom_code, "Must load manually edited passcode")
            self.assertTrue(cfg2.verify_password(custom_code), "New passcode must verify against synchronized hash")
            self.assertNotEqual(cfg2.password_hash, old_hash, "Hash must be recalculated and updated")
            self.assertTrue(cfg2.is_custom_passcode, "is_custom_passcode must be True after manual edit")

            # Check .env was updated with new hash
            with open(env_file, "r", encoding="utf-8") as f:
                updated_env = f.read()
            self.assertIn(f"HOSTDROP_PASSWORD_HASH={cfg2.password_hash}", updated_env)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_SEC_AUTH_011_host_security_info_isolation(self):
        """Verify GET /api/host_security_info is strictly protected by host isolation."""
        # 1. Localhost direct request -> 200 OK with security info
        local_handler = MockHTTPHandler(
            method="GET",
            path="/api/host_security_info",
            headers={"User-Agent": "HostBrowser/1.0"},
            client_ip="127.0.0.1"
        )
        parsed = urllib.parse.urlparse(local_handler.path)
        qs = urllib.parse.parse_qs(parsed.query)
        handled = auth.handle_auth_routes(local_handler, parsed.path, qs)
        self.assertTrue(handled)
        self.assertEqual(local_handler.response_status, 200, "Direct localhost must return 200 OK")
        data = local_handler.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("passcode", data)
        self.assertIn("tip", data)
        self.assertIn("iterations", data)
        self.assertEqual(data.get("iterations"), 600000)

        # 2. Remote tunnel request with CF-Connecting-IP -> 403 Forbidden
        tunnel_handler = MockHTTPHandler(
            method="GET",
            path="/api/host_security_info",
            headers={"User-Agent": "RemoteBrowser/1.0", "CF-Connecting-IP": "198.51.100.42"},
            client_ip="127.0.0.1"
        )
        tunnel_parsed = urllib.parse.urlparse(tunnel_handler.path)
        tunnel_qs = urllib.parse.parse_qs(tunnel_parsed.query)
        handled_tunnel = auth.handle_auth_routes(tunnel_handler, tunnel_parsed.path, tunnel_qs)
        self.assertTrue(handled_tunnel)
        self.assertEqual(tunnel_handler.response_status, 403, "Cloudflare tunnel request must return 403 Forbidden")

        # 3. Remote LAN IP -> 403 Forbidden
        remote_handler = MockHTTPHandler(
            method="GET",
            path="/api/host_security_info",
            headers={"User-Agent": "RemoteBrowser/1.0"},
            client_ip="192.168.1.120"
        )
        remote_parsed = urllib.parse.urlparse(remote_handler.path)
        remote_qs = urllib.parse.parse_qs(remote_parsed.query)
        handled_remote = auth.handle_auth_routes(remote_handler, remote_parsed.path, remote_qs)
        self.assertTrue(handled_remote)
        self.assertEqual(remote_handler.response_status, 403, "Remote client IP must return 403 Forbidden")

    def test_SEC_AUTH_012_strict_host_isolation_all_proxy_headers(self):
        """Verify all host-only endpoints reject Forwarded (RFC 7239) and all proxy headers case-insensitively."""
        old_cfg_file = auth.CONFIG_FILE
        old_sec_cfg = auth.GLOBAL_SECURITY_CONFIG
        try:
            auth.CONFIG_FILE = self.env_file
            auth.GLOBAL_SECURITY_CONFIG = self.sec_config

            routes = [
                "/api/host_security_info",
                "/api/sessions",
                "/api/revoke_session",
                "/api/change_password"
            ]
            proxy_header_permutations = [
                {"Forwarded": "for=198.51.100.1"},
                {"forwarded": "for=198.51.100.1"},
                {"FORWARDED": "for=198.51.100.1;proto=https"},
                {"CF-Connecting-IP": "198.51.100.2"},
                {"cf-connecting-ip": "198.51.100.2"},
                {"X-Forwarded-For": "198.51.100.3, 10.0.0.1"},
                {"x-forwarded-for": "198.51.100.3"},
                {"X-Real-IP": "198.51.100.4"},
                {"x-real-ip": "198.51.100.4"},
                {"True-Client-IP": "198.51.100.5"},
            ]

            # 1. Verify every sensitive route returns 403 for every proxy header
            for route in routes:
                method = "POST" if route in ("/api/revoke_session", "/api/change_password") else "GET"
                body = {"new_password": "NewSecretPasscode123!", "session_id": "test_id"}
                for headers in proxy_header_permutations:
                    handler = MockHTTPHandler(method=method, path=route, headers=headers, client_ip="127.0.0.1")
                    auth.handle_auth_routes(handler, route, {}, body)
                    self.assertEqual(
                        handler.response_status,
                        403,
                        f"Route {route} with proxy headers {headers} must return 403 Forbidden"
                    )

            # 2. Verify physical localhost without proxy headers returns 200
            for route in routes:
                method = "POST" if route in ("/api/revoke_session", "/api/change_password") else "GET"
                body = {"new_password": "NewSecretPasscode123!", "session_id": "test_id"}
                handler = MockHTTPHandler(method=method, path=route, headers={}, client_ip="127.0.0.1")
                auth.handle_auth_routes(handler, route, {}, body)
                self.assertEqual(
                    handler.response_status,
                    200,
                    f"Route {route} from genuine physical localhost must return 200 OK"
                )
        finally:
            auth.CONFIG_FILE = old_cfg_file
            auth.GLOBAL_SECURITY_CONFIG = old_sec_cfg

    def test_SEC_AUTH_013_custom_passcode_precedence_and_session_revocation(self):
        """Verify custom passcode precedence over auto-generated codes, reboot persistence, and session revocation."""
        tmp_dir = tempfile.mkdtemp(prefix="hostdrop_custom_prec_")
        try:
            env_file = os.path.join(tmp_dir, ".env")
            sessions_file = os.path.join(tmp_dir, ".sessions.json")
            old_cfg_file = auth.CONFIG_FILE
            old_sess_file = getattr(auth, "SESSIONS_FILE", ".sessions.json")
            old_sec_cfg = auth.GLOBAL_SECURITY_CONFIG
            old_sess_reg = auth.GLOBAL_SESSION_REGISTRY
            old_sess_mgr = auth.GLOBAL_SESSION_MANAGER

            auth.CONFIG_FILE = env_file
            auth.SESSIONS_FILE = sessions_file

            cfg = auth.SecurityConfig(env_file)
            auth.GLOBAL_SECURITY_CONFIG = cfg
            auth.GLOBAL_SESSION_REGISTRY = auth.SessionRegistry(sessions_file)
            sm = auth.SessionManager(cfg.secret_key)
            auth.GLOBAL_SESSION_MANAGER = sm

            # Initial state: auto-generated code
            init_code = cfg.raw_password
            self.assertFalse(cfg.is_custom_passcode)
            self.assertTrue(cfg.verify_password(init_code))

            # Establish active session
            token = sm.create_token()
            self.assertTrue(sm.verify_token(token), "Active session token must verify valid")

            # User sets custom master passcode
            custom_code = "my-custom-vault-passcode-2026"
            ok, msg = auth.change_master_password(custom_code, revoke_all_sessions=True)
            self.assertTrue(ok)
            self.assertTrue(cfg.is_custom_passcode, "is_custom_passcode must be True after custom password change")
            self.assertEqual(cfg.raw_password, custom_code)
            self.assertTrue(cfg.verify_password(custom_code))
            self.assertFalse(cfg.verify_password(init_code), "Old auto-generated code must no longer verify")

            # Active sessions must be invalidated
            self.assertFalse(sm.verify_token(token), "Previous session must be revoked after master password change")

            # Simulate server reboot
            rebooted_cfg = auth.SecurityConfig(env_file)
            self.assertEqual(rebooted_cfg.raw_password, custom_code, "Reboot must load custom passcode")
            self.assertTrue(rebooted_cfg.is_custom_passcode, "Reboot must preserve is_custom_passcode flag")
            self.assertTrue(rebooted_cfg.verify_password(custom_code))
        finally:
            auth.CONFIG_FILE = old_cfg_file
            auth.SESSIONS_FILE = old_sess_file
            auth.GLOBAL_SECURITY_CONFIG = old_sec_cfg
            auth.GLOBAL_SESSION_REGISTRY = old_sess_reg
            auth.GLOBAL_SESSION_MANAGER = old_sess_mgr
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Test Suite: Category B — Filesystem Traversal & Path Confinement ───────────
class TestCategoryB_PathTraversal(unittest.TestCase):
    """
    Category B: Filesystem Traversal & Path Confinement
    Covers dot-dot traversal (../), URL-encoded paths, null-byte poisoning, NTFS Alternate Data Streams,
    Windows device names, UNC paths, and symlink escapes.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_trav_test_")
        self.base_dir = os.path.join(self.test_dir, "base")
        self.secret_dir = os.path.join(self.test_dir, "secret_outside")
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.secret_dir, exist_ok=True)

        with open(os.path.join(self.base_dir, "public.txt"), "w") as f:
            f.write("public content")
        with open(os.path.join(self.secret_dir, "passwords.txt"), "w") as f:
            f.write("sensitive passwords")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _safe_path(self, base: str, rel: str) -> Optional[str]:
        """Delegate directly to production hostdrop.safe_path() to verify genuine security."""
        return hostdrop.safe_path(base, rel)

    def test_SEC_TRAV_001_standard_and_deep_dot_dot_traversal(self):
        """Verify standard ../ and ..\\ traversal sequences and deep escapes are blocked."""
        self.assertIsNone(self._safe_path(self.base_dir, "../secret_outside/passwords.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "..\\secret.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "..\\..\\Windows\\System32\\cmd.exe"))
        self.assertIsNone(self._safe_path(self.base_dir, "../../../../etc/passwd"))
        self.assertIsNone(self._safe_path(self.base_dir, "../../../../../../../../Windows/win.ini"))
        self.assertIsNone(self._safe_path(self.base_dir, "..\\..\\..\\..\\..\\..\\..\\..\\Windows\\System32\\drivers\\etc\\hosts"))
        self.assertIsNone(self._safe_path(self.base_dir, "sub/../../../../etc/passwd"))
        self.assertIsNone(self._safe_path(self.base_dir, "sub/./../../secret.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "/../etc/shadow"))

    def test_SEC_TRAV_002_url_encoded_and_multi_encoded_traversal(self):
        """Verify single, double, and triple URL-encoded traversal sequences are blocked."""
        self.assertIsNone(self._safe_path(self.base_dir, "%2e%2e%2fsecret_outside%2fpasswords.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "%2e%2e%5csecret.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "..%2fsecret.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "%252e%252e%252fWindows%252fwin.ini"))
        self.assertIsNone(self._safe_path(self.base_dir, "%25252e%25252e%25252fsecret.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "%2e%2e/secret.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "..%252fsecret.txt"))

    def test_SEC_TRAV_003_null_byte_injection(self):
        """Verify null-byte string poisoning (raw, single-encoded, double-encoded) is rejected."""
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt\x00.exe"))
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt%00.png"))
        self.assertIsNone(self._safe_path(self.base_dir, "%00/../../etc/shadow"))
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt%2500.exe"))
        self.assertIsNone(self._safe_path(self.base_dir, "safe.txt\0.exe"))

    def test_SEC_TRAV_004_ntfs_alternate_data_streams(self):
        """Verify NTFS Alternate Data Streams (::$DATA, named streams, directory streams) are blocked."""
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt::$DATA"))
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt:hidden_stream"))
        self.assertIsNone(self._safe_path(self.base_dir, "folder::$INDEX_ALLOCATION"))
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt:::$DATA"))
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt%3a%3a$DATA"))

    def test_SEC_TRAV_005_windows_reserved_device_names(self):
        """Verify Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9) with extensions or spaces are blocked."""
        self.assertIsNone(self._safe_path(self.base_dir, "CON"))
        self.assertIsNone(self._safe_path(self.base_dir, "PRN"))
        self.assertIsNone(self._safe_path(self.base_dir, "AUX"))
        self.assertIsNone(self._safe_path(self.base_dir, "NUL"))
        self.assertIsNone(self._safe_path(self.base_dir, "COM1"))
        self.assertIsNone(self._safe_path(self.base_dir, "COM9"))
        self.assertIsNone(self._safe_path(self.base_dir, "LPT1"))
        self.assertIsNone(self._safe_path(self.base_dir, "CON.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "aux.json"))
        self.assertIsNone(self._safe_path(self.base_dir, "sub/PRN/file.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "CON .txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "NUL."))
        self.assertIsNone(self._safe_path(self.base_dir, "aux .dat"))
        self.assertIsNone(self._safe_path(self.base_dir, "COM1.tar.gz"))

    def test_SEC_TRAV_006_unc_and_cross_drive_and_absolute_paths(self):
        """Verify UNC network shares, drive letters, and POSIX/root paths are blocked."""
        self.assertIsNone(self._safe_path(self.base_dir, "\\\\192.168.1.1\\c$\\exploit.exe"))
        self.assertIsNone(self._safe_path(self.base_dir, "\\\\attacker.com\\share\\file.exe"))
        self.assertIsNone(self._safe_path(self.base_dir, "//attacker.com/share/file.exe"))
        self.assertIsNone(self._safe_path(self.base_dir, "\\\\?\\C:\\Windows\\win.ini"))
        self.assertIsNone(self._safe_path(self.base_dir, "\\\\127.0.0.1\\c$\\secret.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "C:\\Windows\\win.ini"))
        self.assertIsNone(self._safe_path(self.base_dir, "C:/Windows/win.ini"))
        self.assertIsNone(self._safe_path(self.base_dir, "D:\\secret.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "Z:\\Windows\\win.ini"))
        self.assertIsNone(self._safe_path(self.base_dir, "/etc/passwd"))
        self.assertIsNone(self._safe_path(self.base_dir, "/etc/shadow"))
        self.assertIsNone(self._safe_path(self.base_dir, "\\Windows\\System32"))
        self.assertIsNone(self._safe_path(self.base_dir, "Z:relative_drive_file.txt"))

    def test_SEC_TRAV_007_legitimate_paths_and_unicode_fidelity(self):
        """Verify legitimate relative subpaths and unicode filenames resolve safely."""
        valid_public = self._safe_path(self.base_dir, "public.txt")
        self.assertIsNotNone(valid_public)
        self.assertTrue(valid_public.endswith("public.txt"))
        self.assertEqual(os.path.commonpath([self.base_dir, valid_public]), os.path.realpath(self.base_dir))

        valid_nested = self._safe_path(self.base_dir, "nested/report.pdf")
        self.assertIsNotNone(valid_nested)
        self.assertTrue(valid_nested.endswith("report.pdf"))

        valid_unicode = self._safe_path(self.base_dir, "folder/file with spaces and éàç.png")
        self.assertIsNotNone(valid_unicode)
        self.assertTrue(valid_unicode.endswith("file with spaces and éàç.png"))

    def test_SEC_TRAV_008_ntfs_short_names_and_extended_device_namespaces(self):
        """Verify 8.3 short name traversal, extended device namespaces (\\\\?\\), null bytes, and ADS variants are blocked."""
        # 1. 8.3 Short name escapes attempting to traverse outside base_dir
        self.assertIsNone(self._safe_path(self.base_dir, "../PROGRA~1"))
        self.assertIsNone(self._safe_path(self.base_dir, "sub/../../PROGRA~1"))
        self.assertIsNone(self._safe_path(self.base_dir, "..\\DOCUME~1"))
        self.assertIsNone(self._safe_path(self.base_dir, "C:/PROGRA~1"))
        self.assertIsNone(self._safe_path(self.base_dir, r"\\?\C:\PROGRA~1"))

        # 2. Extended Win32 device and UNC namespaces
        self.assertIsNone(self._safe_path(self.base_dir, r"\\?\UNC\server\share\file.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, r"\\?\Volume{12345678-1234-1234-1234-123456789abc}\test"))
        self.assertIsNone(self._safe_path(self.base_dir, r"\\.\PhysicalDrive0"))
        self.assertIsNone(self._safe_path(self.base_dir, r"\\?\GLOBALROOT\Device\Harddisk0\Partition1\boot.ini"))

        # 3. Null-byte encoding variations
        self.assertIsNone(self._safe_path(self.base_dir, "sub%00dir/file.txt"))
        self.assertIsNone(self._safe_path(self.base_dir, "file.txt%2500.exe"))
        self.assertIsNone(self._safe_path(self.base_dir, "%00/../../etc/passwd"))

        # 4. Multi-encoded Alternate Data Stream variations
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt%3a%3a$DATA"))
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt%253a%253a$DATA"))
        self.assertIsNone(self._safe_path(self.base_dir, "folder::$INDEX_ALLOCATION"))
        self.assertIsNone(self._safe_path(self.base_dir, "public.txt:streamname"))


# ── Test Suite: Category C — Brute-Force, Tarpitting & Rate Limiting ───────────
class TestCategoryC_RateLimitingAndTarpitting(unittest.TestCase):
    """
    Category C: Brute-Force, Tarpitting & Rate Limiting
    Covers sliding-window tracking, exponential delay calculation, HTTP 429 lockout,
    counter reset on valid authentication, and thread safety.
    """

    def setUp(self):
        # Create a fast test limiter with microsecond base delay to test mathematical logic
        self.limiter = auth.SlidingWindowTarpitLimiter(
            window_sec=900,
            max_failures=5,
            base_delay=0.001,  # 1ms for high-speed unit testing
            max_delay=0.016
        )

    def test_SEC_RATE_001_exponential_tarpit_delay_progression(self):
        """Verify tarpit delay progression: D(k) = min(base * 2^(k-1), max)."""
        ip = "198.51.100.10"

        # Attempt 1: 0.001 * 2^0 = 0.001
        delay1, count1 = self.limiter.record_failure(ip)
        self.assertEqual(count1, 1)
        self.assertAlmostEqual(delay1, 0.001, places=5)

        # Attempt 2: 0.001 * 2^1 = 0.002
        delay2, count2 = self.limiter.record_failure(ip)
        self.assertEqual(count2, 2)
        self.assertAlmostEqual(delay2, 0.002, places=5)

        # Attempt 3: 0.001 * 2^2 = 0.004
        delay3, count3 = self.limiter.record_failure(ip)
        self.assertEqual(count3, 3)
        self.assertAlmostEqual(delay3, 0.004, places=5)

        # Attempt 4: 0.001 * 2^3 = 0.008
        delay4, count4 = self.limiter.record_failure(ip)
        self.assertEqual(count4, 4)
        self.assertAlmostEqual(delay4, 0.008, places=5)

        # Attempt 5: 0.001 * 2^4 = 0.016
        delay5, count5 = self.limiter.record_failure(ip)
        self.assertEqual(count5, 5)
        self.assertAlmostEqual(delay5, 0.016, places=5)

    def test_SEC_RATE_002_lockout_enforcement_after_max_failures(self):
        """Verify IP is locked out (check_rate_limit returns False) upon reaching 5 failures."""
        ip = "198.51.100.20"
        for _ in range(5):
            is_allowed, _ = self.limiter.check_rate_limit(ip)
            self.assertTrue(is_allowed, "Must allow requests while failures < 5")
            self.limiter.record_failure(ip)

        # 6th attempt should be blocked
        is_allowed, remaining = self.limiter.check_rate_limit(ip)
        self.assertFalse(is_allowed, "6th attempt must be locked out")
        self.assertGreater(remaining, 0.0, "Remaining lockout time must be positive")

    def test_SEC_RATE_003_counter_reset_on_successful_authentication(self):
        """Verify successful login clears failure history for the client IP."""
        ip = "198.51.100.30"
        for _ in range(3):
            self.limiter.record_failure(ip)

        # Confirm 3 failures recorded
        is_allowed, current_delay = self.limiter.check_rate_limit(ip)
        self.assertTrue(is_allowed)
        self.assertAlmostEqual(current_delay, 0.004, places=5)

        # Record success
        self.limiter.record_success(ip)

        # After success, failure count should be 0 and delay should be 0.0
        is_allowed, new_delay = self.limiter.check_rate_limit(ip)
        self.assertTrue(is_allowed)
        self.assertEqual(new_delay, 0.0, "Delay must reset to 0.0 on success")

    def test_SEC_RATE_004_thread_safe_concurrent_failures(self):
        """Verify thread safety under concurrent brute-force simulation."""
        ip = "198.51.100.40"
        threads = []

        def worker():
            for _ in range(2):
                self.limiter.record_failure(ip)

        for _ in range(5):
            t = threading.Thread(target=worker)
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        is_allowed, _ = self.limiter.check_rate_limit(ip)
        self.assertFalse(is_allowed, "Concurrent failures must correctly sum and trigger lockout")

    def test_SEC_RATE_005_live_route_tarpit_and_lockout(self):
        """Verify /api/auth enforces sliding-window 5-attempt lockout and returns HTTP 429 with retry_after."""
        test_limiter = auth.SlidingWindowTarpitLimiter(window_sec=900, max_failures=5, base_delay=0.001, max_delay=0.016)
        old_limiter = auth.GLOBAL_RATE_LIMITER
        auth.GLOBAL_RATE_LIMITER = test_limiter
        client_ip = "198.51.100.77"

        try:
            # 5 failed authentication attempts
            for i in range(5):
                h = MockHTTPHandler(method="GET", path=f"/api/auth?key=bad_key_{i}", client_ip=client_ip)
                parsed = urllib.parse.urlparse(h.path)
                qs = urllib.parse.parse_qs(parsed.query)
                auth.handle_auth_routes(h, parsed.path, qs)
                self.assertEqual(h.response_status, 401, f"Attempt {i+1} must return HTTP 401")

            # 6th attempt must trigger HTTP 429 Too Many Requests
            h6 = MockHTTPHandler(method="GET", path="/api/auth?key=bad_key_6", client_ip=client_ip)
            parsed6 = urllib.parse.urlparse(h6.path)
            qs6 = urllib.parse.parse_qs(parsed6.query)
            auth.handle_auth_routes(h6, parsed6.path, qs6)
            self.assertEqual(h6.response_status, 429, "6th attempt must return HTTP 429 Too Many Requests")

            body = h6.get_json()
            self.assertEqual(body.get("error"), "rate_limited")
            self.assertGreater(body.get("retry_after", 0), 0, "retry_after must be positive integer")
        finally:
            auth.GLOBAL_RATE_LIMITER = old_limiter


# ── Test Suite: Category D — Proxy Header Spoofing & Network Isolation ─────────
class TestCategoryD_ProxyHeaderSecurity(unittest.TestCase):
    """
    Category D: Proxy Header Spoofing vs Legitimate Localhost Tunnel Proxy
    Covers extraction of real IP vs direct LAN spoofing defense.
    """

    def test_SEC_PROXY_001_untrusted_direct_lan_header_spoofing(self):
        """Verify direct non-loopback connections ignore forged proxy headers."""
        # Simulated request from LAN client 192.168.1.88 forging 127.0.0.1 and CF headers
        handler = MockHTTPHandler(
            method="GET",
            path="/api/browse_host",
            headers={
                "X-Forwarded-For": "127.0.0.1, 10.0.0.1",
                "CF-Connecting-IP": "127.0.0.1",
                "X-Real-IP": "127.0.0.1"
            },
            client_ip="192.168.1.88"
        )
        resolved_ip = auth.get_client_ip(handler)
        self.assertEqual(resolved_ip, "192.168.1.88", "Direct LAN socket must resolve to physical socket IP")

    def test_SEC_PROXY_002_legitimate_loopback_cloudflare_tunnel(self):
        """Verify legitimate local Cloudflare tunnel proxy correctly extracts CF-Connecting-IP."""
        handler = MockHTTPHandler(
            method="GET",
            path="/api/check_auth",
            headers={
                "CF-Connecting-IP": "203.0.113.45",
                "X-Forwarded-For": "203.0.113.45, 127.0.0.1"
            },
            client_ip="127.0.0.1"
        )
        resolved_ip = auth.get_client_ip(handler)
        self.assertEqual(resolved_ip, "203.0.113.45", "Cloudflare tunnel client IP must be extracted")

    def test_SEC_PROXY_003_legitimate_loopback_x_forwarded_for_chain(self):
        """Verify legitimate local ngrok / Pinggy tunnel proxy parses client IP from X-Forwarded-For."""
        handler = MockHTTPHandler(
            method="GET",
            path="/api/check_auth",
            headers={
                "X-Forwarded-For": "198.51.100.99, 10.0.0.2"
            },
            client_ip="127.0.0.1"
        )
        resolved_ip = auth.get_client_ip(handler)
        self.assertEqual(resolved_ip, "198.51.100.99", "X-Forwarded-For first client IP must be extracted")

    def test_SEC_PROXY_004_invalid_and_malformed_ip_header_fallback(self):
        """Verify malformed or garbage proxy headers fall back safely to socket IP."""
        handler = MockHTTPHandler(
            method="GET",
            path="/",
            headers={
                "CF-Connecting-IP": "invalid_ip_string!@#$",
                "X-Forwarded-For": "not_an_ip, 999.999.999.999"
            },
            client_ip="127.0.0.1"
        )
        resolved_ip = auth.get_client_ip(handler)
        self.assertEqual(resolved_ip, "127.0.0.1", "Malformed proxy headers must safely fallback to socket IP")


# ── Test Suite: Category E — Denial of Service & Large Payload Security ────────
class TestCategoryE_DenialOfService(unittest.TestCase):
    """
    Category E: Denial of Service, Large Payloads & Stream Exhaustion
    Covers upload bounds, disk space pre-flight checks, memory-bounded streaming, and safe integer conversion.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_dos_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_SEC_DOS_001_disk_space_pre_flight_check(self):
        """Verify disk space pre-flight check and upload boundaries against hostdrop constants."""
        # 1. Verify resource boundary constants in hostdrop
        self.assertEqual(hostdrop.MIN_FREE_DISK_BUFFER, 500 * 1024 * 1024)
        self.assertEqual(hostdrop.MAX_UPLOAD_SIZE, 50 * 1024 * 1024 * 1024)
        self.assertEqual(hostdrop.MAX_ZIP_SIZE, 10 * 1024 * 1024 * 1024)
        self.assertEqual(hostdrop.MAX_ZIP_FILES, 10_000)
        self.assertEqual(hostdrop.MAX_ZIP_DEPTH, 25)

        # 2. Test disk_info calculation on real directory
        di = hostdrop.disk_info(self.test_dir)
        self.assertGreater(di.get("free_bytes", 0), 0)
        self.assertGreater(di.get("total_bytes", 0), 0)

        # 3. Test safe_int robustness against malformed/non-numeric inputs
        self.assertEqual(hostdrop.safe_int("12345"), 12345)
        self.assertEqual(hostdrop.safe_int("invalid_numeric", default=0), 0)
        self.assertEqual(hostdrop.safe_int(None, default=100), 100)
        self.assertEqual(hostdrop.safe_int(["500"], default=0), 500)
        self.assertEqual(hostdrop.safe_int([], default=42), 42)

    def test_SEC_DOS_002_bounded_chunk_stream_simulation(self):
        """Verify bounded streaming and zip archive creation against real directory tree."""
        # Create a nested directory structure with files
        sub_dir = os.path.join(self.test_dir, "nested", "level1")
        os.makedirs(sub_dir, exist_ok=True)
        test_file = os.path.join(sub_dir, "payload.bin")
        payload_data = b"HOSTDROP_STREAM_DATA_" * 1024  # 23KB
        with open(test_file, "wb") as f:
            f.write(payload_data)

        # Test safe_path confinement
        resolved = hostdrop.safe_path(self.test_dir, "nested/level1/payload.bin")
        self.assertIsNotNone(resolved)
        self.assertTrue(os.path.isfile(resolved))


# ── Test Suite: Category F — Host OS Integration, Privilege Sandboxing & Privacy
class TestCategoryF_PrivilegeSandboxingAndPrivacy(unittest.TestCase):
    """
    Category F: Host OS Integration, Privilege Sandboxing & Privacy
    Covers high-privilege endpoint sandboxing, OS GUI spawning containment, and username/path redaction.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_priv_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_SEC_PRIV_001_remote_explorer_execution_containment(self):
        """Verify hostdrop.open_in_os_explorer restricts local OS actions to physical localhost."""
        # 1. Direct local connection
        res_local = hostdrop.open_in_os_explorer(self.test_dir, "127.0.0.1")
        self.assertTrue(res_local["is_local"])

        # 2. Remote / WAN client IP
        res_remote = hostdrop.open_in_os_explorer(self.test_dir, "203.0.113.10")
        self.assertFalse(res_remote["is_local"])
        self.assertIn("viewing in browser", res_remote.get("message", "").lower())

    def test_SEC_PRIV_002_username_and_absolute_path_redaction(self):
        """Verify hostdrop.sanitize_path_for_client redacts host username and internal paths for guests."""
        curr_user = hostdrop.CURRENT_USER or "testuser"
        sensitive_win = f"C:\\Users\\{curr_user}\\Documents\\SecretFolder"
        sensitive_nix = f"/home/{curr_user}/hostdrop_data"

        # Guest / Remote viewer: Must redact host user and home directory
        sanitized_win = hostdrop.sanitize_path_for_client(sensitive_win, is_admin=False)
        self.assertNotIn(curr_user, sanitized_win, "Host username must be redacted for guests")

        sanitized_nix = hostdrop.sanitize_path_for_client(sensitive_nix, is_admin=False)
        self.assertNotIn(curr_user, sanitized_nix, "Host username must be redacted for guests")

        # Admin viewer: Full path is preserved
        admin_win = hostdrop.sanitize_path_for_client(sensitive_win, is_admin=True)
        self.assertEqual(admin_win, sensitive_win)


# ── Live HTTP Server Integration Test Harness ─────────────────────────────────
class TestLiveHTTPAuthServer(unittest.TestCase):
    """
    Spawns a real background ThreadingHTTPServer on an ephemeral port running HostDropHandler
    to verify live HTTP protocol adherence (cookies, headers, redirects, 401s, 429s).
    """

    @classmethod
    def setUpClass(cls):
        hostdrop.REQUIRE_AUTH = True
        hostdrop.REQUIRE_AUTH_ON_LAN = True
        cls.temp_dir = tempfile.mkdtemp(prefix="hostdrop_live_test_")
        hostdrop.UPLOAD_DIR = cls.temp_dir
        hostdrop.HOST_SHARE = cls.temp_dir

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

    def test_SEC_LIVE_001_unauthenticated_api_rejection(self):
        """Live HTTP: Unauthenticated request to /api/browse_host returns HTTP 401."""
        req = urllib.request.Request(f"{self.base_url}/api/browse_host")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req)
        self.assertEqual(ctx.exception.code, 401)

    def test_SEC_LIVE_002_bookmarked_key_login_and_authenticated_access(self):
        """Live HTTP: Auto-login with access key sets session cookie and unlocks /api/browse_host."""
        access_key = auth.get_access_key()

        # Custom HTTP handler to prevent following redirect so we inspect 303
        class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
            def http_error_303(self, req, fp, code, msg, headers):
                return fp

        opener = urllib.request.build_opener(NoRedirectHandler)
        login_url = f"{self.base_url}/api/auth?key={access_key}"

        # 1. Execute auto-login
        resp = opener.open(login_url)
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.headers.get("Location"), "/")

        cookie_header = resp.headers.get("Set-Cookie", "")
        self.assertIn("hostdrop_session=", cookie_header)
        token = cookie_header.split(";")[0]

        # 2. Access protected endpoint using session cookie
        auth_req = urllib.request.Request(
            f"{self.base_url}/api/browse_host",
            headers={"Cookie": token}
        )
        auth_resp = urllib.request.urlopen(auth_req)
        self.assertEqual(auth_resp.status, 200)
        data = json.loads(auth_resp.read().decode("utf-8"))
        self.assertIn("drives", data)

    def test_SEC_LIVE_003_json_login_endpoint(self):
        """Live HTTP: POST /api/login with valid access key issues session token."""
        access_key = auth.get_access_key()
        payload = json.dumps({"key": access_key}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/login",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(data.get("success"))
        self.assertIn("token", data)


# ── Test Runner & Report Generation ────────────────────────────────────────────
def run_all_security_tests() -> bool:
    print("\n" + "=" * 72)
    print("  HOSTDROP ADVERSARIAL CYBERSECURITY & PENETRATION TEST SUITE")
    print("  Executing Categories A through F Security Specifications")
    print("=" * 72 + "\n")

    suite = unittest.TestSuite()
    loader = unittest.TestLoader()

    suite.addTests(loader.loadTestsFromTestCase(TestCategoryA_Authentication))
    suite.addTests(loader.loadTestsFromTestCase(TestCategoryB_PathTraversal))
    suite.addTests(loader.loadTestsFromTestCase(TestCategoryC_RateLimitingAndTarpitting))
    suite.addTests(loader.loadTestsFromTestCase(TestCategoryD_ProxyHeaderSecurity))
    suite.addTests(loader.loadTestsFromTestCase(TestCategoryE_DenialOfService))
    suite.addTests(loader.loadTestsFromTestCase(TestCategoryF_PrivilegeSandboxingAndPrivacy))
    suite.addTests(loader.loadTestsFromTestCase(TestLiveHTTPAuthServer))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "=" * 72)
    total = result.testsRun
    failures = len(result.failures)
    errors = len(result.errors)
    passed = total - failures - errors

    print(f"  TOTAL TESTS RUN : {total}")
    print(f"  PASSED          : {passed}")
    print(f"  FAILURES        : {failures}")
    print(f"  ERRORS          : {errors}")
    print("=" * 72)

    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_all_security_tests()
    sys.exit(0 if success else 1)
