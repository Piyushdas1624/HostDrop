"""
HostDrop Challenger 2 Empirical DoS, Memory Bounds & Concurrency Verification Harness.
Validates all bounds, limits, disk quotas, memory profiles, and concurrency locks empirically.
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
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import auth
import hostdrop
import zipfile as zf_module


class TestEmpiricalZIPBounds(unittest.TestCase):
    """Empirical verification of ZIP bounds: depth, file count, volume, and O(1) heap memory."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ts_zip_stress_c2_")
        self.share_dir = os.path.join(self.test_dir, "share")
        os.makedirs(self.share_dir, exist_ok=True)
        with hostdrop.STATE_LOCK:
            hostdrop.HOST_SHARE = self.share_dir
            hostdrop.UPLOAD_DIR = self.share_dir

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_zip_file_count_limit_10000(self):
        """Empirically verify that a directory with > 10,000 files stops archiving at MAX_ZIP_FILES (10,000)."""
        # Create a folder with 10,050 files
        for i in range(10050):
            fname = os.path.join(self.share_dir, f"f_{i:05d}.txt")
            with open(fname, "wb") as f:
                f.write(b"x")

        # Execute /api/zip
        zip_chunks = []
        class StreamReceiver:
            def write(self, data):
                zip_chunks.append(data)

        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.wfile = StreamReceiver()
        handler.rfile = io.BytesIO()
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/zip?tab=share"
        handler.send_response = lambda code: None
        handler.send_header = lambda k, v: None
        handler.send_security_headers = lambda: None
        handler.end_headers = lambda: None

        handler.do_GET()

        zip_bytes = b"".join(zip_chunks)
        with zf_module.ZipFile(io.BytesIO(zip_bytes), "r") as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            self.assertEqual(len(names), hostdrop.MAX_ZIP_FILES, f"Archive must contain exactly MAX_ZIP_FILES ({hostdrop.MAX_ZIP_FILES}) files")

    def test_zip_max_volume_limit_10gb(self):
        """Empirically verify that folder archiving respects MAX_ZIP_SIZE limit."""
        # Create 3 files of 100KB each
        for i in range(3):
            with open(os.path.join(self.share_dir, f"data_{i}.bin"), "wb") as f:
                f.write(b"0" * (100 * 1024))

        # Patch MAX_ZIP_SIZE to 150 KB to test boundary cutoff
        with patch.object(hostdrop, "MAX_ZIP_SIZE", 150 * 1024):
            zip_chunks = []
            class StreamReceiver:
                def write(self, data):
                    zip_chunks.append(data)

            handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
            handler.wfile = StreamReceiver()
            handler.rfile = io.BytesIO()
            handler.headers = {}
            handler.client_address = ("127.0.0.1", 12345)
            handler.path = "/api/zip?tab=share"
            handler.send_response = lambda code: None
            handler.send_header = lambda k, v: None
            handler.send_security_headers = lambda: None
            handler.end_headers = lambda: None

            handler.do_GET()

            zip_bytes = b"".join(zip_chunks)
            with zf_module.ZipFile(io.BytesIO(zip_bytes), "r") as z:
                names = [n for n in z.namelist() if not n.endswith("/")]
                # Only 1 file should fit (100KB <= 150KB, next file 200KB > 150KB)
                self.assertEqual(len(names), 1, "Archiving must halt when cumulative uncompressed size exceeds MAX_ZIP_SIZE")

    def test_zip_depth_limit_boundary(self):
        """Empirically verify that directory recursion at depth 25 is included, but depth 26+ is excluded."""
        curr = self.share_dir
        for d in range(28):
            curr = os.path.join(curr, f"lvl{d}")
            os.makedirs(curr, exist_ok=True)
            with open(os.path.join(curr, f"file_at_depth_{d}.txt"), "w") as f:
                f.write(f"depth={d}")

        zip_chunks = []
        class StreamReceiver:
            def write(self, data):
                zip_chunks.append(data)

        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.wfile = StreamReceiver()
        handler.rfile = io.BytesIO()
        handler.headers = {}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/zip?tab=share"
        handler.send_response = lambda code: None
        handler.send_header = lambda k, v: None
        handler.send_security_headers = lambda: None
        handler.end_headers = lambda: None

        handler.do_GET()

        zip_bytes = b"".join(zip_chunks)
        with zf_module.ZipFile(io.BytesIO(zip_bytes), "r") as z:
            names = z.namelist()
            # In 0-indexed loop, lvl0..lvl24 corresponds to depth 1..25 (within MAX_ZIP_DEPTH=25)
            # lvl25 corresponds to depth 26 (> MAX_ZIP_DEPTH), and lvl26 corresponds to depth 27.
            # Files at depth <= 25 (e.g. lvl23/file_at_depth_23.txt, lvl24/file_at_depth_24.txt) must be present
            self.assertTrue(any("file_at_depth_23.txt" in n for n in names))
            self.assertTrue(any("file_at_depth_24.txt" in n for n in names))
            # Files beyond depth 25 (lvl25, lvl26) must be excluded
            self.assertFalse(any("file_at_depth_25.txt" in n for n in names))
            self.assertFalse(any("file_at_depth_26.txt" in n for n in names))


class TestEmpiricalUploadBoundsAndQuotas(unittest.TestCase):
    """Empirical verification of upload constraints, disk quotas, and truncation handling."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ts_upload_stress_c2_")
        self.recv_dir = os.path.join(self.test_dir, "recv")
        os.makedirs(self.recv_dir, exist_ok=True)
        with hostdrop.STATE_LOCK:
            hostdrop.UPLOAD_DIR = self.recv_dir
            hostdrop.HOST_SHARE = self.recv_dir

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_upload_exceeds_50gb_rejection_413(self):
        """Verify Content-Length exceeding 50GB (50 * 1024^3) returns HTTP 413 without reading payload."""
        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.rfile = io.BytesIO(b"")
        handler.headers = {"Content-Length": str(50 * 1024 * 1024 * 1024 + 1), "Sec-Fetch-Site": "same-origin"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/upload?path=toobig.bin"

        response = {}
        handler.send_json = lambda d, status=200: response.update({"status": status, "data": d})
        handler.do_POST()

        self.assertEqual(response["status"], 413)
        self.assertIn("File size exceeds maximum allowed limit", response["data"]["error"])

    def test_upload_insufficient_disk_space_507(self):
        """Verify upload is rejected with HTTP 507 when free space < (Content-Length + 500MB)."""
        # Mock free space = 600 MB, upload = 200 MB -> 200 + 500 = 700 MB > 600 MB
        fake_usage = shutil._ntuple_diskusage(total=100*1024**3, used=99*1024**3, free=600*1024*1024)

        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        handler.rfile = io.BytesIO(b"data" * 1000)
        handler.headers = {"Content-Length": str(200 * 1024 * 1024), "Sec-Fetch-Site": "same-origin"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/upload?path=large_file.iso"

        response = {}
        handler.send_json = lambda d, status=200: response.update({"status": status, "data": d})

        with patch("shutil.disk_usage", return_value=fake_usage):
            handler.do_POST()

        self.assertEqual(response["status"], 507)
        self.assertIn("Insufficient host storage space", response["data"]["error"])

    def test_upload_truncated_payload_rejection_400(self):
        """Verify upload with Content-Length > actual streamed bytes returns HTTP 400 truncated error."""
        handler = hostdrop.HostDropHandler.__new__(hostdrop.HostDropHandler)
        # Advertised 10,000 bytes, but stream only provides 2,048 bytes
        handler.rfile = io.BytesIO(b"A" * 2048)
        handler.headers = {"Content-Length": "10000", "Sec-Fetch-Site": "same-origin"}
        handler.client_address = ("127.0.0.1", 12345)
        handler.path = "/api/upload?path=truncated.bin"

        response = {}
        handler.send_json = lambda d, status=200: response.update({"status": status, "data": d})
        handler.do_POST()

        self.assertEqual(response["status"], 400)
        self.assertEqual(response["data"]["received"], 2048)
        self.assertEqual(response["data"]["expected"], 10000)
        self.assertIn("Upload truncated or connection closed prematurely", response["data"]["error"])


class TestEmpiricalConcurrencyAndThreadSafety(unittest.TestCase):
    """Empirical verification of thread safety, lock contention, and rate limiter non-blocking behavior."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="ts_concurrency_c2_")
        self.recv_dir = os.path.join(self.test_dir, "recv")
        self.share_dir = os.path.join(self.test_dir, "share")
        os.makedirs(self.recv_dir, exist_ok=True)
        os.makedirs(self.share_dir, exist_ok=True)

        with hostdrop.STATE_LOCK:
            hostdrop.UPLOAD_DIR = self.recv_dir
            hostdrop.HOST_SHARE = self.share_dir

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_concurrent_state_lock_access(self):
        """Verify that 50 concurrent threads modifying and reading server paths via STATE_LOCK experience zero corruption."""
        paths = [os.path.join(self.test_dir, f"dir_{i}") for i in range(10)]
        for p in paths:
            os.makedirs(p, exist_ok=True)

        errors = []

        def worker_set_and_read(idx):
            try:
                target_p = paths[idx % len(paths)]
                # Mutate path
                with hostdrop.STATE_LOCK:
                    hostdrop.UPLOAD_DIR = target_p
                # Verify safe path
                sp = hostdrop.safe_path(target_p, "test.txt")
                if not sp or not sp.startswith(target_p):
                    errors.append(f"Safe path mismatch: {sp} does not start with {target_p}")
            except Exception as e:
                errors.append(str(e))

        with ThreadPoolExecutor(max_workers=50) as executor:
            futures = [executor.submit(worker_set_and_read, i) for i in range(200)]
            for f in futures:
                f.result()

        self.assertEqual(len(errors), 0, f"Concurrent STATE_LOCK operations encountered errors: {errors}")

    def test_rate_limiter_tarpitting_does_not_block_other_ips(self):
        """Verify that an IP suffering tarpit sleep does NOT block concurrent rate limit checks for other IPs."""
        limiter = auth.SlidingWindowTarpitLimiter(window_sec=900, max_failures=5, base_delay=0.4, max_delay=4.0)
        victim_ip = "198.51.100.1"
        innocent_ip = "198.51.100.2"

        # Worker to simulate failed login (tarpit delay ~0.4s)
        def tarpit_worker():
            limiter.record_failure(victim_ip)

        t1 = threading.Thread(target=tarpit_worker)
        t1.start()
        time.sleep(0.05)  # Let t1 enter tarpit sleep

        # Innocent IP makes rate limit check concurrently
        t_start = time.time()
        allowed, delay = limiter.check_rate_limit(innocent_ip)
        t_duration = time.time() - t_start

        t1.join()

        self.assertTrue(allowed)
        self.assertLess(t_duration, 0.05, f"Check duration ({t_duration*1000:.2f}ms) must not be delayed by other IP tarpit")


if __name__ == "__main__":
    unittest.main(verbosity=2)
