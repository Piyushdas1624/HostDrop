import os
import sys
import json
import time
import hmac
import hashlib
import secrets
import tempfile
import threading
import urllib.request
import urllib.parse
import urllib.error

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import auth
import hostdrop

def run_independent_victory_audit():
    print("=" * 65)
    print(" INDEPENDENT VICTORY AUDITOR FORENSIC & BEHAVIORAL VERIFICATION ")
    print("=" * 65)
    
    passed_checks = 0
    failed_checks = 0

    def assert_check(name, condition, details=""):
        nonlocal passed_checks, failed_checks
        if condition:
            passed_checks += 1
            print(f"  [PASS] {name} - {details}")
        else:
            failed_checks += 1
            print(f"  [FAIL] {name} - {details}")

    # 1. PBKDF2 Hashing and Verification
    pwd = "AuditSecretPassphrase_2026!"
    hashed = auth.hash_password(pwd)
    parts = hashed.split("$")
    assert_check("PBKDF2 Scheme", parts[0] == "pbkdf2_sha256", f"Scheme={parts[0]}")
    assert_check("PBKDF2 Iterations", int(parts[1]) == 600000, f"Iterations={parts[1]}")
    assert_check("PBKDF2 Salt Entropy", len(bytes.fromhex(parts[2])) == 16, "Salt is 16 bytes")
    assert_check("PBKDF2 DK Length", len(bytes.fromhex(parts[3])) == 32, "DK is 32 bytes (256 bits)")

    # 2. Constant-time Verification
    sec_cfg = auth.SecurityConfig()
    sec_cfg.password_hash = hashed
    assert_check("Verify Correct Password", sec_cfg.verify_password(pwd), "Valid password returns True")
    assert_check("Reject Incorrect Password", not sec_cfg.verify_password(pwd + "X"), "Wrong password returns False")
    assert_check("Reject Empty Password", not sec_cfg.verify_password(""), "Empty password returns False")

    # 3. Persistent Signed Stateless Tokens Across Reboots
    secret = secrets.token_hex(32)
    sm1 = auth.SessionManager(secret)
    ua = "AuditorBrowser/1.0"
    token = sm1.create_token(user_agent=ua)
    assert_check("Token Structure", len(token.split(".")) == 6, f"6 segments in token: {token[:20]}...")
    assert_check("Token Validation on Active Instance", sm1.verify_token(token, user_agent=ua), "Valid signature & UA")

    # Simulate server reboot with new SessionManager instance
    sm2 = auth.SessionManager(secret)
    assert_check("Stateless Session Survives Reboot", sm2.verify_token(token, user_agent=ua), "Valid on new manager instance")
    assert_check("Reject Mismatched User-Agent", not sm2.verify_token(token, user_agent="AttackerBot/2.0"), "Spoofed UA rejected")
    
    # Tampered signature
    tampered_sig = token[:-8] + "deadbeef"
    assert_check("Reject Tampered Token Signature", not sm2.verify_token(tampered_sig, user_agent=ua), "Tampered signature rejected")

    # 4. URL-Safe Access Key Auto-Login & 303 PRG
    key = "ts_live_" + secrets.token_urlsafe(24)
    sec_cfg.access_key = key
    assert_check("Access Key Valid", sec_cfg.verify_access_key(key), "Key matches")
    assert_check("Access Key Invalid", not sec_cfg.verify_access_key(key + "_bad"), "Key mismatch rejected")

    # 5. Sliding-Window Rate Limiter & Tarpitting
    limiter = auth.SlidingWindowTarpitLimiter(window_sec=900, max_failures=5, base_delay=0.001, max_delay=0.016)
    test_ip = "198.51.100.77"
    for i in range(5):
        allowed, _ = limiter.check_rate_limit(test_ip)
        assert_check(f"Rate Limiter Pre-check Attempt {i+1}", allowed, f"Attempt {i+1} allowed before max failures")
        limiter.record_failure(test_ip)

    locked, retry = limiter.check_rate_limit(test_ip)
    assert_check("Rate Limiter 6th Attempt Lockout", not locked and retry > 0, f"Locked out=True, Retry-After={retry:.1f}s")
    limiter.record_success(test_ip)
    reset_ok, _ = limiter.check_rate_limit(test_ip)
    assert_check("Rate Limiter Reset on Success", reset_ok, "Counter cleared on successful login")

    # 6. Proxy Header Resolution Security
    class MockH:
        def __init__(self, ip, headers):
            self.client_address = (ip, 1234)
            self.headers = headers

    h_lan = MockH("192.168.1.120", {"CF-Connecting-IP": "127.0.0.1", "X-Forwarded-For": "127.0.0.1"})
    assert_check("Direct LAN Ignores Proxy Headers", auth.get_client_ip(h_lan) == "192.168.1.120", "LAN socket IP preserved")

    h_loop = MockH("127.0.0.1", {"CF-Connecting-IP": "203.0.113.195"})
    assert_check("Loopback Cloudflare Tunnel Resolves CF-IP", auth.get_client_ip(h_loop) == "203.0.113.195", "CF-Connecting-IP parsed")

    # 7. Safe Path & Normalization
    temp_sandbox = tempfile.mkdtemp(prefix="auditor_sandbox_")
    with open(os.path.join(temp_sandbox, "allowed.txt"), "w") as f:
        f.write("allowed")

    traversal_attacks = [
        "../secret.txt",
        "..\\secret.txt",
        "..%2fsecret.txt",
        "%2e%2e%2fsecret.txt",
        "%2e%2e%5csecret.txt",
        "%252e%252e%252fsecret.txt",
        "%25252e%25252e%25252fsecret.txt",
        "allowed.txt\x00.exe",
        "allowed.txt%00.exe",
        "allowed.txt%2500.exe",
        "allowed.txt::",
        "allowed.txt:stream",
        "CON", "PRN", "AUX", "NUL", "COM1", "LPT1",
        "\\\\attacker.com\\share\\file.exe",
        "//attacker.com/share/file.exe",
        "C:\\Windows\\win.ini",
        "C:/Windows/win.ini",
        "/etc/passwd",
        "/../etc/shadow",
        "sub/../../../../etc/passwd"
    ]

    for attack in traversal_attacks:
        res = hostdrop.safe_path(temp_sandbox, attack)
        assert_check(f"Safe Path Blocks Traversal '{attack}'", res is None, "Returned None")

    valid_res = hostdrop.safe_path(temp_sandbox, "allowed.txt")
    assert_check("Safe Path Resolves Legitimate File", valid_res is not None and os.path.isfile(valid_res), f"Resolved to {valid_res}")

    # Clean up
    import shutil
    shutil.rmtree(temp_sandbox, ignore_errors=True)

    print("=" * 65)
    print(f"  TOTAL INDEPENDENT CHECKS : {passed_checks + failed_checks}")
    print(f"  PASSED                   : {passed_checks}")
    print(f"  FAILED                   : {failed_checks}")
    print(f"  VERDICT                  : {'CLEAN & VERIFIED' if failed_checks == 0 else 'FAILURES DETECTED'}")
    print("=" * 65)
    return failed_checks == 0

if __name__ == "__main__":
    ok = run_independent_victory_audit()
    sys.exit(0 if ok else 1)
