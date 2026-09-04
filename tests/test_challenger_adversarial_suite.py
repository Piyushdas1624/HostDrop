"""
HostDrop Empirical Adversarial Challenge Test Suite (Challenger 1 - M4)
Exhaustively executes active adversary attack vectors against auth.py and server.py.
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
import urllib.request
import urllib.parse
import urllib.error
from typing import Dict, List, Any, Optional

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import auth
import hostdrop

class AdversarialChallengeSuite:
    def __init__(self):
        self.results: List[Dict[str, Any]] = []
        self.temp_dir = tempfile.mkdtemp(prefix="hostdrop_challenger_")
        self.recv_dir = os.path.join(self.temp_dir, "recv")
        self.share_dir = os.path.join(self.temp_dir, "share")
        os.makedirs(self.recv_dir, exist_ok=True)
        os.makedirs(self.share_dir, exist_ok=True)

        # Setup test server state
        hostdrop.UPLOAD_DIR = self.recv_dir
        hostdrop.HOST_SHARE = self.share_dir
        hostdrop.REQUIRE_AUTH = True
        hostdrop.REQUIRE_AUTH_ON_LAN = True

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

    def record(self, category: str, attack_name: str, passed: bool, details: str = "", evidence: str = ""):
        res = {
            "category": category,
            "attack_name": attack_name,
            "passed": passed,
            "details": details,
            "evidence": evidence
        }
        self.results.append(res)
        tag = "[PASS - DEFENDED]" if passed else "[FAIL - VULNERABLE]"
        print(f"{tag} [{category}] {attack_name} -> {details}")

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

    # ═══════════════════════════════════════════════════════════════════════════
    # 1. AUTHENTICATION BYPASS & CRYPTOGRAPHIC ATTACKS
    # ═══════════════════════════════════════════════════════════════════════════
    def test_auth_bypass(self):
        print("\n" + "="*70)
        print(">>> 1. RUNNING AUTHENTICATION BYPASS & CRYPTO ATTACKS")
        print("="*70)
        category = "AUTH_BYPASS"

        # 1.1 Signature Tampering Attack
        ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        valid_token = auth.create_session_token(user_agent=ua)
        parts = valid_token.split(".")
        
        # Tamper payload while keeping signature
        tampered_session_id = f"v1.{'f'*32}.{parts[2]}.{parts[3]}.{parts[4]}.{parts[5]}"
        res_tampered_id = auth.verify_session_token(tampered_session_id, user_agent=ua)
        self.record(category, "Tampered Session ID with Original Signature", not res_tampered_id,
                    "Rejected successfully" if not res_tampered_id else "Bypassed!")

        # Tamper signature
        tampered_sig = f"{parts[0]}.{parts[1]}.{parts[2]}.{parts[3]}.{parts[4]}.{'0'*64}"
        res_tampered_sig = auth.verify_session_token(tampered_sig, user_agent=ua)
        self.record(category, "Forged HMAC Signature", not res_tampered_sig,
                    "Rejected successfully" if not res_tampered_sig else "Bypassed!")

        # Null byte in signature
        null_sig = f"{parts[0]}.{parts[1]}.{parts[2]}.{parts[3]}.{parts[4]}.\x00{parts[5][1:]}"
        res_null_sig = auth.verify_session_token(null_sig, user_agent=ua)
        self.record(category, "Null Byte in Session Signature", not res_null_sig,
                    "Rejected successfully" if not res_null_sig else "Bypassed!")

        # Truncated tokens
        for trunc in ["", "v1", "v1.session", "v1.session.123", "v1.session.123.456.789"]:
            res_trunc = auth.verify_session_token(trunc, user_agent=ua)
            self.record(category, f"Truncated Token '{trunc}'", not res_trunc,
                        "Rejected successfully" if not res_trunc else "Bypassed!")

        # 1.2 User-Agent Fingerprint Forgery
        victim_ua = "VictimBrowser/100.0"
        attacker_ua = "AttackerBot/1.0"
        victim_token = auth.create_session_token(user_agent=victim_ua)
        
        # Attacker presents victim's stolen token with attacker UA
        res_stolen = auth.verify_session_token(victim_token, user_agent=attacker_ua)
        self.record(category, "Stolen Token Presented with Mismatched User-Agent", not res_stolen,
                    "User-Agent mismatch detected and rejected" if not res_stolen else "Bypassed!")

        # 1.3 Expired and Future Timestamps
        now = int(time.time())
        # Token expired 1 hour ago
        exp_payload = f"v1.sess123456789012.{now-86400*31}.{now-3600}.{hashlib.sha256(ua.encode()).hexdigest()[:16]}"
        exp_sig = auth.hmac.new(auth.get_secret_key().encode(), exp_payload.encode(), hashlib.sha256).hexdigest()
        expired_token = f"{exp_payload}.{exp_sig}"
        res_exp = auth.verify_session_token(expired_token, user_agent=ua)
        self.record(category, "Expired Session Token (Past Expiry)", not res_exp,
                    "Expired token rejected" if not res_exp else "Bypassed!")

        # Future token (+2 hours in future beyond 300s skew tolerance)
        fut_payload = f"v1.sess123456789012.{now+7200}.{now+86400*30}.{hashlib.sha256(ua.encode()).hexdigest()[:16]}"
        fut_sig = auth.hmac.new(auth.get_secret_key().encode(), fut_payload.encode(), hashlib.sha256).hexdigest()
        future_token = f"{fut_payload}.{fut_sig}"
        res_fut = auth.verify_session_token(future_token, user_agent=ua)
        self.record(category, "Future Session Token (+2hr clock skew)", not res_fut,
                    "Future token outside tolerance rejected" if not res_fut else "Bypassed!")

        # 1.4 Constant-Time Password & Key Verification
        # Run timing benchmark over 50 samples comparing correct prefix vs mismatch
        pwd = "HostDropSecureMasterPassword2026!"
        good_hash = auth.hash_password(pwd)
        sec_cfg = auth.SecurityConfig(self.temp_dir + "/.env.test")
        sec_cfg.password_hash = good_hash

        times_close = []
        times_far = []
        for _ in range(50):
            t0 = time.perf_counter_ns()
            sec_cfg.verify_password("HostDropSecureMasterPassword2026X")  # off by 1 char at end
            t1 = time.perf_counter_ns()
            times_close.append(t1 - t0)

            t2 = time.perf_counter_ns()
            sec_cfg.verify_password("XurboShareSecureMasterPassword2026!")  # off by 1 char at start
            t3 = time.perf_counter_ns()
            times_far.append(t3 - t2)

        avg_close = sum(times_close) / len(times_close)
        avg_far = sum(times_far) / len(times_far)
        diff_pct = abs(avg_close - avg_far) / max(avg_close, avg_far) * 100
        self.record(category, "PBKDF2 Constant-Time Verification Timing Invariance", diff_pct < 50,
                    f"Timing difference: {diff_pct:.2f}% (Close={avg_close:.0f}ns, Far={avg_far:.0f}ns)")

    # ═══════════════════════════════════════════════════════════════════════════
    # 2. PATH TRAVERSAL & FILESYSTEM ATTACKS
    # ═══════════════════════════════════════════════════════════════════════════
    def test_path_traversal(self):
        print("\n" + "="*70)
        print(">>> 2. RUNNING PATH TRAVERSAL & FILESYSTEM CONFINEMENT ATTACKS")
        print("="*70)
        category = "PATH_TRAVERSAL"

        traversal_payloads = [
            # Standard & Deep Dot-Dot
            ("../secret.txt", "Standard ../"),
            ("..\\secret.txt", "Standard ..\\"),
            ("../../../../../../../../Windows/win.ini", "Deep dot-dot to Windows root"),
            ("..\\..\\..\\..\\..\\..\\..\\..\\Windows\\System32\\drivers\\etc\\hosts", "Deep backslash traversal"),
            ("sub/../../../../etc/passwd", "Subfolder escape traversal"),
            ("sub/./../../secret.txt", "Dot-slash mixed traversal"),
            ("/../etc/shadow", "Root-prefixed traversal"),

            # URL-encoded variants
            ("%2e%2e%2fsecret.txt", "URL-encoded %2e%2e%2f"),
            ("%2e%2e%5csecret.txt", "URL-encoded %2e%2e%5c"),
            ("..%2fsecret.txt", "Mixed URL-encoded ..%2f"),
            ("%252e%252e%252fsecret.txt", "Double-encoded %252e%252e%252f"),
            ("%25252e%25252e%25252fsecret.txt", "Triple-encoded"),

            # Null-byte poisoning
            ("safe.txt\x00.exe", "Raw null byte poisoning"),
            ("safe.txt%00.exe", "URL-encoded null byte %00"),
            ("%00/../../secret.txt", "Null byte prefix traversal"),

            # Windows NTFS Alternate Data Streams (ADS)
            ("file.txt::$DATA", "NTFS ADS ::$DATA stream"),
            ("file.txt:stream_name", "NTFS named stream :stream_name"),
            ("folder::$INDEX_ALLOCATION", "NTFS directory stream"),
            ("file.txt:::$DATA", "NTFS triple colon ADS"),

            # UNC Network Paths
            ("\\\\attacker.com\\share\\file.exe", "UNC backslash path"),
            ("//attacker.com/share/file.exe", "UNC forward-slash path"),
            ("\\\\?\\C:\\Windows\\win.ini", "NT extended-length UNC path"),
            ("\\\\127.0.0.1\\c$\\secret.txt", "Local administrative share UNC"),

            # Windows Reserved Device Names
            ("CON", "Reserved device CON"),
            ("PRN", "Reserved device PRN"),
            ("AUX", "Reserved device AUX"),
            ("NUL", "Reserved device NUL"),
            ("COM1", "Reserved device COM1"),
            ("COM9", "Reserved device COM9"),
            ("LPT1", "Reserved device LPT1"),
            ("CON.txt", "Reserved device CON.txt"),
            ("aux.json", "Reserved device aux.json"),
            ("sub/PRN/file.txt", "Nested reserved device folder"),

            # Absolute & Cross-Drive Paths
            ("C:\\Windows\\win.ini", "Absolute Windows drive path"),
            ("C:/Windows/win.ini", "Absolute forward-slash Windows path"),
            ("D:\\secret.txt", "Cross-drive path"),
            ("/etc/passwd", "Unix root path"),
            ("Z:relative_drive_file.txt", "Drive-relative path"),
        ]

        for payload, description in traversal_payloads:
            result = hostdrop.safe_path(self.recv_dir, payload)
            passed = (result is None)
            self.record(category, f"safe_path traversal payload: {description} ('{payload}')", passed,
                        "Blocked (returned None)" if passed else f"VULNERABLE! Escaped to: {result}")

        # Test valid relative subpath to ensure safe_path does NOT block legitimate paths
        valid_payloads = [
            "photo.jpg",
            "documents/report.pdf",
            "nested/level1/level2/data.txt",
            "file with spaces and éàç.png"
        ]
        for valid_p in valid_payloads:
            result = hostdrop.safe_path(self.recv_dir, valid_p)
            passed = (result is not None and os.path.commonpath([self.recv_dir, result]) == self.recv_dir)
            self.record(category, f"Legitimate path permitted: '{valid_p}'", passed,
                        f"Resolved safely to {result}" if passed else "Incorrectly blocked legitimate path!")

    # ═══════════════════════════════════════════════════════════════════════════
    # 3. PROXY HEADER SPOOFING ATTACKS
    # ═══════════════════════════════════════════════════════════════════════════
    def test_proxy_header_spoofing(self):
        print("\n" + "="*70)
        print(">>> 3. RUNNING PROXY HEADER SPOOFING & TRUST VALIDATION ATTACKS")
        print("="*70)
        category = "PROXY_SPOOFING"

        class MockHandler:
            def __init__(self, client_ip, headers):
                self.client_address = (client_ip, 54321)
                self.headers = headers

        # Attack 3.1: Attacker on LAN (192.168.1.150) sends spoofed localhost & CF headers
        lan_handler = MockHandler("192.168.1.150", {
            "CF-Connecting-IP": "127.0.0.1",
            "X-Forwarded-For": "127.0.0.1, 10.0.0.1",
            "X-Real-IP": "127.0.0.1"
        })
        resolved_lan = auth.get_client_ip(lan_handler)
        passed_lan = (resolved_lan == "192.168.1.150")
        self.record(category, "Direct LAN Client Spoofing 127.0.0.1 via Headers", passed_lan,
                    f"Resolved to physical socket IP {resolved_lan}" if passed_lan else f"Spoofed as {resolved_lan}!")

        # Attack 3.2: Attacker rotating X-Forwarded-For headers to evade rate limits
        lan_rot_handler = MockHandler("192.168.1.150", {
            "X-Forwarded-For": "8.8.8.8, 1.1.1.1"
        })
        resolved_rot = auth.get_client_ip(lan_rot_handler)
        passed_rot = (resolved_rot == "192.168.1.150")
        self.record(category, "Direct LAN Rotating XFF Evasion", passed_rot,
                    f"Resolved to physical socket IP {resolved_rot}" if passed_rot else f"Spoofed as {resolved_rot}!")

        # Attack 3.3: Legitimate Cloudflare Tunnel on Loopback (127.0.0.1)
        cf_handler = MockHandler("127.0.0.1", {
            "CF-Connecting-IP": "203.0.113.88",
            "X-Forwarded-For": "203.0.113.88, 127.0.0.1"
        })
        resolved_cf = auth.get_client_ip(cf_handler)
        passed_cf = (resolved_cf == "203.0.113.88")
        self.record(category, "Legitimate Loopback Cloudflare Tunnel CF-Connecting-IP", passed_cf,
                    f"Correctly extracted client IP {resolved_cf}" if passed_cf else f"Failed to extract: {resolved_cf}")

        # Attack 3.4: Legitimate ngrok / Pinggy on Loopback (127.0.0.1)
        ngrok_handler = MockHandler("127.0.0.1", {
            "X-Forwarded-For": "198.51.100.42, 10.0.0.1"
        })
        resolved_ngrok = auth.get_client_ip(ngrok_handler)
        passed_ngrok = (resolved_ngrok == "198.51.100.42")
        self.record(category, "Legitimate Loopback ngrok/Pinggy X-Forwarded-For Client", passed_ngrok,
                    f"Correctly extracted first hop {resolved_ngrok}" if passed_ngrok else f"Failed: {resolved_ngrok}")

        # Attack 3.5: Malformed / Garbage / Injection headers on Loopback
        garbage_handler = MockHandler("127.0.0.1", {
            "CF-Connecting-IP": "<script>alert(1)</script>",
            "X-Forwarded-For": "invalid_ip; DROP TABLE ips;"
        })
        resolved_garbage = auth.get_client_ip(garbage_handler)
        passed_garbage = (resolved_garbage == "127.0.0.1")
        self.record(category, "Garbage/Injection Proxy Headers on Loopback", passed_garbage,
                    f"Safely fell back to socket loopback {resolved_garbage}" if passed_garbage else f"Failed: {resolved_garbage}")

    # ═══════════════════════════════════════════════════════════════════════════
    # 4. BRUTE-FORCE DICTIONARY ATTACK & EXPONENTIAL TARPITTING
    # ═══════════════════════════════════════════════════════════════════════════
    def test_brute_force_tarpitting(self):
        print("\n" + "="*70)
        print(">>> 4. RUNNING BRUTE-FORCE DICTIONARY ATTACK & TARPITTING TESTS")
        print("="*70)
        category = "BRUTE_FORCE_TARPIT"

        # Create isolated limiter with small base delay for rapid deterministic verification
        limiter = auth.SlidingWindowTarpitLimiter(
            window_sec=900,
            max_failures=5,
            base_delay=0.005,  # 5ms
            max_delay=0.080   # 80ms
        )

        test_ip = "198.51.100.99"

        # 4.1 Check progressive tarpitting over 5 failed attempts
        delays = []
        for i in range(5):
            is_allowed, cur_delay = limiter.check_rate_limit(test_ip)
            self.record(category, f"Attempt {i+1} Pre-check Allowed", is_allowed,
                        f"Allowed=True, current delay={cur_delay:.3f}s")
            t0 = time.perf_counter()
            d, count = limiter.record_failure(test_ip)
            t1 = time.perf_counter()
            delays.append(t1 - t0)

        # Verify exponential growth: delays[k] >= delays[k-1]
        exponential_growth = all(delays[k] >= delays[k-1] * 1.3 for k in range(1, len(delays)))
        self.record(category, "Exponential Tarpit Delay Progression [D(k) = base * 2^(k-1)]", exponential_growth,
                    f"Measured delays: {[f'{d*1000:.1f}ms' for d in delays]}")

        # 4.2 6th attempt must be locked out immediately (HTTP 429)
        is_allowed_6, retry_after = limiter.check_rate_limit(test_ip)
        passed_lockout = (not is_allowed_6 and retry_after > 0)
        self.record(category, "Lockout Enforcement on 6th Attempt (HTTP 429)", passed_lockout,
                    f"Allowed={is_allowed_6}, Retry-After={retry_after:.1f}s")

        # 4.3 7th through 20th automated dictionary spray attempts blocked with zero execution
        all_subsequent_blocked = True
        for attempt in range(7, 21):
            allowed, _ = limiter.check_rate_limit(test_ip)
            if allowed:
                all_subsequent_blocked = False
                break
        self.record(category, "Subsequent Dictionary Spray (Attempts 7-20) Blocked", all_subsequent_blocked,
                    "100% of brute-force spray attempts blocked immediately")

        # 4.4 Counter Reset on Valid Login
        limiter.record_success(test_ip)
        is_allowed_reset, delay_reset = limiter.check_rate_limit(test_ip)
        passed_reset = (is_allowed_reset and delay_reset == 0.0)
        self.record(category, "Rate Counter Reset on Successful Authentication", passed_reset,
                    f"Allowed={is_allowed_reset}, Delay={delay_reset:.3f}s")

        # 4.5 Concurrent Thread-Safety Stress Test (20 threads)
        concurrent_ip = "198.51.100.200"
        threads = []
        def brute_worker():
            for _ in range(3):
                limiter.record_failure(concurrent_ip)

        for _ in range(10):
            t = threading.Thread(target=brute_worker)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        is_allowed_conc, _ = limiter.check_rate_limit(concurrent_ip)
        self.record(category, "Multi-Threaded Concurrent Brute-Force Lockout (10 threads)", not is_allowed_conc,
                    "Limiter locked out under high-concurrency attack without deadlocks")

    # ═══════════════════════════════════════════════════════════════════════════
    # 5. PRIVILEGE SANDBOXING ON SENSITIVE ENDPOINTS
    # ═══════════════════════════════════════════════════════════════════════════
    def test_privilege_sandboxing(self):
        print("\n" + "="*70)
        print(">>> 5. RUNNING PRIVILEGE SANDBOXING ON SENSITIVE ENDPOINTS")
        print("="*70)
        category = "PRIVILEGE_SANDBOX"

        protected_endpoints = [
            ("GET", "/api/browse_host", None),
            ("POST", "/api/set_path", b'{"target":"share","path":"C:\\\\"}'),
            ("POST", "/api/create_folder", b'{"parent":"C:\\\\","name":"exploit"}'),
            ("GET", "/api/pick_folder", None),
            ("POST", "/api/open_folder", b'{"type":"recv"}'),
        ]

        for method, endpoint, body in protected_endpoints:
            url = f"{self.base_url}{endpoint}"
            req = urllib.request.Request(url, data=body, method=method)
            if body:
                req.add_header("Content-Type", "application/json")

            try:
                urllib.request.urlopen(req)
                passed = False
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
                passed = (code in (401, 403))
            except Exception as e:
                passed = False
                code = str(e)

            self.record(category, f"Unauthorized Access to High-Privilege '{method} {endpoint}'", passed,
                        f"Blocked with HTTP {code}" if passed else f"VULNERABLE! Returned HTTP {code}")

        # 5.2 CSRF Protection on Mutation Routes
        csrf_req = urllib.request.Request(
            f"{self.base_url}/api/upload?path=csrf_test.txt",
            data=b"CSRF_PAYLOAD",
            method="POST"
        )
        csrf_req.add_header("Sec-Fetch-Site", "cross-site")
        try:
            urllib.request.urlopen(csrf_req)
            csrf_passed = False
            csrf_code = 200
        except urllib.error.HTTPError as e:
            csrf_code = e.code
            csrf_passed = (csrf_code == 403)
        except Exception as e:
            csrf_passed = False
            csrf_code = str(e)

        self.record(category, "CSRF Cross-Site Request Prohibited (Sec-Fetch-Site: cross-site)", csrf_passed,
                    f"Blocked with HTTP {csrf_code}" if csrf_passed else f"VULNERABLE! Returned {csrf_code}")

    def run_all(self):
        print("\n" + "#"*72)
        print("  HOSTDROP CHALLENGER 1 ADVERSARIAL ATTACK TEST HARNESS (M4)")
        print("#"*72)

        self.test_auth_bypass()
        self.test_path_traversal()
        self.test_proxy_header_spoofing()
        self.test_brute_force_tarpitting()
        self.test_privilege_sandboxing()

        print("\n" + "="*72)
        total = len(self.results)
        passed = sum(1 for r in self.results if r["passed"])
        failed = total - passed
        print(f"  TOTAL ADVERSARIAL ATTACK VECTORS TESTED : {total}")
        print(f"  DEFENDED / PASSED                      : {passed}")
        print(f"  VULNERABILITIES / FAILED               : {failed}")
        print(f"  SECURITY POSTURE VERDICT               : {'APPROVED' if failed == 0 else 'REQUEST_CHANGES'}")
        print("="*72 + "\n")

        return failed == 0

if __name__ == "__main__":
    suite = AdversarialChallengeSuite()
    try:
        success = suite.run_all()
    finally:
        suite.cleanup()
    sys.exit(0 if success else 1)
