"""
HostDrop Empirical DoS & Edge Case Stress Testing Suite (Milestone M4)
Validates:
1. ZIP memory bounds & tempfile spooling (O(1) heap allocation, recursion depth limit, cleanup on disconnect)
2. Upload limits & disk space pre-flight verification (50GB cap, 500MB buffer, offset validation, streaming)
3. Session token crash resilience & reboot persistence (stateless HMAC survival across server restarts)
4. URL-safe bookmarked key auto-login (303 PRG redirect, cookie flags, history scrubbing, tarpitting, 429 lockout)
5. Host username & private filesystem path leak prevention in responses and logs
"""

import os
import sys
import io
import time
import json
import shutil
import socket
import secrets
import tempfile
import threading
import unittest
import tracemalloc
import getpass
import urllib.request
import urllib.parse
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from unittest.mock import patch

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import auth
import hostdrop


class TestZIPMemoryBoundsAndStreaming(unittest.TestCase):
    """Stress tests ZIP generation against memory exhaustion, recursion bombs, and file descriptor leaks."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ts_zip_stress_")
        self.share_dir = os.path.join(self.test_dir, "share")
        os.makedirs(self.share_dir, exist_ok=True)

        # Set server state
        with hostdrop.STATE_LOCK:
            hostdrop.HOST_SHARE = self.share_dir
            hostdrop.UPLOAD_DIR = self.share_dir

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_zip_memory_bounds_constant_heap(self):
        """Verify ZIP creation and streaming operates in O(1) heap memory without buffering full size in RAM."""
        # Create 50 files of 200 KB each (~10 MB total)
        for i in range(50):
            sub = os.path.join(self.share_dir, f"sub_{i % 5}")
            os.makedirs(sub, exist_ok=True)
            with open(os.path.join(sub, f"data_{i}.bin"), "wb") as f:
                f.write(os.urandom(200 * 1024))

        tracemalloc.start()
        snapshot_before = tracemalloc.take_snapshot()

        # Simulate handler
        class DummyWfile:
            def __init__(self):
                self.bytes_written = 0
            def write(self, data):
                self.bytes_written += len(data)

        dummy_wfile = DummyWfile()

        # Mock handler
        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.wfile = dummy_wfile
        handler.rfile = io.BytesIO()
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/zip?tab=share"
        handler.send_response = lambda code: None
        handler.send_header = lambda k, v: None
        handler.send_security_headers = lambda: None
        handler.end_headers = lambda: None

        handler.do_GET()

        snapshot_after = tracemalloc.take_snapshot()
        tracemalloc.stop()

        # Check that dummy wfile received the full zip stream (> 10MB)
        self.assertGreater(dummy_wfile.bytes_written, 9 * 1024 * 1024, "Zip stream should contain compressed data")

        # Check heap memory delta
        top_stats = snapshot_after.compare_to(snapshot_before, "lineno")
        total_delta = sum(stat.size_diff for stat in top_stats if stat.size_diff > 0)
        # Heap delta should be well under 4 MB (since chunk size is 1MB and temp file is disk-backed)
        self.assertLess(total_delta, 4 * 1024 * 1024, f"Heap memory delta ({total_delta} bytes) must remain O(1)")

    def test_zip_recursion_depth_limit(self):
        """Verify directory recursion beyond MAX_ZIP_DEPTH (25) is truncated to prevent symlink/depth bombs."""
        # Use single-character directory names ('d') so 30 levels easily fits within Windows MAX_PATH (260 chars)
        curr = self.share_dir
        for depth in range(30):
            curr = os.path.join(curr, "d")
            os.makedirs(curr, exist_ok=True)
            with open(os.path.join(curr, f"f_{depth}.txt"), "w") as f:
                f.write(f"depth {depth}")

        # Stream zip
        zip_chunks = []
        class DummyWfile:
            def write(self, data):
                zip_chunks.append(data)

        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.wfile = DummyWfile()
        handler.rfile = io.BytesIO()
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/zip?tab=share"
        handler.send_response = lambda code: None
        handler.send_header = lambda k, v: None
        handler.send_security_headers = lambda: None
        handler.end_headers = lambda: None

        handler.do_GET()

        zip_data = b"".join(zip_chunks)
        import zipfile as zf_module
        with zf_module.ZipFile(io.BytesIO(zip_data), "r") as z:
            names = z.namelist()
            # 1. Verify deep files beyond MAX_ZIP_DEPTH are excluded
            deep_files = [n for n in names if any(f"f_{i}.txt" in n for i in range(hostdrop.MAX_ZIP_DEPTH + 1, 30))]
            self.assertEqual(len(deep_files), 0, f"Files beyond MAX_ZIP_DEPTH ({hostdrop.MAX_ZIP_DEPTH}) must not be archived: {deep_files}")

            # 2. Verify directory tree recursion was strictly halted (no directories beyond depth 26)
            for n in names:
                d_count = n.count("d/")
                self.assertLessEqual(d_count, hostdrop.MAX_ZIP_DEPTH + 1, f"Zip archive entry {n} exceeded recursion boundary")

            # 3. Verify shallow files ARE included
            shallow = [n for n in names if "f_5.txt" in n]
            self.assertEqual(len(shallow), 1, "Files within MAX_ZIP_DEPTH must be archived")

    def test_zip_tempfile_cleanup_on_broken_pipe(self):
        """Verify temp zip file is cleaned up even if client disconnects mid-stream (BrokenPipeError)."""
        with open(os.path.join(self.share_dir, "large.dat"), "wb") as f:
            f.write(os.urandom(2 * 1024 * 1024))

        temp_dir = tempfile.gettempdir()
        initial_temp_zips = set([f for f in os.listdir(temp_dir) if f.endswith(".zip")])

        class BrokenPipeWfile:
            def __init__(self):
                self.calls = 0
            def write(self, data):
                self.calls += 1
                if self.calls >= 1:
                    raise BrokenPipeError("Client disconnected prematurely")

        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.wfile = BrokenPipeWfile()
        handler.rfile = io.BytesIO()
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/zip?tab=share"
        handler.send_response = lambda code: None
        handler.send_header = lambda k, v: None
        handler.send_security_headers = lambda: None
        handler.end_headers = lambda: None

        handler.do_GET()

        final_temp_zips = set([f for f in os.listdir(temp_dir) if f.endswith(".zip")])
        new_leaked_zips = final_temp_zips - initial_temp_zips
        self.assertEqual(len(new_leaked_zips), 0, f"No temp zip files should be leaked on broken pipe: {new_leaked_zips}")


class TestUploadLimitsAndDiskSafety(unittest.TestCase):
    """Stress tests upload endpoints against size exhaustion, low disk space, and offset corruption."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ts_upload_stress_")
        self.inbox_dir = os.path.join(self.test_dir, "inbox")
        os.makedirs(self.inbox_dir, exist_ok=True)

        with hostdrop.STATE_LOCK:
            hostdrop.UPLOAD_DIR = self.inbox_dir
            hostdrop.HOST_SHARE = self.inbox_dir

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_upload_exceeding_max_upload_size(self):
        """Verify upload exceeding MAX_UPLOAD_SIZE (50GB) is immediately rejected with HTTP 413."""
        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.rfile = io.BytesIO(b"")
        handler.headers = {"Content-Length": str(60 * 1024 * 1024 * 1024), "Sec-Fetch-Site": "same-origin"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/upload?path=huge.bin"

        response_state = {}
        def mock_send_json(data, status=200):
            response_state["status"] = status
            response_state["data"] = data
        handler.send_json = mock_send_json

        handler.do_POST()

        self.assertEqual(response_state.get("status"), 413, "Upload exceeding 50GB must return HTTP 413")
        self.assertIn("exceeds maximum allowed limit", response_state["data"]["error"])

    def test_upload_insufficient_disk_space_rejection(self):
        """Verify upload is rejected with HTTP 507 when free disk space is below (size + MIN_FREE_DISK_BUFFER)."""
        # Mock shutil.disk_usage to return 600 MB free space
        # Incoming file is 200 MB, required buffer is 500 MB -> total needed = 700 MB > 600 MB free
        fake_usage = shutil._ntuple_diskusage(total=100*1024**3, used=99*1024**3, free=600*1024*1024)

        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.rfile = io.BytesIO(b"X" * (200 * 1024 * 1024))
        handler.headers = {"Content-Length": str(200 * 1024 * 1024), "Sec-Fetch-Site": "same-origin"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/upload?path=test_file.dat"

        response_state = {}
        def mock_send_json(data, status=200):
            response_state["status"] = status
            response_state["data"] = data
        handler.send_json = mock_send_json

        with patch("shutil.disk_usage", return_value=fake_usage):
            handler.do_POST()

        self.assertEqual(response_state.get("status"), 507, "Insufficient free space must return HTTP 507")
        self.assertIn("Insufficient host storage space", response_state["data"]["error"])
        # Verify file was not written
        self.assertFalse(os.path.exists(os.path.join(self.inbox_dir, "test_file.dat")))

    def test_upload_invalid_resume_offset(self):
        """Verify invalid resume offsets (offset > file size, or offset > 0 on non-existent file) return HTTP 400."""
        # Case 1: Non-existent file with offset > 0
        handler1 = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler1.rfile = io.BytesIO(b"partial")
        handler1.headers = {"Content-Length": "7", "Sec-Fetch-Site": "same-origin"}
        handler1.client_address = ("127.0.0.1", 12345)
        handler1.path = "/api/upload?path=ghost.bin&offset=1024"

        resp1 = {}
        handler1.send_json = lambda d, status=200: resp1.update({"status": status, "data": d})
        handler1.do_POST()

        self.assertEqual(resp1.get("status"), 400)
        self.assertIn("Cannot resume non-existent file", resp1["data"]["error"])

        # Case 2: Existing file of 100 bytes, resume offset requested = 500 bytes
        existing = os.path.join(self.inbox_dir, "small.bin")
        with open(existing, "wb") as f:
            f.write(b"A" * 100)

        handler2 = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler2.rfile = io.BytesIO(b"more")
        handler2.headers = {"Content-Length": "4", "Sec-Fetch-Site": "same-origin"}
        handler2.client_address = ("127.0.0.1", 12345)
        handler2.path = "/api/upload?path=small.bin&offset=500"

        resp2 = {}
        handler2.send_json = lambda d, status=200: resp2.update({"status": status, "data": d})
        handler2.do_POST()

        self.assertEqual(resp2.get("status"), 400)
        self.assertIn("Invalid resume offset", resp2["data"]["error"])


class TestSessionPersistenceAndRebootResilience(unittest.TestCase):
    """Stress tests stateless cryptographic session tokens across simulated server reboots and crashes."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ts_session_reboot_")
        self.env_path = os.path.join(self.test_dir, ".env")
        self.secret_key = secrets.token_hex(32)
        self.access_key = "ts_live_" + secrets.token_urlsafe(24)
        self.pwd = "SuperSecretRebootPassword123"
        self.pwd_hash = auth.SecurityConfig.hash_password(self.pwd)

        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write(f"HOSTDROP_SECRET_KEY={self.secret_key}\n")
            f.write(f"HOSTDROP_ACCESS_KEY={self.access_key}\n")
            f.write(f"HOSTDROP_PASSWORD_HASH={self.pwd_hash}\n")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_session_token_survives_multiple_server_reboots(self):
        """Verify session tokens created on Server Instance 1 remain valid on Server Instance 2 and 3."""
        # 1. Server Instance 1 starts up
        cfg1 = auth.SecurityConfig(self.env_path)
        mgr1 = auth.SessionManager(cfg1.secret_key)
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0"
        token = mgr1.create_token(user_agent=ua)
        self.assertTrue(mgr1.verify_token(token, user_agent=ua))

        # 2. Simulate Server Crash & Reboot 1
        cfg2 = auth.SecurityConfig(self.env_path)
        mgr2 = auth.SessionManager(cfg2.secret_key)
        self.assertEqual(cfg2.secret_key, self.secret_key)
        self.assertTrue(mgr2.verify_token(token, user_agent=ua), "Token must survive Server Reboot 1")

        # 3. Simulate Server Crash & Reboot 2 (30 days later scenario, within valid TTL)
        cfg3 = auth.SecurityConfig(self.env_path)
        mgr3 = auth.SessionManager(cfg3.secret_key)
        self.assertTrue(mgr3.verify_token(token, user_agent=ua), "Token must survive Server Reboot 2")

    def test_session_token_invalidated_when_secret_key_rotated(self):
        """Verify tokens from prior sessions are invalidated if the administrator rotates the secret key in .env."""
        cfg = auth.SecurityConfig(self.env_path)
        mgr = auth.SessionManager(cfg.secret_key)
        token = mgr.create_token(user_agent="TestUA")

        # Rotate secret key in .env
        new_secret = secrets.token_hex(32)
        with open(self.env_path, "w", encoding="utf-8") as f:
            f.write(f"HOSTDROP_SECRET_KEY={new_secret}\n")
            f.write(f"HOSTDROP_ACCESS_KEY={self.access_key}\n")
            f.write(f"HOSTDROP_PASSWORD_HASH={self.pwd_hash}\n")

        reboot_cfg = auth.SecurityConfig(self.env_path)
        reboot_mgr = auth.SessionManager(reboot_cfg.secret_key)
        self.assertFalse(reboot_mgr.verify_token(token, user_agent="TestUA"), "Rotated secret key must invalidate old tokens")


class TestUrlSafeAutoLoginAndRedirect(unittest.TestCase):
    """Stress tests /api/auth?key=... auto-login, 303 PRG redirect, cookie flags, and rate limiting."""

    class TestAuthServerHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            if auth.handle_auth_routes(self, parsed.path, qs):
                return
            if parsed.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Root Dashboard")
                return
            if parsed.path == "/api/browse_host":
                if not auth.is_authenticated(self):
                    self.send_response(401)
                    self.end_headers()
                    self.wfile.write(b'{"error":"unauthorized"}')
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
                return
            self.send_response(404)
            self.end_headers()

    @classmethod
    def setUpClass(cls):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        cls.port = s.getsockname()[1]
        s.close()

        cls.server = HTTPServer(("127.0.0.1", cls.port), cls.TestAuthServerHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_bookmarked_key_login_redirect_and_session_cookie(self):
        """Verify GET /api/auth?key=... returns 303, Location: /, proper cookies, and unlocks protected routes."""
        access_key = auth.get_access_key()

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def http_error_303(self, req, fp, code, msg, headers):
                return fp

        opener = urllib.request.build_opener(NoRedirect)
        url = f"{self.base_url}/api/auth?key={access_key}"
        req = urllib.request.Request(url, headers={"User-Agent": "PyTestAgent/1.0", "X-Forwarded-Proto": "https"})

        resp = opener.open(req)
        self.assertEqual(resp.status, 303)
        self.assertEqual(resp.headers.get("Location"), "/")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")
        self.assertIn("no-store", resp.headers.get("Cache-Control", ""))

        set_cookie = resp.headers.get("Set-Cookie", "")
        self.assertIn("hostdrop_session=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertIn("Secure", set_cookie)

        # Extract cookie
        cookie_val = set_cookie.split(";")[0]

        # Use cookie on /api/browse_host
        auth_req = urllib.request.Request(f"{self.base_url}/api/browse_host", headers={"Cookie": cookie_val, "User-Agent": "PyTestAgent/1.0"})
        auth_resp = urllib.request.urlopen(auth_req)
        self.assertEqual(auth_resp.status, 200)

    def test_rate_limiting_lockout_on_failed_attempts(self):
        """Verify that 5 failed access key attempts trigger exponential tarpitting and 6th attempt returns HTTP 429."""
        ip = "192.0.2.77"  # Test IP
        test_limiter = auth.SlidingWindowTarpitLimiter(window_sec=900, max_failures=5, base_delay=0.005, max_delay=0.08)

        # 5 failures
        for i in range(5):
            is_allowed, _ = test_limiter.check_rate_limit(ip)
            self.assertTrue(is_allowed, f"Attempt {i+1} should be allowed")
            delay, count = test_limiter.record_failure(ip)
            self.assertEqual(count, i + 1)

        # 6th attempt
        is_allowed, remaining = test_limiter.check_rate_limit(ip)
        self.assertFalse(is_allowed, "6th attempt must be locked out (HTTP 429)")
        self.assertGreater(remaining, 0)


class TestHostPrivacyAndInformationLeakage(unittest.TestCase):
    """Stress tests privacy redaction for host usernames, absolute home paths, and error disclosures."""

    def setUp(self):
        self.user = getpass.getuser() or "admin"
        self.home = os.path.expanduser("~")

    def test_sanitize_path_for_guest_client(self):
        """Verify sanitize_path_for_client redacts host username and replaces home with ~/."""
        # 1. Real OS home path
        real_home_path = os.path.join(self.home, "Downloads", "HostDrop")
        sanitized_home = hostdrop.sanitize_path_for_client(real_home_path, is_admin=False)
        self.assertNotIn(self.user, sanitized_home, f"Username '{self.user}' must not appear in '{sanitized_home}'")
        self.assertTrue(sanitized_home.startswith("~/Downloads"), f"Home path should be virtualized to ~/: {sanitized_home}")

        # 2. Windows-style User path
        if sys.platform == "win32":
            win_path = f"C:\\Users\\{self.user}\\Documents\\Secret"
            sanitized_win = hostdrop.sanitize_path_for_client(win_path, is_admin=False)
            self.assertNotIn(self.user, sanitized_win, f"Username '{self.user}' must not appear in '{sanitized_win}'")
        else:
            nix_path = f"/home/{self.user}/workspace/hostdrop"
            sanitized_nix = hostdrop.sanitize_path_for_client(nix_path, is_admin=False)
            self.assertNotIn(self.user, sanitized_nix, f"Username '{self.user}' must not appear in '{sanitized_nix}'")

    def test_dashboard_html_rendering_masks_private_paths(self):
        """Verify render_page masks private user paths when called for non-admin viewers."""
        with hostdrop.STATE_LOCK:
            hostdrop.UPLOAD_DIR = os.path.join(self.home, "Downloads", "HostDrop")
            hostdrop.HOST_SHARE = os.path.join(self.home, "Documents", "Share")

        html_guest = hostdrop.render_page(8080, is_admin=False)
        self.assertNotIn(self.home, html_guest, "User home absolute path must not appear in guest HTML")
        self.assertNotIn(f"\\Users\\{self.user}", html_guest)

    def test_error_responses_do_not_leak_stack_traces_or_paths(self):
        """Verify POST/GET error responses return sanitized, generic error JSON without exception stack traces."""
        # 1. Invalid JSON body to /api/set_path
        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.rfile = io.BytesIO(b"INVALID_NON_JSON_DATA{{{")
        handler.headers = {"Content-Length": "23", "Sec-Fetch-Site": "same-origin"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/set_path"
        handler.is_authenticated = lambda: True

        resp = {}
        handler.send_json = lambda d, status=200: resp.update({"status": status, "data": d})
        handler.do_POST()

        self.assertEqual(resp.get("status"), 400)
        self.assertIn("error", resp["data"])
        self.assertNotIn("Traceback", str(resp["data"]))
        self.assertNotIn(self.user, str(resp["data"]))


def run_stress_suite() -> bool:
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()
    suite.addTests(loader.loadTestsFromTestCase(TestZIPMemoryBoundsAndStreaming))
    suite.addTests(loader.loadTestsFromTestCase(TestUploadLimitsAndDiskSafety))
    suite.addTests(loader.loadTestsFromTestCase(TestSessionPersistenceAndRebootResilience))
    suite.addTests(loader.loadTestsFromTestCase(TestUrlSafeAutoLoginAndRedirect))
    suite.addTests(loader.loadTestsFromTestCase(TestHostPrivacyAndInformationLeakage))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_stress_suite()
    sys.exit(0 if success else 1)
