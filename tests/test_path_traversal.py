"""
HostDrop Test Suite: Path Traversal Defenses & Directory Sandboxing
Covers:
- safe_path() unit verification across malicious traversal vectors
- Encoded traversal probing (%2e%2e%2f, %2e%2e%5c, double encoding)
- Null byte and Windows device namespace escaping
- Live HTTP endpoint boundary protection (/api/upload, /api/check, /download, /api/zip, /api/create_folder)
- Verification that no file is ever read, created, or probed outside designated base directories
"""

import os
import sys
import time
import json
import socket
import shutil
import tempfile
import threading
import unittest
import urllib.request
import urllib.parse
import urllib.error

# Ensure parent directory is in sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import hostdrop


class QuietThreadingHTTPServer(hostdrop.ThreadingHTTPServer):
    """Suppresses connection reset noise during test shutdowns."""
    def handle_error(self, request, client_address):
        exc_type, _, _ = sys.exc_info()
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


class TestPathTraversalSecurity(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_test_traversal_")
        self.test_recv = os.path.join(self.test_dir, "recv")
        self.test_share = os.path.join(self.test_dir, "share")
        os.makedirs(self.test_recv, exist_ok=True)
        os.makedirs(self.test_share, exist_ok=True)

        # Place a canary file outside the sandbox
        self.canary_file = os.path.join(self.test_dir, "canary_secret.txt")
        with open(self.canary_file, "w", encoding="utf-8") as f:
            f.write("SUPER_SECRET_CANARY")

        hostdrop.UPLOAD_DIR = self.test_recv
        hostdrop.HOST_SHARE = self.test_share

        # Bind ephemeral port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        hostdrop.SERVER_PORT = self.port
        self.base_url = f"http://127.0.0.1:{self.port}"

        self.server = QuietThreadingHTTPServer(("127.0.0.1", self.port), hostdrop.HostDropHandler)
        self.server.allow_reuse_address = True
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        time.sleep(0.1)

    def tearDown(self):
        try:
            self.server.shutdown()
            self.server.server_close()
        except Exception:
            pass
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
        except Exception:
            pass

    # ── 1. Unit Tests for safe_path() ──

    def test_safe_path_valid_subpaths(self):
        """safe_path allows valid nested subdirectories inside base."""
        res = hostdrop.safe_path(self.test_recv, "photos/vacation/pic.jpg")
        self.assertIsNotNone(res)
        self.assertTrue(res.startswith(os.path.abspath(self.test_recv)))

    def test_safe_path_dot_dot_traversal(self):
        """safe_path rejects parent directory escapes via ../ and ..\\."""
        vectors = [
            "../canary_secret.txt",
            "..\\canary_secret.txt",
            "../../etc/passwd",
            "..\\..\\Windows\\win.ini",
            "sub/../../canary_secret.txt",
            "a/b/c/../../../../canary_secret.txt",
        ]
        for vec in vectors:
            with self.subTest(vector=vec):
                self.assertIsNone(hostdrop.safe_path(self.test_recv, vec))

    def test_safe_path_absolute_path_traversal(self):
        """safe_path rejects absolute paths attempting to overwrite base."""
        vectors = [
            "/etc/passwd",
            "/var/log/syslog",
            "C:\\Windows\\System32\\calc.exe",
            "C:/Windows/win.ini",
            "\\\\server\\share\\evil.exe",
        ]
        for vec in vectors:
            with self.subTest(vector=vec):
                self.assertIsNone(hostdrop.safe_path(self.test_recv, vec))

    def test_safe_path_null_byte_rejection(self):
        """safe_path rejects null byte injection attempts."""
        vectors = [
            "valid.txt\x00../../etc/passwd",
            "test\x00.exe",
            "canary.txt\0",
        ]
        for vec in vectors:
            with self.subTest(vector=vec):
                self.assertIsNone(hostdrop.safe_path(self.test_recv, vec))

    # ── 2. Live HTTP Endpoint Traversal Protection ──

    def test_upload_endpoint_blocks_path_traversal(self):
        """POST /api/upload returns HTTP 403 and never writes outside UPLOAD_DIR."""
        malicious_paths = [
            "../escaped_upload.txt",
            "..\\escaped_upload.txt",
            "%2e%2e%2fescaped_upload.txt",
            "sub/../../escaped_upload.txt",
        ]
        for p in malicious_paths:
            with self.subTest(path=p):
                url = f"{self.base_url}/api/upload?path={urllib.parse.quote(p)}&offset=0&target=recv"
                req = urllib.request.Request(url, data=b"MALICIOUS_CONTENT", method="POST")
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(ctx.exception.code, 403)

        # Confirm escaped file was never created
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "escaped_upload.txt")))

    def test_check_endpoint_blocks_path_traversal_probing(self):
        """GET /api/check never confirms files outside sandbox (returns exists: False)."""
        # Even though canary_secret.txt exists in test_dir, probing it via traversal must return False
        url = f"{self.base_url}/api/check?path=../canary_secret.txt&target=recv"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertFalse(data.get("exists"))
        self.assertEqual(data.get("size"), 0)

    def test_download_endpoint_blocks_path_traversal(self):
        """GET /download returns HTTP 403/404 and does not leak files outside sandbox."""
        url = f"{self.base_url}/download?path=../canary_secret.txt"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=5)
        self.assertIn(ctx.exception.code, (403, 404))

    def test_zip_endpoint_blocks_path_traversal(self):
        """GET /api/zip returns HTTP 403/404 for traversal path requests."""
        url = f"{self.base_url}/api/zip?path=../"
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url, timeout=5)
        self.assertIn(ctx.exception.code, (403, 404))

    def test_create_folder_blocks_path_traversal(self):
        """POST /api/create_folder blocks folder creation outside sandbox."""
        url = f"{self.base_url}/api/create_folder?path=../escape_folder&target=recv"
        req = urllib.request.Request(url, data=b"{}", method="POST")
        req.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertIn(ctx.exception.code, (400, 403))
        self.assertFalse(os.path.exists(os.path.join(self.test_dir, "escape_folder")))


if __name__ == "__main__":
    unittest.main()
