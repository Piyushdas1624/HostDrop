"""
HostDrop Test Suite: /api/check Edge Cases & Smart Resume Verification
Covers:
- Querying existing files (exists: true, size: exact, free_bytes: >0)
- Querying non-existent and empty (0-byte) files
- Partially uploaded file size reporting for seamless smart resume
- Target directory scoping (target=recv vs target=share separation)
- Path parsing edge cases (missing params, empty paths, spaces, unicode characters)
- Directory vs file differentiation
- Remote unauthenticated access handling
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
import auth


class QuietThreadingHTTPServer(hostdrop.ThreadingHTTPServer):
    """Suppresses connection reset noise during test shutdowns."""
    def handle_error(self, request, client_address):
        exc_type, _, _ = sys.exc_info()
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


class TestApiCheckEdgeCases(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_test_check_")
        self.test_recv = os.path.join(self.test_dir, "recv")
        self.test_share = os.path.join(self.test_dir, "share")
        os.makedirs(self.test_recv, exist_ok=True)
        os.makedirs(self.test_share, exist_ok=True)

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

    def _get_check(self, path="", target="recv", extra_params=""):
        url = f"{self.base_url}/api/check?path={urllib.parse.quote(path)}&target={target}{extra_params}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_check_existing_file(self):
        """Querying an existing file returns exists: true, exact size, and free disk bytes."""
        filepath = os.path.join(self.test_recv, "existing_doc.pdf")
        payload = b"Hello HostDrop World" * 500  # 10,000 bytes
        with open(filepath, "wb") as f:
            f.write(payload)

        status, data = self._get_check("existing_doc.pdf", target="recv")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("exists"))
        self.assertEqual(data.get("size"), len(payload))
        self.assertGreater(data.get("free_bytes", 0), 0)

    def test_check_non_existent_file(self):
        """Querying a non-existent file returns exists: false, size: 0, and free disk bytes."""
        status, data = self._get_check("phantom_file.iso", target="recv")
        self.assertEqual(status, 200)
        self.assertFalse(data.get("exists"))
        self.assertEqual(data.get("size"), 0)
        self.assertGreater(data.get("free_bytes", 0), 0)

    def test_check_empty_zero_byte_file(self):
        """Querying an empty 0-byte file correctly returns exists: true and size: 0."""
        filepath = os.path.join(self.test_recv, "empty.txt")
        open(filepath, "wb").close()

        status, data = self._get_check("empty.txt", target="recv")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("exists"))
        self.assertEqual(data.get("size"), 0)

    def test_check_partially_uploaded_file(self):
        """Simulates partial upload; confirms /api/check reports exact bytes on disk."""
        filepath = os.path.join(self.test_recv, "video.mp4")
        partial_bytes = os.urandom(65536)  # 64 KB written so far
        with open(filepath, "wb") as f:
            f.write(partial_bytes)

        status, data = self._get_check("video.mp4", target="recv")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("exists"))
        self.assertEqual(data.get("size"), 65536)

    def test_check_directory_differentiation(self):
        """Querying a directory path returns exists: false (safe resume only targets files)."""
        sub_dir = os.path.join(self.test_recv, "subfolder")
        os.makedirs(sub_dir, exist_ok=True)

        status, data = self._get_check("subfolder", target="recv")
        self.assertEqual(status, 200)
        self.assertFalse(data.get("exists"))

    def test_check_target_directory_scoping(self):
        """target=recv and target=share are strictly isolated from each other."""
        # Create 'unique.txt' only in HOST_SHARE
        share_file = os.path.join(self.test_share, "unique.txt")
        with open(share_file, "w", encoding="utf-8") as f:
            f.write("In Share")

        # Query with target=recv -> must NOT find it
        status, data_recv = self._get_check("unique.txt", target="recv")
        self.assertFalse(data_recv.get("exists"))

        # Query with target=share -> must find it
        status, data_share = self._get_check("unique.txt", target="share")
        self.assertTrue(data_share.get("exists"))
        self.assertEqual(data_share.get("size"), os.path.getsize(share_file))

    def test_check_spaces_and_special_characters(self):
        """Handles filenames with spaces, parentheses, and URL-encoded symbols cleanly."""
        nested_dir = os.path.join(self.test_recv, "My Photos")
        os.makedirs(nested_dir, exist_ok=True)
        complex_name = "Vacation Photo (2026) #1 & final.jpg"
        filepath = os.path.join(nested_dir, complex_name)
        with open(filepath, "wb") as f:
            f.write(b"IMAGE_DATA")

        status, data = self._get_check(f"My Photos/{complex_name}", target="recv")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("exists"))
        self.assertEqual(data.get("size"), len(b"IMAGE_DATA"))

    def test_check_unicode_characters(self):
        """Handles unicode paths (e.g. Japanese, Hindi, emoji) accurately."""
        unicode_rel = "ドキュメント/レポート.txt"
        full_dir = os.path.join(self.test_recv, "ドキュメント")
        os.makedirs(full_dir, exist_ok=True)
        with open(os.path.join(self.test_recv, unicode_rel), "wb") as f:
            f.write(b"UNICODE_CONTENT")

        status, data = self._get_check(unicode_rel, target="recv")
        self.assertEqual(status, 200)
        self.assertTrue(data.get("exists"))
        self.assertEqual(data.get("size"), len(b"UNICODE_CONTENT"))

    def test_check_missing_path_parameter(self):
        """Calling /api/check with no path param returns exists: false, size: 0."""
        url = f"{self.base_url}/api/check?target=recv"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(resp.status, 200)
        self.assertFalse(data.get("exists"))
        self.assertEqual(data.get("size"), 0)

    def test_check_unauthenticated_remote_handling(self):
        """When called with remote proxy header without session, /api/check returns 401."""
        url = f"{self.base_url}/api/check?path=test.txt&target=recv"
        req = urllib.request.Request(url)
        # Simulate request arriving over Cloudflare Tunnel (CF-Connecting-IP header)
        req.add_header("CF-Connecting-IP", "203.0.113.195")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)
        err_body = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertTrue(err_body.get("login_required"))


if __name__ == "__main__":
    unittest.main()
