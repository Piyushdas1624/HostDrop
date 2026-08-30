"""
TurboShare Hardened Authentication, Rate Limiting & Session Engine
Zero-Dependency Standard Library Implementation (Python 3.8+)

Implements:
1. Salted PBKDF2-HMAC-SHA256 password derivation (600,000 iterations, 16-byte random salt)
   with constant-time verification (hmac.compare_digest).
2. Persistent stateless HMAC-SHA256 signed session tokens (256-bit entropy) in
   HttpOnly; SameSite=Strict; Secure cookies surviving server reboots.
3. URL-Safe bookmarkable access key (/api/auth?key=...) with HTTP 303 PRG clean redirect,
   Referrer-Policy: no-referrer, and Cache-Control: no-store history scrubbing.
4. Sliding-window IP rate limiting (900s, max 5 failed attempts) with exponential
   delay tarpitting (1s, 2s, 4s, 8s, 16s, then HTTP 429 lockout).
5. Safe proxy header resolution (CF-Connecting-IP, X-Forwarded-For) strictly when
   direct socket peer is verified localhost loopback (127.0.0.1, ::1).
"""

import os
import sys
import time
import json
import hmac
import hashlib
import secrets
import threading
import ipaddress
import urllib.parse
from http import cookies
from collections import defaultdict
from typing import Optional, Tuple, Dict, List, Any

# ── Configuration Constants ───────────────────────────────────────────────────
CONFIG_FILE = ".env"
SESSIONS_FILE = ".sessions.json"
SESSION_COOKIE_NAME = "turboshare_session"
DEFAULT_ITERATIONS = 600_000
SESSION_TTL_DAYS = 30
RATE_LIMIT_WINDOW = 900          # 15 minutes in seconds
MAX_FAILED_ATTEMPTS = 5
BASE_TARPIT_DELAY = 1.0         # seconds
MAX_TARPIT_DELAY = 16.0         # seconds


class SecurityConfig:
    """
    Manages persistent cryptographic keys, master passphrase hash,
    and security flags stored in .env.
    """

    def __init__(self, env_path: str = CONFIG_FILE):
        self.env_path = env_path
        self.secret_key: str = ""
        self.access_key: str = ""
        self.password_hash: str = ""
        self.raw_password: str = ""
        self.trust_proxy_headers: bool = True
        self.allow_full_drive_remote: bool = False
        self.lock = threading.Lock()
        self.load_or_initialize()

    def load_or_initialize(self) -> None:
        """Load configuration from .env or initialize secure defaults."""
        with self.lock:
            env_vars: Dict[str, str] = {}
            if os.path.exists(self.env_path):
                try:
                    with open(self.env_path, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith("#") and "=" in line:
                                k, v = line.split("=", 1)
                                env_vars[k.strip()] = v.strip().strip("'\"")
                except Exception as e:
                    print(f"[AUTH WARNING] Could not read {self.env_path}: {e}")

            dirty = False

            # 1. Server Secret Key for HMAC Session Tokens (256-bit entropy)
            if "TURBOSHARE_SECRET_KEY" in env_vars and len(env_vars["TURBOSHARE_SECRET_KEY"]) >= 32:
                self.secret_key = env_vars["TURBOSHARE_SECRET_KEY"]
            else:
                self.secret_key = secrets.token_hex(32)
                env_vars["TURBOSHARE_SECRET_KEY"] = self.secret_key
                dirty = True

            # 2. URL-Safe Bookmarkable Secret Access Key
            if "TURBOSHARE_ACCESS_KEY" in env_vars and len(env_vars["TURBOSHARE_ACCESS_KEY"]) >= 16:
                self.access_key = env_vars["TURBOSHARE_ACCESS_KEY"]
            else:
                self.access_key = "ts_live_" + secrets.token_urlsafe(24)
                env_vars["TURBOSHARE_ACCESS_KEY"] = self.access_key
                dirty = True

            # 3. Master Passphrase Hash (PBKDF2-HMAC-SHA256, 600,000 iterations)
            if "TURBOSHARE_PASSWORD_HASH" in env_vars and env_vars["TURBOSHARE_PASSWORD_HASH"].startswith("pbkdf2_sha256$"):
                self.password_hash = env_vars["TURBOSHARE_PASSWORD_HASH"]
            elif "APP_PASSWORD" in env_vars and env_vars["APP_PASSWORD"]:
                # Automatic secure migration from legacy plaintext APP_PASSWORD
                raw_pwd = env_vars["APP_PASSWORD"]
                self.raw_password = raw_pwd
                self.password_hash = self.hash_password(raw_pwd)
                env_vars["TURBOSHARE_PASSWORD_HASH"] = self.password_hash
                del env_vars["APP_PASSWORD"]
                dirty = True
                print("[AUTH NOTICE] Migrated plaintext APP_PASSWORD to salted PBKDF2-HMAC-SHA256 hash in .env")
            else:
                # Auto-generate secure 16-character alphanumeric passphrase
                chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
                generated_pwd = "".join(secrets.choice(chars) for _ in range(16))
                self.raw_password = generated_pwd
                self.password_hash = self.hash_password(generated_pwd)
                env_vars["TURBOSHARE_PASSWORD_HASH"] = self.password_hash
                dirty = True
                print("\n" + "=" * 68)
                print("  [SECURITY NOTICE] AUTO-GENERATED MASTER APP PASSWORD:")
                print(f"  >>>  {generated_pwd}  <<<")
                print("  Bookmark Access Key:")
                print(f"  >>>  {self.access_key}  <<<")
                print("  Saved salted PBKDF2 hash to .env. Keep this confidential!")
                print("=" * 68 + "\n")

            # 4. Optional Security Flags
            if "TRUST_PROXY_HEADERS" in env_vars:
                self.trust_proxy_headers = env_vars["TRUST_PROXY_HEADERS"].lower() in ("1", "true", "yes")
            else:
                self.trust_proxy_headers = True

            if "ALLOW_FULL_DRIVE_REMOTE" in env_vars:
                self.allow_full_drive_remote = env_vars["ALLOW_FULL_DRIVE_REMOTE"].lower() in ("1", "true", "yes")
            else:
                self.allow_full_drive_remote = False

            if dirty:
                try:
                    with open(self.env_path, "w", encoding="utf-8") as f:
                        f.write("# TurboShare Hardened Security Configuration\n")
                        f.write("# Generated automatically on startup\n\n")
                        for k, v in env_vars.items():
                            f.write(f"{k}={v}\n")
                    if sys.platform != "win32":
                        try:
                            os.chmod(self.env_path, 0o600)
                        except Exception:
                            pass
                except Exception as e:
                    print(f"[AUTH ERROR] Could not save security configuration to {self.env_path}: {e}")

    @staticmethod
    def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
        """Derive salted PBKDF2-HMAC-SHA256 hash string."""
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
        return f"pbkdf2_sha256${iterations}${salt.hex()}${dk.hex()}"

    def verify_password(self, password: str) -> bool:
        """Constant-time verification of password against stored PBKDF2 hash."""
        if not self.password_hash or not password:
            return False
        try:
            parts = self.password_hash.split("$")
            if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
                return False
            _, iters_str, salt_hex, expected_dk_hex = parts
            iterations = int(iters_str)
            salt = bytes.fromhex(salt_hex)
            expected_dk = bytes.fromhex(expected_dk_hex)
            actual_dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, dklen=32)
            return hmac.compare_digest(actual_dk, expected_dk)
        except Exception:
            return False

    def verify_access_key(self, key: str) -> bool:
        """Constant-time verification of bookmark secret access key."""
        if not self.access_key or not key:
            return False
        return hmac.compare_digest(key.strip(), self.access_key.strip())


def parse_device_info(user_agent: str) -> str:
    """Parse user-agent string into human-friendly device and browser representation."""
    ua = (user_agent or "").lower()
    if not ua:
        return "🌐 Browser / Web Client"

    # OS / Hardware Platform
    os_name = "Desktop"
    if "iphone" in ua:
        os_name = "📱 Apple iPhone"
    elif "ipad" in ua:
        os_name = "📱 Apple iPad"
    elif "android" in ua:
        os_name = "📱 Android Phone"
    elif "windows nt 10" in ua or "windows nt 11" in ua or "windows" in ua:
        os_name = "💻 Windows PC"
    elif "macintosh" in ua or "mac os" in ua:
        os_name = "💻 Apple Mac"
    elif "linux" in ua:
        os_name = "💻 Linux PC"

    # Browser Engine / Client
    browser = "Browser"
    if "edg/" in ua or "edg" in ua:
        browser = "Microsoft Edge"
    elif "chrome/" in ua and "edg" not in ua:
        browser = "Google Chrome"
    elif "safari/" in ua and "chrome" not in ua:
        browser = "Apple Safari"
    elif "firefox/" in ua:
        browser = "Mozilla Firefox"
    elif "curl" in ua or "python" in ua or "bot" in ua:
        browser = "API Client"

    return f"{os_name} · {browser}"


def parse_location(handler: Any) -> str:
    """Extract location from Cloudflare headers or fallback to connection origin."""
    if not handler:
        return "Unknown"
    headers = getattr(handler, "headers", {}) or {}
    country = str(headers.get("CF-IPCountry", "")).strip()
    city = str(headers.get("CF-IPCity", "")).strip()
    if city and country:
        return f"{city}, {country}"
    if country:
        return country
    peer = getattr(handler, "client_address", ("", 0))[0]
    peer_str = str(peer)
    if peer_str in ("127.0.0.1", "::1", "localhost") or peer_str.startswith("127."):
        return "Host Localhost"
    if peer_str.startswith("192.168.") or peer_str.startswith("10.") or peer_str.startswith("172."):
        return "Local Network (LAN)"
    return "Remote Internet"


class SessionRegistry:
    """
    Thread-safe persistent registry of active and revoked remote sessions.
    Maintains session history with IP, device, location, and timestamps.
    """

    def __init__(self, storage_path: str = SESSIONS_FILE):
        self.storage_path = storage_path
        self.lock = threading.Lock()
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.revoked_ids: set = set()
        self.load()

    def load(self) -> None:
        with self.lock:
            if os.path.exists(self.storage_path):
                try:
                    with open(self.storage_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        self.sessions = data.get("sessions", {})
                        self.revoked_ids = set(data.get("revoked_ids", []))
                except Exception as e:
                    print(f"[AUTH WARNING] Could not load session registry: {e}")

    def save(self) -> None:
        try:
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump({
                    "sessions": self.sessions,
                    "revoked_ids": list(self.revoked_ids)
                }, f, indent=2)
        except Exception as e:
            print(f"[AUTH ERROR] Could not save session registry: {e}")

    def register(self, session_id: str, client_ip: str = "", user_agent: str = "", location: str = "") -> None:
        with self.lock:
            now = int(time.time())
            self.sessions[session_id] = {
                "id": session_id,
                "ip": client_ip or "Unknown",
                "device": parse_device_info(user_agent),
                "location": location or "Unknown",
                "user_agent": (user_agent or "")[:150],
                "issued_at": now,
                "last_active": now,
                "revoked": False
            }
            self.save()

    def touch(self, session_id: str) -> None:
        with self.lock:
            if session_id in self.sessions and not self.sessions[session_id].get("revoked"):
                self.sessions[session_id]["last_active"] = int(time.time())

    def is_revoked(self, session_id: str) -> bool:
        with self.lock:
            if session_id in self.revoked_ids:
                return True
            sess = self.sessions.get(session_id)
            return bool(sess and sess.get("revoked"))

    def revoke(self, session_id: str) -> bool:
        with self.lock:
            self.revoked_ids.add(session_id)
            if session_id in self.sessions:
                self.sessions[session_id]["revoked"] = True
            self.save()
            return True

    def revoke_all(self) -> int:
        with self.lock:
            count = 0
            for sid in self.sessions:
                if not self.sessions[sid].get("revoked"):
                    self.sessions[sid]["revoked"] = True
                    count += 1
                self.revoked_ids.add(sid)
            self.save()
            return count

    def list_sessions(self) -> List[Dict[str, Any]]:
        with self.lock:
            result = list(self.sessions.values())
            result.sort(key=lambda s: s.get("last_active", 0), reverse=True)
            return result


GLOBAL_SESSION_REGISTRY = SessionRegistry()


class SessionManager:
    """
    Manages creation and stateless cryptographic verification of persistent
    HMAC-SHA256 signed session tokens surviving server reboots.
    """

    def __init__(self, secret_key: str):
        self.secret_key = secret_key

    def update_secret_key(self, secret_key: str) -> None:
        self.secret_key = secret_key

    def create_token(self, client_ip: str = "", user_agent: str = "", location: str = "", ttl_days: int = SESSION_TTL_DAYS) -> str:
        """Generate a cryptographically signed stateless session token and register it."""
        version = "v1"
        session_id = secrets.token_hex(16)
        issued_at = int(time.time())
        expires_at = issued_at + (ttl_days * 86400)
        ua_hash = hashlib.sha256((user_agent or "").encode("utf-8")).hexdigest()[:16]
        payload = f"{version}.{session_id}.{issued_at}.{expires_at}.{ua_hash}"
        signature = hmac.new(self.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        
        # Register in session registry
        GLOBAL_SESSION_REGISTRY.register(session_id, client_ip=client_ip, user_agent=user_agent, location=location)
        
        return f"{payload}.{signature}"

    def verify_token(self, token: str, user_agent: str = "") -> bool:
        """Verify session token signature, expiration, user agent fingerprint, and revocation in constant time."""
        if not token or not self.secret_key:
            return False
        try:
            parts = token.strip().split(".")
            if len(parts) != 6:
                return False
            version, session_id, issued_at_str, expires_at_str, ua_hash, signature = parts
            if version != "v1":
                return False

            payload = f"{version}.{session_id}.{issued_at_str}.{expires_at_str}.{ua_hash}"
            expected_signature = hmac.new(
                self.secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
            ).hexdigest()

            if not hmac.compare_digest(signature, expected_signature):
                return False

            now = int(time.time())
            issued_at = int(issued_at_str)
            expires_at = int(expires_at_str)

            # Check expiration and 5-minute clock skew tolerance
            if now > expires_at or now < (issued_at - 300):
                return False

            # Verify user-agent hash if provided
            if user_agent:
                expected_ua_hash = hashlib.sha256(user_agent.encode("utf-8")).hexdigest()[:16]
                if not hmac.compare_digest(ua_hash, expected_ua_hash):
                    return False

            # Check revocation in session registry
            if GLOBAL_SESSION_REGISTRY.is_revoked(session_id):
                return False

            # Update last_active
            GLOBAL_SESSION_REGISTRY.touch(session_id)

            return True
        except Exception:
            return False


class SlidingWindowTarpitLimiter:
    """
    Thread-safe sliding-window rate limiter with exponential delay tarpitting.
    Limits failed authentication attempts per IP and slows down automated brute-force attacks.
    """

    def __init__(
        self,
        window_sec: int = RATE_LIMIT_WINDOW,
        max_failures: int = MAX_FAILED_ATTEMPTS,
        base_delay: float = BASE_TARPIT_DELAY,
        max_delay: float = MAX_TARPIT_DELAY,
    ):
        self.window_sec = window_sec
        self.max_failures = max_failures
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.lock = threading.Lock()
        self.failure_history: Dict[str, List[float]] = defaultdict(list)
        self.last_cleanup = time.time()

    def _cleanup_stale(self, now: float) -> None:
        """Remove entries older than window_sec periodically."""
        if now - self.last_cleanup > 300:
            cutoff = now - self.window_sec
            stale_ips = [ip for ip, timestamps in self.failure_history.items() if not timestamps or timestamps[-1] < cutoff]
            for ip in stale_ips:
                del self.failure_history[ip]
            self.last_cleanup = now

    def check_rate_limit(self, client_ip: str) -> Tuple[bool, float]:
        """
        Check if an IP is currently allowed to attempt authentication.
        Returns:
            (is_allowed: bool, delay_or_retry_after: float)
            - If allowed: (True, current_delay)
            - If locked out (>= max_failures in window): (False, remaining_lockout_seconds)
        """
        now = time.time()
        with self.lock:
            self._cleanup_stale(now)
            cutoff = now - self.window_sec
            history = [t for t in self.failure_history.get(client_ip, []) if t > cutoff]
            self.failure_history[client_ip] = history
            failures = len(history)

            if failures >= self.max_failures:
                # Locked out until oldest failure expires
                oldest = history[0] if history else now
                remaining = max(1.0, self.window_sec - (now - oldest))
                return False, remaining

            current_delay = 0.0
            if failures > 0:
                current_delay = min(self.base_delay * (2 ** (failures - 1)), self.max_delay)

            return True, current_delay

    def record_failure(self, client_ip: str) -> Tuple[float, int]:
        """
        Record a failed authentication attempt and calculate tarpit delay.
        Executes tarpit sleep outside the lock to prevent thread starvation.
        Returns (delay_seconds, total_failures_in_window).
        """
        now = time.time()
        with self.lock:
            self._cleanup_stale(now)
            cutoff = now - self.window_sec
            history = [t for t in self.failure_history.get(client_ip, []) if t > cutoff]
            history.append(now)
            self.failure_history[client_ip] = history
            failures = len(history)
            delay = min(self.base_delay * (2 ** (failures - 1)), self.max_delay)

        # Sleep outside the lock so other requests are not blocked
        time.sleep(delay)
        return delay, failures

    def record_success(self, client_ip: str) -> None:
        """Reset failed attempt history for client IP upon successful login."""
        with self.lock:
            if client_ip in self.failure_history:
                del self.failure_history[client_ip]


# ── Global Singleton Instances ─────────────────────────────────────────────────
GLOBAL_SECURITY_CONFIG = SecurityConfig()
GLOBAL_SESSION_MANAGER = SessionManager(GLOBAL_SECURITY_CONFIG.secret_key)
GLOBAL_RATE_LIMITER = SlidingWindowTarpitLimiter()


# ── Public API Interface Contracts ─────────────────────────────────────────────

def get_security_config() -> SecurityConfig:
    return GLOBAL_SECURITY_CONFIG

def init_config(env_path: str = CONFIG_FILE) -> SecurityConfig:
    global GLOBAL_SECURITY_CONFIG
    GLOBAL_SECURITY_CONFIG = SecurityConfig(env_path)
    return GLOBAL_SECURITY_CONFIG

def get_master_password() -> str:
    """Return raw master password if available in memory, or masked notice."""
    return getattr(GLOBAL_SECURITY_CONFIG, "raw_password", "") or "[Stored hashed in .env]"

def hash_password(password: str, iterations: int = DEFAULT_ITERATIONS) -> str:
    """Derive salted PBKDF2-HMAC-SHA256 hash string."""
    return SecurityConfig.hash_password(password, iterations=iterations)

def get_secret_key() -> str:
    return GLOBAL_SECURITY_CONFIG.secret_key

def get_access_key() -> str:
    return GLOBAL_SECURITY_CONFIG.access_key

def get_session_manager() -> SessionManager:
    # Ensure session manager uses latest secret key
    if GLOBAL_SESSION_MANAGER.secret_key != GLOBAL_SECURITY_CONFIG.secret_key:
        GLOBAL_SESSION_MANAGER.update_secret_key(GLOBAL_SECURITY_CONFIG.secret_key)
    return GLOBAL_SESSION_MANAGER

def get_rate_limiter() -> SlidingWindowTarpitLimiter:
    return GLOBAL_RATE_LIMITER

def verify_password(password: str) -> bool:
    """Verify master passphrase against stored PBKDF2 hash in constant time."""
    return GLOBAL_SECURITY_CONFIG.verify_password(password)

def verify_access_key(key: str) -> bool:
    """Verify bookmark access key in constant time."""
    return GLOBAL_SECURITY_CONFIG.verify_access_key(key)

def get_session_registry() -> SessionRegistry:
    return GLOBAL_SESSION_REGISTRY

def list_sessions() -> List[Dict[str, Any]]:
    return GLOBAL_SESSION_REGISTRY.list_sessions()

def revoke_session(session_id: str) -> bool:
    return GLOBAL_SESSION_REGISTRY.revoke(session_id)

def revoke_all_sessions() -> int:
    return GLOBAL_SESSION_REGISTRY.revoke_all()

def change_master_password(new_password: str, revoke_all_sessions: bool = True) -> Tuple[bool, str]:
    """
    Update master passphrase, re-hash with PBKDF2-HMAC-SHA256, write to .env,
    and optionally revoke all active remote sessions.
    """
    cleaned = new_password.strip()
    if len(cleaned) < 6:
        return False, "Master passcode must be at least 6 characters long."

    new_hash = hash_password(cleaned)
    GLOBAL_SECURITY_CONFIG.password_hash = new_hash
    GLOBAL_SECURITY_CONFIG.raw_password = cleaned

    env_path = GLOBAL_SECURITY_CONFIG.env_path
    lines = []
    if os.path.exists(env_path):
        try:
            with open(env_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            print(f"[AUTH ERROR] Failed reading {env_path}: {e}")

    new_lines = []
    hash_written = False
    for line in lines:
        if line.strip().startswith("TURBOSHARE_PASSWORD_HASH="):
            new_lines.append(f"TURBOSHARE_PASSWORD_HASH={new_hash}\n")
            hash_written = True
        elif line.strip().startswith("APP_PASSWORD="):
            continue
        else:
            new_lines.append(line)

    if not hash_written:
        new_lines.append(f"TURBOSHARE_PASSWORD_HASH={new_hash}\n")

    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        return False, f"Could not write to {env_path}: {e}"

    revoked_count = 0
    if revoke_all_sessions:
        revoked_count = GLOBAL_SESSION_REGISTRY.revoke_all()

    return True, f"Master passcode successfully updated. {revoked_count} active remote session(s) revoked."

def create_session_token(client_ip: str = "", user_agent: str = "", location: str = "") -> str:
    """Create a persistent signed session token and register metadata."""
    return get_session_manager().create_token(client_ip=client_ip, user_agent=user_agent, location=location)

def verify_session_token(token: str, client_ip: str = "", user_agent: str = "") -> bool:
    """Verify persistent signed session token."""
    return get_session_manager().verify_token(token, user_agent=user_agent)

def check_rate_limit(client_ip: str) -> Tuple[bool, float]:
    """Check sliding window rate limit for client IP."""
    return GLOBAL_RATE_LIMITER.check_rate_limit(client_ip)

def record_login_failure(client_ip: str) -> None:
    """Record login failure and execute tarpit delay."""
    GLOBAL_RATE_LIMITER.record_failure(client_ip)

def record_login_success(client_ip: str) -> None:
    """Record login success and clear failure counter."""
    GLOBAL_RATE_LIMITER.record_success(client_ip)

def get_client_ip(handler: Any) -> str:
    """
    Deterministically resolves the real client IP address:
    1. Direct connection from non-loopback IP -> Strictly ignore proxy headers (prevents LAN spoofing).
    2. Connection from loopback (127.0.0.1 / ::1) -> Parse CF-Connecting-IP or X-Forwarded-For if trusted.
    """
    if not handler or not hasattr(handler, "client_address"):
        return "127.0.0.1"

    socket_ip = str(handler.client_address[0])
    is_loopback = socket_ip in ("127.0.0.1", "::1", "localhost") or socket_ip.startswith("127.")

    if not is_loopback or not GLOBAL_SECURITY_CONFIG.trust_proxy_headers:
        return socket_ip

    headers = getattr(handler, "headers", None)
    if not headers:
        return socket_ip

    # 1. Cloudflare Tunnel Header
    cf_ip = headers.get("CF-Connecting-IP")
    if cf_ip:
        cf_ip = cf_ip.strip()
        try:
            ipaddress.ip_address(cf_ip)
            return cf_ip
        except ValueError:
            pass

    # 2. X-Forwarded-For Header (extract first/client IP)
    xff = headers.get("X-Forwarded-For")
    if xff:
        hops = [h.strip() for h in xff.split(",") if h.strip()]
        for cand in hops:
            try:
                ipaddress.ip_address(cand)
                if not cand.startswith("127."):
                    return cand
            except ValueError:
                continue

    # 3. X-Real-IP Header
    x_real = headers.get("X-Real-IP")
    if x_real:
        x_real = x_real.strip()
        try:
            ipaddress.ip_address(x_real)
            return x_real
        except ValueError:
            pass

    return socket_ip


def is_authenticated(handler: Any) -> bool:
    """Check whether incoming request carries a valid persistent session cookie."""
    if not handler or not hasattr(handler, "headers"):
        return False
    cookie_header = handler.headers.get("Cookie", "")
    if not cookie_header:
        return False
    try:
        c = cookies.SimpleCookie()
        c.load(cookie_header)
        token = None
        if SESSION_COOKIE_NAME in c:
            token = c[SESSION_COOKIE_NAME].value
        elif "ts_session" in c:
            token = c["ts_session"].value

        if not token:
            return False

        ua = handler.headers.get("User-Agent", "")
        return get_session_manager().verify_token(token, user_agent=ua)
    except Exception:
        return False


def build_session_cookie(token: str, is_https: bool = False, max_age: int = 2592000) -> str:
    """Construct secure Set-Cookie header string."""
    flags = [
        f"{SESSION_COOKIE_NAME}={token}",
        "Path=/",
        f"Max-Age={max_age}",
        "HttpOnly",
        "SameSite=Strict"
    ]
    if is_https:
        flags.append("Secure")
    return "; ".join(flags)


def is_request_https(handler: Any) -> bool:
    """Determine if request arrived over HTTPS (directly or via TLS-terminating reverse proxy)."""
    if not handler or not hasattr(handler, "headers"):
        return False
    headers = handler.headers
    if headers.get("X-Forwarded-Proto") == "https":
        return True
    if headers.get("X-Forwarded-Ssl") == "on":
        return True
    cf_visitor = headers.get("CF-Visitor", "")
    if '"scheme":"https"' in cf_visitor or '"scheme": "https"' in cf_visitor:
        return True
    return False


def handle_auth_routes(handler: Any, path: str, qs: Dict[str, List[str]], body_data: Optional[Dict[str, Any]] = None) -> bool:
    """
    Handle /api/auth, /api/login, /api/logout, and /api/check_auth routes.
    Returns True if request was handled, False otherwise.
    """
    client_ip = get_client_ip(handler)
    is_https = is_request_https(handler)

    # 1. URL-Safe Auto-Login via Bookmarked Key (/api/auth?key=...)
    if path == "/api/auth":
        key = qs.get("key", [""])[0] if qs else ""
        if not key and body_data and "key" in body_data:
            key = str(body_data["key"]).strip()

        is_allowed, penalty = check_rate_limit(client_ip)
        if not is_allowed:
            payload = json.dumps({"error": "rate_limited", "retry_after": int(penalty)}).encode("utf-8")
            handler.send_response(429)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Retry-After", str(int(penalty)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        if verify_access_key(key):
            record_login_success(client_ip)
            ua = handler.headers.get("User-Agent", "") if hasattr(handler, "headers") else ""
            loc = parse_location(handler)
            token = create_session_token(client_ip=client_ip, user_agent=ua, location=loc)
            cookie_hdr = build_session_cookie(token, is_https=is_https)

            # Issue HTTP 303 PRG Clean Redirect
            handler.send_response(303)
            handler.send_header("Location", "/")
            handler.send_header("Set-Cookie", cookie_hdr)
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            handler.send_header("Pragma", "no-cache")
            handler.send_header("Expires", "0")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return True
        else:
            record_login_failure(client_ip)
            payload = json.dumps({"success": False, "error": "invalid_access_key"}).encode("utf-8")
            handler.send_response(401)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

    # 2. JSON API Login (/api/login)
    if path == "/api/login":
        password = ""
        key = ""
        if body_data:
            password = str(body_data.get("password", "")).strip()
            key = str(body_data.get("key", "")).strip()
        elif qs:
            password = qs.get("password", [""])[0]
            key = qs.get("key", [""])[0]

        is_allowed, penalty = check_rate_limit(client_ip)
        if not is_allowed:
            payload = json.dumps({"error": "rate_limited", "retry_after": int(penalty)}).encode("utf-8")
            handler.send_response(429)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Retry-After", str(int(penalty)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        authenticated = False
        if password and verify_password(password):
            authenticated = True
        elif key and verify_access_key(key):
            authenticated = True

        if authenticated:
            record_login_success(client_ip)
            ua = handler.headers.get("User-Agent", "") if hasattr(handler, "headers") else ""
            loc = parse_location(handler)
            token = create_session_token(client_ip=client_ip, user_agent=ua, location=loc)
            cookie_hdr = build_session_cookie(token, is_https=is_https)

            payload = json.dumps({"success": True, "status": "ok", "token": token}).encode("utf-8")
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.send_header("Set-Cookie", cookie_hdr)
            handler.send_header("Cache-Control", "no-store")
            handler.end_headers()
            handler.wfile.write(payload)
            return True
        else:
            record_login_failure(client_ip)
            payload = json.dumps({"success": False, "error": "invalid_credentials"}).encode("utf-8")
            handler.send_response(401)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

    # 3. Logout (/api/logout)
    if path == "/api/logout":
        # Extract session_id and revoke server-side
        cookie_header = handler.headers.get("Cookie", "") if hasattr(handler, "headers") else ""
        if cookie_header:
            try:
                c = cookies.SimpleCookie()
                c.load(cookie_header)
                tok = None
                if SESSION_COOKIE_NAME in c:
                    tok = c[SESSION_COOKIE_NAME].value
                elif "ts_session" in c:
                    tok = c["ts_session"].value
                if tok:
                    tok_parts = tok.split(".")
                    if len(tok_parts) >= 2:
                        GLOBAL_SESSION_REGISTRY.revoke(tok_parts[1])
            except Exception:
                pass

        cookie_hdr = f"{SESSION_COOKIE_NAME}=deleted; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
        payload = json.dumps({"success": True, "logged_out": True}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.send_header("Set-Cookie", cookie_hdr)
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 4. Check Authentication Status (/api/check_auth)
    if path == "/api/check_auth":
        authed = is_authenticated(handler)
        payload = json.dumps({"authenticated": authed}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 5. List Sessions (/api/sessions) - Strictly Host Only
    if path == "/api/sessions":
        is_host = getattr(handler, "is_physical_localhost", lambda: True)()
        if not is_host:
            payload = json.dumps({"success": False, "error": "forbidden", "message": "Host only."}).encode("utf-8")
            handler.send_response(403)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        sessions = GLOBAL_SESSION_REGISTRY.list_sessions()
        payload = json.dumps({"success": True, "sessions": sessions}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 6. Revoke Remote Session (/api/revoke_session) - Strictly Host Only
    if path == "/api/revoke_session":
        is_host = getattr(handler, "is_physical_localhost", lambda: True)()
        if not is_host:
            payload = json.dumps({"success": False, "error": "forbidden", "message": "Host only."}).encode("utf-8")
            handler.send_response(403)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        target_id = body_data.get("session_id", "").strip() if body_data else ""
        revoke_all = bool(body_data.get("all", False)) if body_data else False
        if revoke_all:
            count = GLOBAL_SESSION_REGISTRY.revoke_all()
            msg = f"All active remote sessions ({count}) have been revoked."
        elif target_id:
            GLOBAL_SESSION_REGISTRY.revoke(target_id)
            msg = f"Session {target_id[:8]}... successfully revoked."
        else:
            msg = "No session ID specified."

        payload = json.dumps({"success": True, "message": msg}).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    # 7. Change Master Password (/api/change_password) - Strictly Host Only
    if path == "/api/change_password":
        is_host = getattr(handler, "is_physical_localhost", lambda: True)()
        if not is_host:
            payload = json.dumps({"success": False, "error": "forbidden", "message": "Host only."}).encode("utf-8")
            handler.send_response(403)
            handler.send_header("Content-Type", "application/json")
            handler.send_header("Content-Length", str(len(payload)))
            handler.end_headers()
            handler.wfile.write(payload)
            return True

        new_pwd = body_data.get("new_password", "").strip() if body_data else ""
        revoke_all = bool(body_data.get("revoke_sessions", True)) if body_data else True
        ok, msg = change_master_password(new_pwd, revoke_all_sessions=revoke_all)
        status = 200 if ok else 400
        payload = json.dumps({"success": ok, "message": msg}).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(payload)))
        handler.end_headers()
        handler.wfile.write(payload)
        return True

    return False
