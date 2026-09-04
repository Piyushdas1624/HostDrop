"""
HostDrop Novel Adversarial Attack Vectors (Challenger 1 - Iteration 2 Deep Stress Suite)
Tests advanced edge cases across:
1. Advanced path traversal (quad/penta-decoding, multi-slash, DOS devices, ADS variations, root escapes)
2. Advanced proxy header trust (IPv6, multi-header collisions, octal/integer IPs, whitespace padding, case-insensitivity)
3. Advanced auth bypass & crypto stress (type confusion, delimiter injection, single-bit flips, empty UA vs spoofed UA)
4. High-concurrency rate limit race conditions & cross-IP DoS isolation
5. Memory/Resource DoS bounds (pre-flight checks, depth ceilings, zip limits)
"""

import os
import sys
import io
import time
import json
import socket
import secrets
import hashlib
import tempfile
import threading
import concurrent.futures
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import auth
import hostdrop

class NovelAdversarialSuite:
    def __init__(self):
        self.results = []
        self.temp_dir = tempfile.mkdtemp(prefix="hostdrop_novel_adv_")
        self.recv_dir = os.path.join(self.temp_dir, "recv")
        self.share_dir = os.path.join(self.temp_dir, "share")
        os.makedirs(self.recv_dir, exist_ok=True)
        os.makedirs(self.share_dir, exist_ok=True)

        hostdrop.UPLOAD_DIR = self.recv_dir
        hostdrop.HOST_SHARE = self.share_dir
        hostdrop.REQUIRE_AUTH = True

        # Find free ephemeral port
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        self.port = s.getsockname()[1]
        s.close()

        hostdrop.SERVER_PORT = self.port
        self.base_url = f"http://127.0.0.1:{self.port}"

        self.httpd = hostdrop.create_server("127.0.0.1", self.port)
        self.server_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.server_thread.start()
        time.sleep(0.3)

    def record(self, category: str, test_name: str, passed: bool, details: str = ""):
        self.results.append({
            "category": category,
            "test_name": test_name,
            "passed": passed,
            "details": details
        })
        status = "[PASS - DEFENDED]" if passed else "[FAIL - VULNERABLE]"
        print(f"{status} [{category}] {test_name} -> {details}")

    def cleanup(self):
        try:
            self.httpd.shutdown()
            self.httpd.server_close()
        except Exception:
            pass
        try:
            import shutil
            shutil.rmtree(self.temp_dir, ignore_errors=True)
        except Exception:
            pass

    # ─────────────────────────────────────────────────────────────────────────
    # 1. NOVEL PATH TRAVERSAL & NORMALIZATION VECTORS
    # ─────────────────────────────────────────────────────────────────────────
    def test_novel_path_traversal(self):
        print("\n" + "="*70)
        print(">>> 1. NOVEL PATH TRAVERSAL & NORMALIZATION ATTACK VECTORS")
        print("="*70)
        category = "ADV_PATH_TRAVERSAL"

        traversal_attack_vectors = [
            # 1. Multi-level Nested URL Encodings
            ("%2525252e%2525252e%2525252fsecret.txt", "Quad-encoded traversal (%2525252e)"),
            ("%252525252e%252525252e%252525252fsecret.txt", "Penta-encoded traversal (%252525252e)"),
            
            # 2. Mixed Slash & Redundant Slash Traversal
            ("..//..//secret.txt", "Double forward-slash traversal (..//..//)"),
            ("..\\\\..\\\\secret.txt", "Double backslash traversal (..\\\\..\\\\)"),
            ("..\\/..\\/secret.txt", "Mixed backslash-slash traversal (..\\/..\\/)"),
            ("..//\\//..//secret.txt", "Chaotic multi-slash traversal"),
            ("../secret.txt.", "Traversal with trailing dot"),
            ("..\\secret.txt. . . ", "Backslash traversal with trailing dots and spaces"),
            ("   ../secret.txt   ", "Leading/trailing whitespace wrapped traversal"),
            ("\t..\\secret.txt\n", "Tab/Newline whitespace wrapped traversal"),
            
            # 3. Windows Reserved DOS Devices (case variations, multi-ext, ADS)
            ("CON.tar.gz", "Multi-extension DOS device (CON.tar.gz)"),
            ("nul.exe", "Lowercase DOS device (nul.exe)"),
            ("com1.txt", "DOS device with extension (com1.txt)"),
            ("lpt9.dat", "LPT9 device (lpt9.dat)"),
            ("aux.json", "AUX device (aux.json)"),
            ("sub/dir/con/payload.txt", "DOS device as directory name"),
            ("CON::$DATA", "DOS device with NTFS ADS"),
            ("NUL:stream", "DOS device with named stream"),
            ("com4.longextension", "COM device with long extension"),
            
            # 4. Alternate Data Stream Variations
            ("photo.jpg::$INDEX_ALLOCATION", "Directory stream on file"),
            ("photo.jpg:evil.exe:$DATA", "Named ADS with data stream"),
            ("photo.jpg%3a%3a$DATA", "URL-encoded ADS colon (%3a)"),
            ("photo.jpg%253a%253a$DATA", "Double-encoded ADS colon (%253a)"),
            
            # 5. Drive Colon Injections
            ("C:secret.txt", "Drive-relative colon C:secret.txt"),
            ("c:secret.txt", "Lowercase drive-relative c:secret.txt"),
            ("subfolder:stream", "Relative subfolder colon stream"),
            
            # 6. Absolute paths with drive letters & root indicators
            ("C:/secret.txt", "Absolute forward slash C:/"),
            ("C:\\secret.txt", "Absolute backslash C:\\"),
            ("\\secret.txt", "Root-relative backslash \\secret.txt"),
            ("/secret.txt", "Root-relative slash /secret.txt"),
            ("\\\\?\\C:\\secret.txt", "Extended NT UNC prefix"),
            ("\\\\.\\C:\\secret.txt", "Device namespace UNC prefix"),
        ]

        for payload, desc in traversal_attack_vectors:
            res = hostdrop.safe_path(self.recv_dir, payload)
            passed = (res is None)
            self.record(category, f"Attack Vector: {desc} ['{payload}']", passed,
                        "Successfully blocked (None)" if passed else f"VULNERABILITY: Escaped to {res}")

        # Ensure valid nested subpaths (including non-traversing filenames) are safely confined
        confined_cases = [
            ("valid.txt", "Simple valid file"),
            ("folder/valid.txt", "Subfolder valid file"),
            ("folder/sub2/sub3/doc.pdf", "Deep subfolder file"),
            ("my file (1) [final] #2.txt", "Complex valid filename with symbols"),
            ("secret.txt.", "File with trailing dot (confined)"),
            ("secret.txt. . . ", "File with trailing dots and spaces (confined)"),
            ("%c0%afsecret.txt", "Overlong UTF-8 token treated as literal filename inside sandbox"),
            ("%e0%80%afsecret.txt", "3-byte overlong token treated as literal filename inside sandbox"),
        ]
        for payload, desc in confined_cases:
            res = hostdrop.safe_path(self.recv_dir, payload)
            passed = (res is not None and os.path.commonpath([self.recv_dir, res]) == self.recv_dir)
            self.record(category, f"Confined Case: {desc}", passed,
                        f"Resolved safely to {res}" if passed else "Failed: Not confined in sandbox")

    # ─────────────────────────────────────────────────────────────────────────
    # 2. NOVEL PROXY HEADER SPOOFING & TRUST RESOLUTION
    # ─────────────────────────────────────────────────────────────────────────
    def test_novel_proxy_trust(self):
        print("\n" + "="*70)
        print(">>> 2. NOVEL PROXY HEADER SPOOFING & TRUST RESOLUTION ATTACKS")
        print("="*70)
        category = "ADV_PROXY_TRUST"

        class DummyHandler:
            def __init__(self, ip, headers):
                self.client_address = (ip, 12345)
                self.headers = headers

        # 2.1 IPv6 and loopback subnet variants (127.0.0.0/8)
        for loopback_ip in ["127.0.0.1", "127.0.0.2", "::1", "localhost"]:
            handler = DummyHandler(loopback_ip, {"CF-Connecting-IP": "198.51.100.77"})
            resolved = auth.get_client_ip(handler)
            passed = (resolved == "198.51.100.77")
            self.record(category, f"Loopback resolution for '{loopback_ip}'", passed,
                        f"Resolved correctly to {resolved}" if passed else f"Failed: {resolved}")

        # 2.2 IPv6 Client IP in CF-Connecting-IP behind loopback
        ipv6_client = "2001:db8:85a3::8a2e:370:7334"
        handler = DummyHandler("127.0.0.1", {"CF-Connecting-IP": ipv6_client})
        resolved = auth.get_client_ip(handler)
        passed = (resolved == ipv6_client)
        self.record(category, "Valid IPv6 in CF-Connecting-IP", passed,
                    f"Resolved to {resolved}" if passed else f"Failed: {resolved}")

        # 2.3 X-Forwarded-For with multiple hops, some private, some loopback, extracting real client
        handler_xff = DummyHandler("127.0.0.1", {
            "X-Forwarded-For": "127.0.0.1, 198.51.100.55, 10.0.0.1"
        })
        resolved_xff = auth.get_client_ip(handler_xff)
        passed_xff = (resolved_xff == "198.51.100.55")
        self.record(category, "XFF Loopback skip and first external IP extraction", passed_xff,
                    f"Extracted {resolved_xff}" if passed_xff else f"Failed: {resolved_xff}")

        # 2.4 External LAN connection trying to inject CF-Connecting-IP
        handler_lan = DummyHandler("10.0.0.50", {
            "CF-Connecting-IP": "1.2.3.4",
            "X-Forwarded-For": "5.6.7.8",
            "X-Real-IP": "9.10.11.12"
        })
        resolved_lan = auth.get_client_ip(handler_lan)
        passed_lan = (resolved_lan == "10.0.0.50")
        self.record(category, "External LAN socket ignores all spoofed headers", passed_lan,
                    f"Locked to physical socket {resolved_lan}" if passed_lan else f"Spoofed: {resolved_lan}")

        # 2.5 Malformed IPs (octal, letters, invalid ranges, SQLi payloads)
        for malformed in ["999.999.999.999", "127.0.0.1.1", "abc.def.ghi.jkl", "1.2.3.4; DROP TABLE", ""]:
            handler_bad = DummyHandler("127.0.0.1", {"CF-Connecting-IP": malformed})
            resolved_bad = auth.get_client_ip(handler_bad)
            passed_bad = (resolved_bad == "127.0.0.1")
            self.record(category, f"Malformed IP fallback for '{malformed}'", passed_bad,
                        f"Safely fell back to socket {resolved_bad}" if passed_bad else f"Accepted invalid: {resolved_bad}")

    # ─────────────────────────────────────────────────────────────────────────
    # 3. NOVEL AUTHENTICATION BYPASS & CRYPTO TYPE CONFUSION
    # ─────────────────────────────────────────────────────────────────────────
    def test_novel_auth_and_crypto(self):
        print("\n" + "="*70)
        print(">>> 3. NOVEL AUTHENTICATION BYPASS & CRYPTO TYPE CONFUSION")
        print("="*70)
        category = "ADV_AUTH_CRYPTO"

        # 3.1 Type confusion in verify_password and verify_access_key
        bad_types = [None, 12345, True, False, [], {}, 3.14, b"bytes_pw"]
        all_bad_pw_rejected = True
        for bt in bad_types:
            try:
                res = auth.verify_password(bt)
                if res is True:
                    all_bad_pw_rejected = False
            except Exception:
                pass
        self.record(category, "Type confusion rejection in verify_password", all_bad_pw_rejected,
                    "All non-string/malformed types safely rejected")

        all_bad_key_rejected = True
        for bt in bad_types:
            try:
                res = auth.verify_access_key(bt)
                if res is True:
                    all_bad_key_rejected = False
            except Exception:
                pass
        self.record(category, "Type confusion rejection in verify_access_key", all_bad_key_rejected,
                    "All non-string/malformed keys safely rejected")

        # 3.2 Single-Bit Signature Corruption (Fuzzing 16 bits across HMAC signature)
        ua = "TestBrowser/2.0"
        valid_tok = auth.create_session_token(user_agent=ua)
        parts = valid_tok.split(".")
        sig = parts[5]
        sig_bytes = list(sig)
        
        all_corrupted_rejected = True
        for i in range(min(16, len(sig_bytes))):
            corrupted = sig_bytes.copy()
            corrupted[i] = "0" if corrupted[i] != "0" else "1"
            bad_tok = f"{'.'.join(parts[:5])}.{''.join(corrupted)}"
            if auth.verify_session_token(bad_tok, user_agent=ua):
                all_corrupted_rejected = False
                break
        self.record(category, "Single-Bit Signature Flipping Fuzzing", all_corrupted_rejected,
                    "100% of bit-flipped signatures rejected")

        # 3.3 Delimiter Injection in Token Components
        injected_tok = f"v1.sess.extra.dots.{int(time.time())}.{int(time.time())+86400}.{parts[4]}.{sig}"
        res_inj = auth.verify_session_token(injected_tok, user_agent=ua)
        self.record(category, "Delimiter Injection (extra dots) in Session Token", not res_inj,
                    "Malformed segment count rejected")

    # ─────────────────────────────────────────────────────────────────────────
    # 4. HIGH-CONCURRENCY RATE LIMIT RACE CONDITIONS & MULTI-IP ISOLATION
    # ─────────────────────────────────────────────────────────────────────────
    def test_concurrency_and_rate_limit(self):
        print("\n" + "="*70)
        print(">>> 4. HIGH-CONCURRENCY RATE LIMIT RACE CONDITIONS & MULTI-IP ISOLATION")
        print("="*70)
        category = "ADV_RATE_LIMIT_CONCURRENCY"

        limiter = auth.SlidingWindowTarpitLimiter(
            window_sec=900,
            max_failures=5,
            base_delay=0.001,  # 1ms
            max_delay=0.010   # 10ms
        )

        # 4.1 Race Condition Spray: 30 concurrent threads attacking from single IP
        attacker_ip = "198.51.100.123"
        success_count = 0
        blocked_count = 0
        lock = threading.Lock()

        def attack_worker():
            nonlocal success_count, blocked_count
            allowed, _ = limiter.check_rate_limit(attacker_ip)
            if allowed:
                limiter.record_failure(attacker_ip)
                with lock:
                    success_count += 1
            else:
                with lock:
                    blocked_count += 1

        threads = [threading.Thread(target=attack_worker) for _ in range(30)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        is_locked, retry = limiter.check_rate_limit(attacker_ip)
        passed_race = (not is_locked and retry > 0)
        self.record(category, "Concurrent 30-Thread Burst Race Condition Defense", passed_race,
                    f"Attacker locked out. Allowed through gate={success_count}, Blocked={blocked_count}, Locked={not is_locked}")

        # 4.2 Cross-Tenant Isolation: Ensure innocent user IP is unaffected by attacker lockout
        innocent_ip = "203.0.113.1"
        innocent_allowed, _ = limiter.check_rate_limit(innocent_ip)
        self.record(category, "Cross-Tenant Isolation (Innocent IP not affected by Attacker lockout)", innocent_allowed,
                    f"Innocent IP Allowed={innocent_allowed}")

    # ─────────────────────────────────────────────────────────────────────────
    # 5. RESOURCE LIMITS & DOS BOUNDS VERIFICATION
    # ─────────────────────────────────────────────────────────────────────────
    def test_resource_dos_bounds(self):
        print("\n" + "="*70)
        print(">>> 5. RESOURCE LIMITS & DOS BOUNDS VERIFICATION")
        print("="*70)
        category = "ADV_DOS_BOUNDS"

        # 5.1 Pre-flight check constants
        self.record(category, "MAX_UPLOAD_SIZE constant bounded (50GB)", hostdrop.MAX_UPLOAD_SIZE == 50 * 1024 * 1024 * 1024,
                    f"Value: {hostdrop.MAX_UPLOAD_SIZE} bytes")
        self.record(category, "MAX_ZIP_SIZE constant bounded (10GB)", hostdrop.MAX_ZIP_SIZE == 10 * 1024 * 1024 * 1024,
                    f"Value: {hostdrop.MAX_ZIP_SIZE} bytes")
        self.record(category, "MAX_ZIP_FILES bounded (10,000)", hostdrop.MAX_ZIP_FILES == 10_000,
                    f"Value: {hostdrop.MAX_ZIP_FILES} entries")
        self.record(category, "MAX_ZIP_DEPTH bounded (25)", hostdrop.MAX_ZIP_DEPTH == 25,
                    f"Value: {hostdrop.MAX_ZIP_DEPTH} levels")

        # 5.2 Test /api/zip non-existent subpath returns 404 cleanly without crashing
        try:
            req = urllib.request.urlopen(f"{self.base_url}/api/zip?path=nonexistent_subfolder_xyz123")
            zip_passed = False
        except urllib.error.HTTPError as e:
            zip_passed = (e.code == 404)
        except Exception:
            zip_passed = False

        self.record(category, "/api/zip Non-Existent Directory Rejection", zip_passed,
                    "Clean HTTP 404 returned without unhandled exception")

    def run_all(self):
        print("\n" + "#"*72)
        print("  HOSTDROP CHALLENGER 1 NOVEL ADVERSARIAL STRESS SUITE")
        print("#"*72)

        self.test_novel_path_traversal()
        self.test_novel_proxy_trust()
        self.test_novel_auth_and_crypto()
        self.test_concurrency_and_rate_limit()
        self.test_resource_dos_bounds()

        print("\n" + "="*72)
        total = len(self.results)
        passed = sum(1 for r in self.results if r["passed"])
        failed = total - passed
        print(f"  TOTAL NOVEL ADVERSARIAL ATTACK VECTORS : {total}")
        print(f"  DEFENDED / PASSED                     : {passed}")
        print(f"  VULNERABILITIES / FAILED              : {failed}")
        print(f"  NOVEL STRESS VERDICT                  : {'APPROVED' if failed == 0 else 'REQUEST_CHANGES'}")
        print("="*72 + "\n")
        return failed == 0

if __name__ == "__main__":
    suite = NovelAdversarialSuite()
    try:
        success = suite.run_all()
    finally:
        suite.cleanup()
    sys.exit(0 if success else 1)
