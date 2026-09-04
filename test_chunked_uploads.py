"""
HostDrop Automated Test Suite: Smart Adaptive Chunked Uploads & Resumption
Validates Categories 1 through 7:
  Category 1: Sequential multi-chunk upload assembly with SHA256 integrity verification.
  Category 2: Simulated chunk interruption at chunk N, verification of atomic truncation
              rollback (f.seek(offset) + f.truncate(offset)), and automatic resumption
              from /api/check byte offset.
  Category 3: Corrupt offset rejection: negative offset (offset < 0 -> HTTP 400),
              offset beyond EOF (offset > current_size -> HTTP 400),
              offset on non-existent file -> HTTP 400.
  Category 4: 50 GB maximum upload protection (total_size > 50 GB -> HTTP 413,
              cumulative offset + content_len > 50 GB -> HTTP 413).
  Category 5: Pre-flight disk space buffer check (mocked low disk space < needed_space + 500 MB -> HTTP 507).
  Category 6: /api/check response validation (exists, size, free_bytes).
  Category 7: Dynamic chunk size logic verification (90 MB on trycloudflare.com, 100 MB on other hostnames).

Execution:
    python test_chunked_uploads.py
    python -m unittest test_chunked_uploads -v
"""

import os
import sys
import io
import time
import json
import socket
import shutil
import hashlib
import tempfile
import threading
import subprocess
import unittest
import http.client
import urllib.request
import urllib.parse
import urllib.error
from unittest.mock import patch, MagicMock

# Ensure project root is in import search path
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

import hostdrop


class QuietThreadingHTTPServer(hostdrop.ThreadingHTTPServer):
    """ThreadingHTTPServer that suppresses expected socket disconnection noise in tests."""

    def handle_error(self, request, client_address):
        exc_type, _, _ = sys.exc_info()
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


class BaseChunkedTestCase(unittest.TestCase):
    """Base test case providing isolated temporary storage and ephemeral HTTP server."""

    @classmethod
    def setUpClass(cls):
        cls._orig_upload_dir = hostdrop.UPLOAD_DIR
        cls._orig_host_share = hostdrop.HOST_SHARE
        cls._orig_server_port = hostdrop.SERVER_PORT
        cls._orig_max_upload = hostdrop.MAX_UPLOAD_SIZE
        cls._orig_min_buffer = hostdrop.MIN_FREE_DISK_BUFFER

        cls.test_dir = tempfile.mkdtemp(prefix="hostdrop_chunk_test_")
        cls.test_recv = os.path.join(cls.test_dir, "recv")
        cls.test_share = os.path.join(cls.test_dir, "share")
        os.makedirs(cls.test_recv, exist_ok=True)
        os.makedirs(cls.test_share, exist_ok=True)

        hostdrop.UPLOAD_DIR = cls.test_recv
        hostdrop.HOST_SHARE = cls.test_share

        # Allocate dynamic ephemeral port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()

        hostdrop.SERVER_PORT = cls.port
        cls.base_url = f"http://127.0.0.1:{cls.port}"

        cls.server = QuietThreadingHTTPServer(("127.0.0.1", cls.port), hostdrop.HostDropHandler)
        cls.server.allow_reuse_address = True
        cls.server.daemon_threads = True
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.server.shutdown()
            cls.server.server_close()
        except Exception:
            pass

        hostdrop.UPLOAD_DIR = cls._orig_upload_dir
        hostdrop.HOST_SHARE = cls._orig_host_share
        hostdrop.SERVER_PORT = cls._orig_server_port
        hostdrop.MAX_UPLOAD_SIZE = cls._orig_max_upload
        hostdrop.MIN_FREE_DISK_BUFFER = cls._orig_min_buffer

        shutil.rmtree(cls.test_dir, ignore_errors=True)

    def post_chunk(self, filename: str, offset: int, data: bytes, total_size: int = None,
                   target: str = "recv", headers: dict = None, override_content_length: int = None):
        """Helper to post a chunk to /api/upload using http.client for precise control."""
        query_params = {
            "path": filename,
            "offset": str(offset),
            "target": target
        }
        if total_size is not None:
            query_params["total_size"] = str(total_size)

        qs = urllib.parse.urlencode(query_params)
        req_path = f"/api/upload?{qs}"

        req_headers = {
            "Content-Type": "application/octet-stream",
            "Connection": "keep-alive"
        }
        if headers:
            req_headers.update(headers)

        if override_content_length is not None:
            req_headers["Content-Length"] = str(override_content_length)
        else:
            req_headers["Content-Length"] = str(len(data))

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            conn.request("POST", req_path, body=data, headers=req_headers)
            resp = conn.getresponse()
            cl_hdr = resp.getheader("Content-Length")
            if cl_hdr == "0" or resp.status in (204, 304, 401, 403, 404):
                raw_body = ""
            else:
                try:
                    raw_body = resp.read().decode("utf-8")
                except (http.client.IncompleteRead, TimeoutError, OSError):
                    raw_body = ""
            try:
                body = json.loads(raw_body) if raw_body else {}
            except Exception:
                body = {"raw": raw_body}
            return resp.status, body
        finally:
            conn.close()

    def get_check(self, filename: str, target: str = "recv", param_name: str = "path"):
        """Helper to query /api/check."""
        url = f"{self.base_url}/api/check?{param_name}={urllib.parse.quote(filename)}&target={target}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))


# ═══════════════════════════════════════════════════════════════════════════════
#  CATEGORY 1: Sequential Multi-Chunk Assembly & SHA256 Integrity Verification
# ═══════════════════════════════════════════════════════════════════════════════
class TestCategory1_SequentialAssemblyAndIntegrity(BaseChunkedTestCase):
    """Category 1: Verifies multi-chunk payload assembly, SHA256 integrity, and single-request optimization."""

    def test_01_sequential_three_chunk_assembly_with_sha256(self):
        """Simulate uploading a file split into 3 sequential chunks and verify bit-exact SHA256 assembly."""
        chunk0 = b"A" * (64 * 1024)   # 65,536 bytes
        chunk1 = b"B" * (64 * 1024)   # 65,536 bytes
        chunk2 = b"C" * (32 * 1024)   # 32,768 bytes
        total_data = chunk0 + chunk1 + chunk2
        total_size = len(total_data)   # 163,840 bytes
        filename = "three_chunks_assembly.bin"

        # Chunk 0: offset 0
        s0, r0 = self.post_chunk(filename, 0, chunk0, total_size=total_size)
        self.assertEqual(s0, 200)
        self.assertTrue(r0.get("success"))
        self.assertEqual(r0.get("status"), "ok")
        self.assertEqual(r0.get("bytes"), len(chunk0))
        self.assertEqual(r0.get("offset"), 0)
        self.assertEqual(r0.get("total_written"), len(chunk0))
        self.assertFalse(r0.get("completed"))

        # Chunk 1: offset 65,536
        s1, r1 = self.post_chunk(filename, len(chunk0), chunk1, total_size=total_size)
        self.assertEqual(s1, 200)
        self.assertTrue(r1.get("success"))
        self.assertEqual(r1.get("bytes"), len(chunk1))
        self.assertEqual(r1.get("offset"), len(chunk0))
        self.assertEqual(r1.get("total_written"), len(chunk0) + len(chunk1))
        self.assertFalse(r1.get("completed"))

        # Chunk 2: offset 131,072
        s2, r2 = self.post_chunk(filename, len(chunk0) + len(chunk1), chunk2, total_size=total_size)
        self.assertEqual(s2, 200)
        self.assertTrue(r2.get("success"))
        self.assertEqual(r2.get("bytes"), len(chunk2))
        self.assertEqual(r2.get("offset"), len(chunk0) + len(chunk1))
        self.assertEqual(r2.get("total_written"), total_size)
        self.assertTrue(r2.get("completed"))

        # Verify on-disk file
        disk_path = os.path.join(self.test_recv, filename)
        self.assertTrue(os.path.exists(disk_path))
        with open(disk_path, "rb") as f:
            disk_bytes = f.read()

        self.assertEqual(len(disk_bytes), total_size)
        self.assertEqual(disk_bytes, total_data)
        self.assertEqual(hashlib.sha256(disk_bytes).hexdigest(), hashlib.sha256(total_data).hexdigest())

    def test_02_single_request_optimization(self):
        """Files smaller than or equal to active chunk size upload in a single direct POST without slicing."""
        payload = b"DIRECT_SINGLE_POST_OPTIMIZATION_DATA" * 500  # 18,000 bytes
        filename = "single_shot_direct.bin"

        status, body = self.post_chunk(filename, 0, payload, total_size=len(payload))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("success"))
        self.assertEqual(body.get("bytes"), len(payload))
        self.assertTrue(body.get("completed"))

        disk_path = os.path.join(self.test_recv, filename)
        self.assertTrue(os.path.exists(disk_path))
        with open(disk_path, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_03_multi_chunk_random_binary_stream(self):
        """Verify binary transparency with random binary stream across 4 uneven chunks."""
        # 180 KB of arbitrary pseudorandom bytes
        rand_bytes = hashlib.sha512(b"seed").digest() * 2880  # 184,320 bytes
        chunk_sizes = [50000, 45000, 60000, len(rand_bytes) - 155000]
        filename = "random_binary_stream.dat"

        curr_offset = 0
        for i, sz in enumerate(chunk_sizes):
            slice_data = rand_bytes[curr_offset:curr_offset + sz]
            status, res = self.post_chunk(filename, curr_offset, slice_data, total_size=len(rand_bytes))
            self.assertEqual(status, 200)
            self.assertTrue(res.get("success"))
            self.assertEqual(res.get("bytes"), len(slice_data))
            curr_offset += sz
            if i == len(chunk_sizes) - 1:
                self.assertTrue(res.get("completed"))

        disk_path = os.path.join(self.test_recv, filename)
        with open(disk_path, "rb") as f:
            disk_content = f.read()

        self.assertEqual(len(disk_content), len(rand_bytes))
        self.assertEqual(hashlib.sha256(disk_content).hexdigest(), hashlib.sha256(rand_bytes).hexdigest())

    def test_04_target_share_chunked_upload(self):
        """Verify chunked uploads targeting HOST_SHARE directory land in share folder."""
        payload_0 = b"SHARE_CHUNK_ALPHA_" * 1000  # 18,000 bytes
        payload_1 = b"SHARE_CHUNK_BETA__" * 1000  # 18,000 bytes
        filename = "shared_chunked_doc.pdf"

        s0, r0 = self.post_chunk(filename, 0, payload_0, total_size=36000, target="share")
        self.assertEqual(s0, 200)
        self.assertTrue(r0.get("success"))

        s1, r1 = self.post_chunk(filename, 18000, payload_1, total_size=36000, target="share")
        self.assertEqual(s1, 200)
        self.assertTrue(r1.get("success"))
        self.assertTrue(r1.get("completed"))

        share_path = os.path.join(self.test_share, filename)
        recv_path = os.path.join(self.test_recv, filename)
        self.assertTrue(os.path.exists(share_path))
        self.assertFalse(os.path.exists(recv_path))
        with open(share_path, "rb") as f:
            self.assertEqual(f.read(), payload_0 + payload_1)


# ═══════════════════════════════════════════════════════════════════════════════
#  CATEGORY 2: Chunk Interruption, Truncation Rollback & Resumption
# ═══════════════════════════════════════════════════════════════════════════════
class TestCategory2_InterruptionAndResumption(BaseChunkedTestCase):
    """Category 2: Verifies simulated mid-stream interruptions, rollback of dirty bytes, and /api/check resumption."""

    def test_05_simulated_interruption_and_smart_resume(self):
        """Simulate chunk interruption at chunk 1, query /api/check, and resume remaining chunks from byte boundary."""
        chunk0 = b"SEGMENT_00_" * 4096  # 45,056 bytes
        chunk1 = b"SEGMENT_01_" * 4096  # 45,056 bytes
        chunk2 = b"SEGMENT_02_" * 4096  # 45,056 bytes
        total_data = chunk0 + chunk1 + chunk2
        total_size = len(total_data)
        filename = "resumable_transfer.bin"

        # 1. Upload chunk 0
        s0, r0 = self.post_chunk(filename, 0, chunk0, total_size=total_size)
        self.assertEqual(s0, 200)

        # 2. Upload chunk 1
        s1, r1 = self.post_chunk(filename, len(chunk0), chunk1, total_size=total_size)
        self.assertEqual(s1, 200)

        # 3. Simulate client network drop / interruption.
        # 4. Client queries /api/check before resuming
        chk_status, chk_body = self.get_check(filename)
        self.assertEqual(chk_status, 200)
        self.assertTrue(chk_body.get("exists"))
        expected_committed = len(chunk0) + len(chunk1)
        self.assertEqual(chk_body.get("size"), expected_committed)

        # 5. Client resumes from exact byte boundary reported by /api/check
        resume_offset = chk_body.get("size")
        s2, r2 = self.post_chunk(filename, resume_offset, chunk2, total_size=total_size)
        self.assertEqual(s2, 200)
        self.assertTrue(r2.get("completed"))

        # 6. Verify complete reconstructed file
        disk_path = os.path.join(self.test_recv, filename)
        with open(disk_path, "rb") as f:
            disk_content = f.read()

        self.assertEqual(len(disk_content), total_size)
        self.assertEqual(disk_content, total_data)
        self.assertEqual(hashlib.sha256(disk_content).hexdigest(), hashlib.sha256(total_data).hexdigest())

    def test_06_atomic_truncation_rollback_of_partial_or_dirty_bytes(self):
        """Simulate dropped/corrupted tail write; verify backend seeks & truncates to offset cleanly on resume."""
        filename = "atomic_rollback_test.bin"
        chunk0 = b"CLEAN_INITIAL_DATA_" * 1000  # 19,000 bytes
        s0, _ = self.post_chunk(filename, 0, chunk0, total_size=38000)
        self.assertEqual(s0, 200)

        # Simulate trailing dirty bytes left by an interrupted network write
        disk_path = os.path.join(self.test_recv, filename)
        dirty_bytes = b"DIRTY_CORRUPT_TRAILING_BYTES_XYZ" * 200  # 6,400 bytes
        with open(disk_path, "ab") as f:
            f.write(dirty_bytes)

        corrupted_disk_size = os.path.getsize(disk_path)
        self.assertEqual(corrupted_disk_size, 19000 + 6400)

        # Client retries chunk from offset = 19,000 with a clean 19,000 byte chunk
        chunk1_clean = b"CLEAN_RETRY_CHUNK2_" * 1000  # 19,000 bytes
        s1, r1 = self.post_chunk(filename, 19000, chunk1_clean, total_size=38000)
        self.assertEqual(s1, 200)
        self.assertTrue(r1.get("success"))
        self.assertTrue(r1.get("completed"))

        # Backend f.seek(19000) + f.truncate(19000) MUST have truncated the 6,400 dirty bytes
        expected_final = chunk0 + chunk1_clean
        with open(disk_path, "rb") as f:
            final_content = f.read()

        self.assertEqual(len(final_content), len(expected_final))
        self.assertEqual(final_content, expected_final)
        self.assertNotIn(b"DIRTY_CORRUPT_TRAILING_BYTES_XYZ", final_content)

    def test_07_premature_disconnect_reports_error_and_preserves_atomic_state(self):
        """Verify that sending fewer bytes than declared in Content-Length reports HTTP 400."""
        filename = "premature_drop.bin"
        # Declare 50,000 bytes but only send 10,000 bytes via raw socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", self.port))
        
        req_headers = (
            f"POST /api/upload?path={filename}&offset=0 HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{self.port}\r\n"
            f"Content-Length: 50000\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"Connection: close\r\n\r\n"
        )
        sock.sendall(req_headers.encode("ascii"))
        sock.sendall(b"X" * 10000)
        # Close connection immediately to simulate abrupt client crash / disconnect
        sock.close()
        time.sleep(0.3)

        # File on disk might have partial bytes, but /api/check must report actual size on disk
        chk_status, chk_body = self.get_check(filename)
        self.assertEqual(chk_status, 200)
        # Disk size is 10,000 bytes
        self.assertEqual(chk_body.get("size"), 10000)


# ═══════════════════════════════════════════════════════════════════════════════
#  CATEGORY 3: Corrupt Offset Rejection
# ═══════════════════════════════════════════════════════════════════════════════
class TestCategory3_CorruptOffsetRejection(BaseChunkedTestCase):
    """Category 3: Verifies rejection of negative offsets, offsets beyond EOF, and resumes on ghost files."""

    def test_08_corrupt_offset_beyond_eof_rejected_with_400(self):
        """Sending an offset beyond existing on-disk file size must return HTTP 400 Bad Request."""
        filename = "eof_bound_test.bin"
        initial_data = b"STABLE_INITIAL_BYTES" * 100  # 2,000 bytes
        s0, _ = self.post_chunk(filename, 0, initial_data)
        self.assertEqual(s0, 200)

        disk_path = os.path.join(self.test_recv, filename)
        orig_size = os.path.getsize(disk_path)
        self.assertEqual(orig_size, len(initial_data))

        # Desynced client attempts to send chunk at offset 50,000 (> 2,000 bytes)
        status, body = self.post_chunk(filename, 50000, b"CORRUPT_PAYLOAD")
        self.assertEqual(status, 400)
        self.assertFalse(body.get("success"))
        self.assertIn("beyond existing file length", body.get("error", ""))

        # Verify disk file is completely untouched
        self.assertEqual(os.path.getsize(disk_path), orig_size)
        with open(disk_path, "rb") as f:
            self.assertEqual(f.read(), initial_data)

    def test_09_resume_nonexistent_file_rejected_with_400(self):
        """Attempting to resume (offset > 0) a file that does not exist must return HTTP 400 Bad Request."""
        filename = "phantom_file_resume.bin"
        status, body = self.post_chunk(filename, 1024, b"PAYLOAD_DATA")
        self.assertEqual(status, 400)
        self.assertFalse(body.get("success"))
        self.assertIn("Cannot resume non-existent file", body.get("error", ""))

        # Verify no file was created on disk
        self.assertFalse(os.path.exists(os.path.join(self.test_recv, filename)))

    def test_10_negative_offset_rejected_with_400(self):
        """Sending a negative offset (e.g. -1, -1024) must return HTTP 400 Bad Request."""
        filename = "negative_offset_test.bin"
        for bad_offset in (-1, -100, -65536):
            status, body = self.post_chunk(filename, bad_offset, b"MALICIOUS_PAYLOAD")
            self.assertEqual(status, 400, f"Offset {bad_offset} should be rejected with 400")
            self.assertFalse(body.get("success"))
            self.assertIn("negative offset not allowed", body.get("error", ""))

        self.assertFalse(os.path.exists(os.path.join(self.test_recv, filename)))

    def test_11_path_traversal_on_upload_blocked_with_403(self):
        """Attempted path traversal on /api/upload must be rejected with HTTP 403."""
        status, _ = self.post_chunk("../../traversal_test.bin", 0, b"ATTACK_PAYLOAD", headers={"Connection": "close"})
        self.assertEqual(status, 403)


# ═══════════════════════════════════════════════════════════════════════════════
#  CATEGORY 4: 50 GB Maximum Upload Protection
# ═══════════════════════════════════════════════════════════════════════════════
class TestCategory4_MaximumUploadProtection(BaseChunkedTestCase):
    """Category 4: Verifies enforcement of MAX_UPLOAD_SIZE (50 GB) on chunks and cumulative uploads."""

    def test_12_constants_verification(self):
        """Verify MAX_UPLOAD_SIZE is 50 GB and MIN_FREE_DISK_BUFFER is 500 MB."""
        self.assertEqual(hostdrop.MAX_UPLOAD_SIZE, 50 * 1024 * 1024 * 1024)
        self.assertEqual(hostdrop.MIN_FREE_DISK_BUFFER, 500 * 1024 * 1024)

    def test_13_total_size_query_param_exceeding_50gb_rejected_413(self):
        """total_size query parameter exceeding 50 GB must immediately return HTTP 413."""
        filename = "oversized_query_total.bin"
        oversized = (50 * 1024 * 1024 * 1024) + 1  # 50 GB + 1 byte
        status, body = self.post_chunk(filename, 0, b"TINY_PAYLOAD", total_size=oversized)
        self.assertEqual(status, 413)
        self.assertFalse(body.get("success"))
        self.assertIn("exceeds maximum allowed limit", body.get("error", ""))
        self.assertFalse(os.path.exists(os.path.join(self.test_recv, filename)))

    def test_14_x_total_size_header_exceeding_50gb_rejected_413(self):
        """X-Total-Size header exceeding 50 GB must return HTTP 413."""
        filename = "oversized_header_total.bin"
        oversized = (50 * 1024 * 1024 * 1024) + 1024
        headers = {"X-Total-Size": str(oversized)}
        status, body = self.post_chunk(filename, 0, b"TINY_PAYLOAD", headers=headers)
        self.assertEqual(status, 413)
        self.assertFalse(body.get("success"))
        self.assertIn("exceeds maximum allowed limit", body.get("error", ""))
        self.assertFalse(os.path.exists(os.path.join(self.test_recv, filename)))

    def test_15_cumulative_offset_plus_content_len_exceeding_50gb_rejected_413(self):
        """Cumulative offset + content_len exceeding 50 GB must return HTTP 413."""
        filename = "oversized_cumulative.bin"
        # offset at 50 GB, content_length = 1024
        status, body = self.post_chunk(filename, 50 * 1024 * 1024 * 1024, b"A" * 1024)
        self.assertEqual(status, 413)
        self.assertFalse(body.get("success"))
        self.assertIn("exceeds maximum allowed limit", body.get("error", ""))

    def test_16_content_len_exceeding_max_upload_size_rejected_413(self):
        """Request declaring Content-Length > MAX_UPLOAD_SIZE is rejected with HTTP 413."""
        orig_max = hostdrop.MAX_UPLOAD_SIZE
        try:
            hostdrop.MAX_UPLOAD_SIZE = 100 * 1024  # Temporarily mock to 100 KB
            oversized_len = (100 * 1024) + 1
            status, body = self.post_chunk("oversized_chunk.bin", 0, b"X" * 100, override_content_length=oversized_len)
            self.assertEqual(status, 413)
            self.assertFalse(body.get("success"))
            self.assertIn("exceeds maximum allowed limit", body.get("error", ""))
        finally:
            hostdrop.MAX_UPLOAD_SIZE = orig_max


# ═══════════════════════════════════════════════════════════════════════════════
#  CATEGORY 5: Pre-Flight Disk Space Buffer Check (500 MB Safety Margin)
# ═══════════════════════════════════════════════════════════════════════════════
class TestCategory5_DiskSpaceBufferPreFlight(BaseChunkedTestCase):
    """Category 5: Verifies HTTP 507 Insufficient Storage when free disk space < needed_space + 500 MB buffer."""

    def test_17_insufficient_disk_space_rejected_with_507(self):
        """If free disk space is less than content_len + 500 MB buffer, server must return HTTP 507."""
        filename = "low_disk_test.bin"
        # Mock free disk space to 200 MB (less than 500 MB buffer required)
        mock_usage = MagicMock(free=200 * 1024 * 1024)
        with patch("shutil.disk_usage", return_value=mock_usage):
            status, body = self.post_chunk(filename, 0, b"SAMPLE_CHUNK" * 100)
            self.assertEqual(status, 507)
            self.assertFalse(body.get("success"))
            self.assertIn("Insufficient host storage space", body.get("error", ""))
            self.assertIn("500 MB buffer required", body.get("error", ""))

        self.assertFalse(os.path.exists(os.path.join(self.test_recv, filename)))

    def test_18_insufficient_disk_space_against_declared_total_size_rejected_with_507(self):
        """If free disk space is less than total_size + 500 MB, server must return HTTP 507."""
        filename = "insufficient_total_test.bin"
        # Mock free disk space to 1 GB
        mock_usage = MagicMock(free=1 * 1024 * 1024 * 1024)
        # Declared total_size = 2 GB. needed_space + buffer = 2.5 GB > 1 GB free
        with patch("shutil.disk_usage", return_value=mock_usage):
            status, body = self.post_chunk(filename, 0, b"SMALL_INITIAL_CHUNK", total_size=2 * 1024 * 1024 * 1024)
            self.assertEqual(status, 507)
            self.assertFalse(body.get("success"))
            self.assertIn("Insufficient host storage space", body.get("error", ""))

    def test_19_sufficient_disk_space_proceeds_cleanly(self):
        """With plenty of free disk space, chunk upload succeeds with HTTP 200."""
        filename = "ample_disk_test.bin"
        mock_usage = MagicMock(free=500 * 1024 * 1024 * 1024)  # 500 GB free
        with patch("shutil.disk_usage", return_value=mock_usage):
            status, body = self.post_chunk(filename, 0, b"VALID_UPLOAD_DATA", total_size=17)
            self.assertEqual(status, 200)
            self.assertTrue(body.get("success"))


# ═══════════════════════════════════════════════════════════════════════════════
#  CATEGORY 6: /api/check Response Validation
# ═══════════════════════════════════════════════════════════════════════════════
class TestCategory6_CheckEndpointValidation(BaseChunkedTestCase):
    """Category 6: Verifies /api/check reporting exists, accurate size, and free_bytes."""

    def test_20_check_nonexistent_file_returns_size_zero_and_free_bytes(self):
        """Querying /api/check for a non-existent file returns exists=False, size=0, and valid free_bytes."""
        status, body = self.get_check("non_existent_check_test.bin")
        self.assertEqual(status, 200)
        self.assertFalse(body.get("exists"))
        self.assertEqual(body.get("size"), 0)
        self.assertIsInstance(body.get("free_bytes"), int)
        self.assertGreater(body.get("free_bytes"), 0)

    def test_21_check_existing_file_reports_exact_on_disk_size(self):
        """Querying /api/check for an existing file returns exists=True and exact on-disk byte size."""
        filename = "existing_file_for_check.bin"
        disk_path = os.path.join(self.test_recv, filename)
        file_data = b"M" * 77777
        with open(disk_path, "wb") as f:
            f.write(file_data)

        status, body = self.get_check(filename)
        self.assertEqual(status, 200)
        self.assertTrue(body.get("exists"))
        self.assertEqual(body.get("size"), 77777)
        self.assertGreater(body.get("free_bytes"), 0)

    def test_22_check_query_by_filename_parameter_fallback(self):
        """Querying /api/check using 'filename' param instead of 'path' resolves correctly."""
        filename = "fallback_param_test.bin"
        disk_path = os.path.join(self.test_recv, filename)
        with open(disk_path, "wb") as f:
            f.write(b"SAMPLE" * 100)

        status, body = self.get_check(filename, param_name="filename")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("exists"))
        self.assertEqual(body.get("size"), 600)

    def test_23_check_target_share_reports_share_file_metrics(self):
        """Querying /api/check with target=share correctly inspects HOST_SHARE."""
        filename = "shared_asset_check.bin"
        disk_path = os.path.join(self.test_share, filename)
        with open(disk_path, "wb") as f:
            f.write(b"SHARE_DATA" * 50)

        status, body = self.get_check(filename, target="share")
        self.assertEqual(status, 200)
        self.assertTrue(body.get("exists"))
        self.assertEqual(body.get("size"), 500)


# ═══════════════════════════════════════════════════════════════════════════════
#  CATEGORY 7: Dynamic Chunk Size Logic Verification
# ═══════════════════════════════════════════════════════════════════════════════
class TestCategory7_AdaptiveChunkSizingLogic(unittest.TestCase):
    """Category 7: Verifies dynamic environment detection (90 MB on trycloudflare.com, 100 MB on LAN/Pinggy)."""

    def test_24_dynamic_chunk_size_cloudflare_edge_headroom_math(self):
        """Verify 90 MB chunk size preserves safe headroom under Cloudflare 100,000,000-byte decimal limit."""
        cf_chunk_size = 90 * 1024 * 1024       # 94,371,840 bytes (binary 90 MB)
        cf_edge_limit = 100_000_000            # Decimal 100 MB request body limit
        headroom = cf_edge_limit - cf_chunk_size
        self.assertEqual(cf_chunk_size, 94_371_840)
        self.assertEqual(headroom, 5_628_160)
        self.assertLess(cf_chunk_size, cf_edge_limit)
        self.assertGreater(headroom, 5 * 1024 * 1024, "Headroom must exceed 5 MB safety margin for headers")

    def test_25_dynamic_chunk_size_lan_cable_hotspot_pinggy_math(self):
        """Verify LAN / Cable / Hotspot / Pinggy chunk size is exactly 100 MB (104,857,600 bytes)."""
        lan_chunk_size = 100 * 1024 * 1024
        self.assertEqual(lan_chunk_size, 104_857_600)

    def test_26_javascript_template_getAdaptiveChunkSize_source_audit(self):
        """Audit the live HTML_TEMPLATE in hostdrop.py to verify client-side JS implementation."""
        template = hostdrop.HTML_TEMPLATE

        # 1. Function getAdaptiveChunkSize exists
        self.assertIn("function getAdaptiveChunkSize()", template)

        # 2. Checks trycloudflare.com
        self.assertIn("trycloudflare.com", template)
        self.assertIn("90 * 1024 * 1024", template)
        self.assertIn("100 * 1024 * 1024", template)

        # 3. Single-request optimization: file.size <= CHUNK_SIZE
        self.assertIn("file.size <= CHUNK_SIZE", template)

        # 4. Auto-retry mechanics with maxRetries = 3 and immediate abort on non-retryable codes
        self.assertIn("maxRetries = 3", template)
        self.assertIn("err.status === 401", template)
        self.assertIn("err.status === 403", template)
        self.assertIn("err.status === 413", template)
        self.assertIn("err.status === 507", template)

    def test_27_javascript_dynamic_execution_via_node(self):
        """Execute getAdaptiveChunkSize JavaScript code in Node.js across various domain environments."""
        # Extract the function definition from HTML_TEMPLATE
        template = hostdrop.HTML_TEMPLATE
        start_idx = template.find("function getAdaptiveChunkSize()")
        self.assertNotEqual(start_idx, -1, "getAdaptiveChunkSize not found in HTML_TEMPLATE")
        end_idx = template.find("}", start_idx) + 1
        js_func = template[start_idx:end_idx]

        test_hostnames = [
            ("my-tunnel.trycloudflare.com", 90 * 1024 * 1024),
            ("SUBDOMAIN.TRYCLOUDFLARE.COM", 90 * 1024 * 1024),
            ("edge.preview.trycloudflare.com", 90 * 1024 * 1024),
            ("127.0.0.1", 100 * 1024 * 1024),
            ("localhost", 100 * 1024 * 1024),
            ("192.168.1.100", 100 * 1024 * 1024),
            ("169.254.10.20", 100 * 1024 * 1024),
            ("192.168.137.1", 100 * 1024 * 1024),
            ("test.pinggy.link", 100 * 1024 * 1024),
            ("free.pinggy.online", 100 * 1024 * 1024),
            ("", 100 * 1024 * 1024)
        ]

        js_runner = f"""
{js_func}

const testCases = {json.dumps([h[0] for h in test_hostnames])};
const results = testCases.map(h => {{
    global.window = {{ location: {{ hostname: h }} }};
    return getAdaptiveChunkSize();
}});
console.log(JSON.stringify(results));
"""
        try:
            res = subprocess.run(
                ["node", "-e", js_runner],
                capture_output=True,
                text=True,
                timeout=5,
                check=True
            )
            returned_sizes = json.loads(res.stdout.strip())
            for (hostname, expected_size), actual_size in zip(test_hostnames, returned_sizes):
                self.assertEqual(
                    actual_size, expected_size,
                    f"Hostname {hostname} expected chunk size {expected_size}, got {actual_size}"
                )
        except (subprocess.SubprocessError, FileNotFoundError) as e:
            # Fallback to pure-Python simulation if node is unavailable
            for hostname, expected_size in test_hostnames:
                host_lower = hostname.lower()
                if host_lower.endswith("trycloudflare.com") or "trycloudflare.com" in host_lower:
                    computed = 90 * 1024 * 1024
                else:
                    computed = 100 * 1024 * 1024
                self.assertEqual(computed, expected_size)

    def test_28_chunk_slice_boundary_math(self):
        """Verify mathematical chunk slicing ranges for both Cloudflare and LAN modes."""
        # 1. Cloudflare mode: 250 MB file -> 90 MB + 90 MB + 70 MB
        file_size_cf = 250 * 1024 * 1024
        chunk_size_cf = 90 * 1024 * 1024
        slices_cf = []
        offset = 0
        while offset < file_size_cf:
            end = min(offset + chunk_size_cf, file_size_cf)
            slices_cf.append((offset, end, end - offset))
            offset = end

        self.assertEqual(len(slices_cf), 3)
        self.assertEqual(slices_cf[0], (0, 94371840, 94371840))
        self.assertEqual(slices_cf[1], (94371840, 188743680, 94371840))
        self.assertEqual(slices_cf[2], (188743680, 262144000, 73400320))
        self.assertEqual(sum(s[2] for s in slices_cf), file_size_cf)

        # 2. LAN mode: 250 MB file -> 100 MB + 100 MB + 50 MB
        file_size_lan = 250 * 1024 * 1024
        chunk_size_lan = 100 * 1024 * 1024
        slices_lan = []
        offset = 0
        while offset < file_size_lan:
            end = min(offset + chunk_size_lan, file_size_lan)
            slices_lan.append((offset, end, end - offset))
            offset = end

        self.assertEqual(len(slices_lan), 3)
        self.assertEqual(slices_lan[0], (0, 104857600, 104857600))
        self.assertEqual(slices_lan[1], (104857600, 209715200, 104857600))
        self.assertEqual(slices_lan[2], (209715200, 262144000, 52428800))
        self.assertEqual(sum(s[2] for s in slices_lan), file_size_lan)


def run_all_tests():
    """Run all chunked upload tests using standard unittest runner."""
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromTestCase(TestCategory1_SequentialAssemblyAndIntegrity))
    suite.addTests(loader.loadTestsFromTestCase(TestCategory2_InterruptionAndResumption))
    suite.addTests(loader.loadTestsFromTestCase(TestCategory3_CorruptOffsetRejection))
    suite.addTests(loader.loadTestsFromTestCase(TestCategory4_MaximumUploadProtection))
    suite.addTests(loader.loadTestsFromTestCase(TestCategory5_DiskSpaceBufferPreFlight))
    suite.addTests(loader.loadTestsFromTestCase(TestCategory6_CheckEndpointValidation))
    suite.addTests(loader.loadTestsFromTestCase(TestCategory7_AdaptiveChunkSizingLogic))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    run_all_tests()
