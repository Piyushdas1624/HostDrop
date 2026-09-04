"""
HostDrop Test Suite: Chunk Reassembly & Resumption
Covers:
- Sequential multi-chunk upload assembly with SHA-256 bit-exact verification
- Mid-upload interruption and resumption from exact byte offset
- Atomic seek & truncate rollback on duplicate/retry chunks
- Boundary rejections: negative offsets, beyond-EOF offsets, non-existent file resume
- 50 GB maximum storage cap and 500 MB disk headroom protection
"""

import os
import sys
import time
import json
import socket
import shutil
import hashlib
import tempfile
import threading
import unittest
import http.client
import urllib.request
import urllib.parse
import urllib.error
from unittest.mock import patch

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


class TestChunkReassembly(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_test_chunk_")
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

    def _upload_chunk(self, filename, data, offset=0, target="recv"):
        url = f"{self.base_url}/api/upload?path={urllib.parse.quote(filename)}&offset={offset}&target={target}"
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))

    def test_multi_chunk_sequential_assembly_integrity(self):
        """Uploads a file in 4 distinct chunks and verifies bit-for-bit SHA-256 match."""
        filename = "assembled_payload.bin"
        chunk1 = os.urandom(25 * 1024)
        chunk2 = os.urandom(25 * 1024)
        chunk3 = os.urandom(25 * 1024)
        chunk4 = os.urandom(25 * 1024)
        expected_payload = chunk1 + chunk2 + chunk3 + chunk4
        expected_sha = hashlib.sha256(expected_payload).hexdigest()

        # Chunk 1: offset 0
        status, res = self._upload_chunk(filename, chunk1, offset=0)
        self.assertEqual(status, 200)
        self.assertTrue(res.get("success"))

        # Chunk 2: offset 25 KB
        status, res = self._upload_chunk(filename, chunk2, offset=len(chunk1))
        self.assertEqual(status, 200)
        self.assertTrue(res.get("success"))

        # Chunk 3: offset 50 KB
        status, res = self._upload_chunk(filename, chunk3, offset=len(chunk1) + len(chunk2))
        self.assertEqual(status, 200)
        self.assertTrue(res.get("success"))

        # Chunk 4: offset 75 KB
        status, res = self._upload_chunk(filename, chunk4, offset=len(chunk1) + len(chunk2) + len(chunk3))
        self.assertEqual(status, 200)
        self.assertTrue(res.get("success"))

        # Verify disk file
        disk_path = os.path.join(self.test_recv, filename)
        self.assertTrue(os.path.exists(disk_path))
        self.assertEqual(os.path.getsize(disk_path), len(expected_payload))

        with open(disk_path, "rb") as f:
            actual_sha = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(actual_sha, expected_sha)

    def test_chunk_resume_after_simulated_interruption(self):
        """Uploads partial chunks, queries /api/check, and resumes remaining bytes."""
        filename = "resumed_transfer.dat"
        chunk1 = os.urandom(30 * 1024)
        chunk2 = os.urandom(30 * 1024)
        full_data = chunk1 + chunk2

        # Step 1: Upload chunk 1
        status, res = self._upload_chunk(filename, chunk1, offset=0)
        self.assertEqual(status, 200)

        # Step 2: Query /api/check
        check_url = f"{self.base_url}/api/check?path={urllib.parse.quote(filename)}&target=recv"
        with urllib.request.urlopen(check_url, timeout=5) as resp:
            check_data = json.loads(resp.read().decode("utf-8"))
        self.assertTrue(check_data.get("exists"))
        self.assertEqual(check_data.get("size"), len(chunk1))

        # Step 3: Resume from exact size reported by server
        resume_offset = check_data["size"]
        status, res = self._upload_chunk(filename, chunk2, offset=resume_offset)
        self.assertEqual(status, 200)

        # Step 4: Verify complete file on disk
        disk_path = os.path.join(self.test_recv, filename)
        self.assertEqual(os.path.getsize(disk_path), len(full_data))
        with open(disk_path, "rb") as f:
            self.assertEqual(f.read(), full_data)

    def test_atomic_rollback_and_truncate_on_retry(self):
        """Retrying a chunk at an existing offset truncates to that offset and replaces clean."""
        filename = "retry_truncate.dat"
        initial_data = b"A" * 40960  # 40 KB
        self._upload_chunk(filename, initial_data, offset=0)

        # Re-send from offset 20480 (20 KB) with different data
        replacement = b"B" * 20480  # 20 KB of 'B'
        status, res = self._upload_chunk(filename, replacement, offset=20480)
        self.assertEqual(status, 200)

        disk_path = os.path.join(self.test_recv, filename)
        self.assertEqual(os.path.getsize(disk_path), 40960)
        with open(disk_path, "rb") as f:
            content = f.read()
            self.assertEqual(content[:20480], b"A" * 20480)
            self.assertEqual(content[20480:], b"B" * 20480)

    def test_negative_offset_rejection(self):
        """Upload with negative offset returns HTTP 400."""
        filename = "negative_offset.bin"
        url = f"{self.base_url}/api/upload?path={filename}&offset=-500&target=recv"
        req = urllib.request.Request(url, data=b"data", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_offset_beyond_eof_rejection(self):
        """Upload with offset greater than current file size returns HTTP 400."""
        filename = "beyond_eof.bin"
        self._upload_chunk(filename, b"small", offset=0)

        # Attempt to write at offset 50000 on a 5-byte file
        url = f"{self.base_url}/api/upload?path={filename}&offset=50000&target=recv"
        req = urllib.request.Request(url, data=b"data", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_resume_non_existent_file_rejection(self):
        """Attempting to resume (offset > 0) a file that does not exist returns HTTP 400."""
        filename = "ghost_file.bin"
        url = f"{self.base_url}/api/upload?path={filename}&offset=1024&target=recv"
        req = urllib.request.Request(url, data=b"data", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_50gb_limit_rejection(self):
        """Upload exceeding MAX_UPLOAD_SIZE (50 GB) returns HTTP 413."""
        filename = "too_large.bin"
        url = f"{self.base_url}/api/upload?path={filename}&offset=0&target=recv"
        # Send raw HTTP request with fake Content-Length > 50 GB
        parsed = urllib.parse.urlparse(self.base_url)
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
        conn.request("POST", f"/api/upload?path={filename}&offset=0&target=recv",
                     headers={"Content-Length": str(hostdrop.MAX_UPLOAD_SIZE + 1024)})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()

    def test_insufficient_disk_buffer_rejection(self):
        """Upload rejected with HTTP 507 when free disk space is below needed + 500 MB."""
        filename = "disk_full.bin"
        fake_usage = shutil._ntuple_diskusage(total=10**12, used=10**12 - 10**6, free=10**6) # 1 MB free
        with patch("shutil.disk_usage", return_value=fake_usage):
            url = f"{self.base_url}/api/upload?path={filename}&offset=0&target=recv"
            req = urllib.request.Request(url, data=b"X" * 1024, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 507)


if __name__ == "__main__":
    unittest.main()
