# TurboShare Adversarial Cybersecurity Audit & Architectural Redesign Specification

**Target System**: TurboShare Multi-Platform File Transfer Engine  
**Document Version**: 1.0.0-PROD-SEC  
**Security Classification**: CONFIDENTIAL & HIGH-ASSURANCE  
**Audit Standard**: OWASP Top 10 (2021/2026), NIST SP 800-63B, CWE/SANS Top 25  
**Integrity Mode**: Zero-Dependency Python 3.8+ Standard Library  

---

## 1. Executive Summary & Security Philosophy

TurboShare is an ultra-fast, zero-dependency cross-device file transfer engine designed for PC-to-PC, PC-to-mobile, and mobile-to-PC data transfer. While initially architected for trusted, isolated Local Area Networks (Wi-Fi, direct Ethernet, and Mobile Hotspot), extending TurboShare to support **Global Remote Access via reverse tunnels (Cloudflare Tunnel, Pinggy SSH, ngrok)** fundamentally transforms its operational threat environment.

Exposing a local HTTP file server to the open internet exposes it to automated vulnerability scanners, credential stuffers, dictionary botnets, and targeted adversarial exploitation. Prior versions of the application operated under the implicit assumption of trusted physical and network adjacency, leaving critical gaps:
1. Complete absence of authentication on high-privilege administrative endpoints.
2. Inadvertent disclosure of host usernames, directory structures, and drive letters.
3. Vulnerability to remote GUI execution and desktop harassment via localhost proxy aliasing.
4. Memory exhaustion denial of service via unbounded in-memory ZIP archiving.
5. Cross-Site Request Forgery (CSRF) and cross-origin data theft via wildcard CORS headers.

This specification documents the **zero-trust adversarial cybersecurity audit**, the **hardened persistent global authentication architecture**, formal **mathematical security invariants**, the **comprehensive penetration testing matrix (Categories A–F)**, and **operational liability disclaimers**.

---

## 2. Threat Modeling: Public Reverse Tunnel Exposure

```
[ Adversary / Port Scanner / Botnet ]       [ Legitimate Remote Mobile User ]
                   │                                        │
                   ▼ (HTTPS / TLS 1.3)                      ▼ (HTTPS / TLS 1.3)
 ┌────────────────────────────────────────────────────────────────────────────┐
 │         Reverse Tunnel Edge Provider (Cloudflare / Pinggy / ngrok)          │
 │ - Edge TLS Termination                                                     │
 │ - Injection of Reverse-Proxy Headers (CF-Connecting-IP, X-Forwarded-For)  │
 └─────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Outbound Multiplexed Tunnel (QUIC / SSH)
                                       ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │       Local Tunnel Client on Host PC (cloudflared / ssh / ngrok daemon)    │
 └─────────────────────────────────────┬──────────────────────────────────────┘
                                       │ Direct Loopback HTTP (127.0.0.1:8080)
                                       ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │                     TURBOSHARE ZERO-TRUST DEFENSE GATE                     │
 │ 1. Trust-Aware Client IP Extraction (Prevent LAN Spoofing & Tunnel Collapse│
 │ 2. Sliding-Window Rate Limiter & Exponential Delay Tarpitting              │
 │ 3. PBKDF2-HMAC-SHA256 Passphrase & Signed Stateless Session Tokens        │
 │ 4. URL-Safe Bookmarked Key Auto-Login with 303 PRG History Scrubbing       │
 │ 5. Privilege Sandbox & Path Confinement (C:\ Sandboxing, ADS, Null Bytes)  │
 │ 6. Memory-Bounded Stream Compression & Pre-Flight Disk Quota Checks        │
 └────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 Tunnel Provider Mechanics & Attack Surfaces

| Reverse Tunnel Provider | Ingress Protocol | Edge Capabilities & Disclosures | Inbound Local Hop | Critical Threat Vector |
|---|---|---|---|---|
| **Cloudflare Tunnel** (`cloudflared`) | HTTPS / QUIC | Sets `CF-Connecting-IP`, `CF-Visitor`, `CF-Ray`. Strips forged client headers at edge. | Local loopback `127.0.0.1:8080` | All remote traffic arrives with peer IP `127.0.0.1`. If server relies on socket IP, remote users bypass `is_local` checks. |
| **Pinggy SSH Tunnel** (`ssh -p 443 -R 0:localhost:8080`) | Reverse SSH TCP | Sets `X-Forwarded-For` and `X-Real-IP`. Random public URL generation. | Local loopback `127.0.0.1:8080` | High-frequency port scan indexing. If rate limiter blocks `127.0.0.1`, host PC user is locked out. |
| **ngrok** (`ngrok http 8080`) | TLS / HTTP/2 | Sets `X-Forwarded-For` and `X-Forwarded-Proto`. Enforces ngrok warning interstitial. | Local loopback `127.0.0.1:8080` | Botnet credential stuffing against persistent ngrok subdomains. |

### 2.2 The Localhost Trust Trap (`127.0.0.1` Collapse)
When running behind a local tunnel daemon, every incoming connection from the internet connects to the Python HTTP socket from `127.0.0.1`. 
* **Failure Mode**: If the codebase evaluates `client_ip in ("127.0.0.1", "localhost")` to grant administrative or OS integration privileges (e.g. `open_in_os_explorer` or `pick_folder_powershell`), **every stranger on the internet is granted host administrator status**, allowing them to remotely spawn GUI windows and modal file dialogs on the host PC's physical display.
* **Remediation**: The system must explicitly track whether the connection arrived via loopback tunnel versus direct physical host interaction, and restrict OS GUI triggers strictly to direct local physical access.

### 2.3 Collective Rate-Limiting Denial of Service (Self-Lockout)
* **Failure Mode**: If a sliding-window rate limiter tracks failed login attempts by `self.client_address[0]`:
  1. An attacker sends 5 invalid password attempts to the public tunnel URL.
  2. The rate limiter records 5 failures for `127.0.0.1` and triggers an IP lockout.
  3. The legitimate host user opening `http://127.0.0.1:8080` on their physical PC is blocked from accessing their own system.
* **Remediation**: The rate limiter must key on the extracted real client IP extracted through the Trust-Aware Proxy Resolver.

### 2.4 Proxy Header Spoofing on Direct LAN Connections
* **Failure Mode**: If the server blindly trusts `CF-Connecting-IP` or `X-Forwarded-For` on all incoming requests:
  1. An attacker on the local Wi-Fi network sends `X-Forwarded-For: 127.0.0.1` or rotates random IP addresses (`10.0.0.1`, `10.0.0.2`).
  2. The attacker evades rate limiting entirely and impersonates internal components.
* **Remediation**: Proxy headers MUST ONLY be evaluated if the physical TCP socket peer (`client_address[0]`) is a verified loopback address (`127.0.0.1` or `::1`) AND trusted proxy handling is explicitly enabled or auto-detected.

---

## 3. Comprehensive OWASP Top 10 Analysis for Exposed File Servers

| OWASP Top 10 Category | CVSS 3.1 Base | Vulnerability Description | Exploit Scenario | Remediation & Hardening Invariant |
|---|---|---|---|---|
| **A01:2021 — Broken Access Control** | **10.0 (Critical)** | High-privilege endpoints (`/api/browse_host`, `/api/set_path`, `/api/create_folder`) accessible without authentication. | Attacker calls `POST /api/set_path` with `{"path": "C:\\"}`, then downloads `C:\Users\<user>\.ssh\id_rsa`. | Strict session cookie validation. Guest/unauthenticated sessions are strictly sandboxed to designated directories. |
| **A02:2021 — Cryptographic Failures** | **9.8 (Critical)** | Storing passwords in plaintext or using weak/unsalted hashing; unauthenticated session tokens. | Attacker recovers plaintext password from config files or forges unsigned session cookies. | Salted PBKDF2-HMAC-SHA256 (600,000 iterations), 256-bit cryptographically signed session tokens with HMAC-SHA256. |
| **A03:2021 — Injection** | **8.6 (High)** | Unsanitized path parameters passed to system commands (`explorer.exe` / PowerShell dialogs). | Attacker injects special characters or UNC paths to trigger remote OS execution or GUI window flooding. | Complete parameter sanitization, removal of shell interpolation, and strict confinement of OS GUI triggers to local host. |
| **A04:2021 — Insecure Design** | **8.2 (High)** | In-memory ZIP compression (`io.BytesIO()`) buffering gigabyte-scale directories on the Python heap. | Attacker requests `/api/zip` on a 10GB folder; Python triggers `MemoryError` or process OOM termination. | Memory-bounded streaming ZIP generator or temporary spooling with recursive depth and total size ceilings. |
| **A05:2021 — Security Misconfiguration** | **8.5 (High)** | Universal wildcard `Access-Control-Allow-Origin: *` headers paired with state-mutating endpoints. | Attacker lures host user to a malicious site; malicious JS issues background `fetch()` to exfiltrate files. | Removal of wildcard CORS headers; enforce `SameSite=Strict` cookies and custom header validation (`X-TurboShare-Auth`). |
| **A06:2021 — Vulnerable Components** | **0.0 (None)** | Dependency vulnerabilities in third-party web frameworks. | Supply-chain attacks on dependencies. | Zero external dependencies; 100% Python standard library implementation (`http.server`, `hashlib`, `hmac`, `secrets`). |
| **A07:2021 — Auth & Identification Failures** | **8.1 (High)** | Lack of rate limiting and exponential delay tarpitting against dictionary/brute-force attacks. | Botnet sprays thousands of passwords against public tunnel within seconds. | In-memory sliding-window rate limiter (5 failed attempts per 15 min) with exponential tarpitting ($1\text{s} \to 16\text{s}$). |
| **A08:2021 — Software & Data Integrity** | **9.1 (Critical)** | Unrestricted file uploads allowing script drop into system startup folders. | Attacker uploads `.bat` / `.vbs` to Windows Startup folder via manipulated upload path. | Strict path validation (`safe_path`), extension screening, and sandboxing against directory traversal. |
| **A09:2021 — Logging & Monitoring Failures** | **5.3 (Medium)** | Complete suppression of security event logging (`log_message = lambda ...: None`). | Unauthorized access attempts occur without leaving an audit trail. | Structured security event logging for auth failures, rate-limit triggers, path changes, and anomalous file transfers. |
| **A10:2021 — Server-Side Request Forgery & DoS** | **7.8 (High)** | Unbounded file uploads without disk space verification leading to hard drive exhaustion. | Attacker uploads 500GB stream, exhausting host disk space and crashing OS subsystem services. | Pre-flight disk space verification (`shutil.disk_usage`) with 1GB safety margin and global file size caps. |

---

## 4. Deep-Dive Analysis of Vibecoding Security Hazards

"Vibecoding" refers to rapid AI-assisted development focused on immediate functional aesthetics and user flow, often neglecting adversarial boundary conditions and threat modeling:

### 4.1 Host Username & Absolute Path Disclosure
* **Hazard**: String interpolation substituting `os.path.expanduser("~")` or `UPLOAD_DIR` directly into client HTML (e.g. `C:\Users\Username\Downloads\TurboShare`).
* **Vulnerability**: Unauthenticated internet visitors learn the host OS, username (`Username`), directory structure, and volume configuration.
* **Hardening**: Virtualize paths in client responses (e.g. `[Inbox Storage]`, `[Library Storage]`). Mask absolute host paths from all unauthenticated or guest responses.

### 4.2 Accidental High-Privilege Endpoint Exposure
* **Hazard**: Exposing `/api/browse_host` (full OS filesystem navigation) and `/api/set_path` (arbitrary root folder modification) without authorization checks.
* **Exploit Chain**:
  1. Attacker calls `POST /api/set_path` with `{"target": "share", "path": "C:\\"}`.
  2. Server updates `HOST_SHARE = "C:\\"`.
  3. Attacker downloads `C:\Windows\System32\drivers\etc\hosts` or `.ssh\id_rsa` via `/download`.
* **Hardening**: RBAC enforcement. High-privilege endpoints strictly require an active, authenticated Admin session.

### 4.3 Side-Channel Timing Attacks in Credential Evaluation
* **Hazard**: Using standard Python string comparison `if user_password == MASTER_PASSWORD:` which terminates on the first non-matching byte.
* **Vulnerability**: An attacker measuring latency over thousands of requests can determine each character of the password sequentially.
* **Hardening**: Mandatory use of `hmac.compare_digest()` for all password, access key, and session signature comparisons.

### 4.4 Large Payload Denial of Service & Heap Memory Exhaustion
* **Hazard**: `/api/zip` buffering entire folder contents in an in-memory `io.BytesIO()` buffer before sending response headers.
* **Vulnerability**: Requesting a zip of a 10 GB directory causes Python to allocate 10+ GB on the heap, triggering immediate `MemoryError` and crashing the server for all users.
* **Hardening**: Streaming chunked ZIP generation writing directly to the output socket, bounded recursion depth ($\le 20$), and hard limits on total compressed volume.

### 4.5 Universal Wildcard CORS & CSRF Hazards
* **Hazard**: Sending `Access-Control-Allow-Origin: *` unconditionally on all API responses.
* **Vulnerability**: Any malicious website visited by the host user can issue background `fetch()` calls to `http://127.0.0.1:8080/api/browse_host` and exfiltrate private files.
* **Hardening**: Remove wildcard CORS headers; enforce `SameSite=Strict; HttpOnly; Secure` cookies.

### 4.6 File Path Normalization, Symlinks, NTFS ADS, and Windows Device Names
* **Hazard**: Path traversal via URL encoding (`%2e%2e%2f`), null bytes (`%00`), NTFS Alternate Data Streams (`file.txt::$DATA`), UNC paths (`\\?\C:\`), or Windows reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).
* **Hardening**: Canonical path resolution via `os.path.realpath`, verification that `os.path.commonpath([base, target]) == base`, explicit rejection of null bytes, stripping of NTFS stream colons, and validation against Windows device names.

---

## 5. Architectural Specification: Hardened Persistent Global Authentication

### 5.1 Salted PBKDF2-HMAC-SHA256 Password Derivation
* **Algorithm**: `PBKDF2-HMAC-SHA256`
* **Iterations**: $600,000$ (OWASP 2024/2026 standard)
* **Salt**: $16$ bytes cryptographically secure random bytes (`secrets.token_bytes(16)`)
* **Derived Key Length**: $32$ bytes ($256$ bits)
* **Storage Format in `.env`**:
  ```ini
  TURBOSHARE_PASSWORD_HASH=pbkdf2_sha256$600000$<salt_hex>$<dk_hex>
  TURBOSHARE_SECRET_KEY=<64_char_hex_secret>
  TURBOSHARE_ACCESS_KEY=ts_live_<urlsafe_token>
  ```
* **Verification**: Evaluated strictly via `hmac.compare_digest(actual_dk, expected_dk)` with constant-time exception safety.

### 5.2 Stateless Cryptographically Signed Persistent Sessions
* **Token Structure**:
  $$\text{Token} = \text{Version} \,\|\, \text{SessionID} \,\|\, \text{IssuedAt} \,\|\, \text{ExpiresAt} \,\|\, \text{UserAgentFingerprint} \,\|\, \text{HMAC-SHA256 Signature}$$
* **Parameters**:
  - `Version`: `v1`
  - `SessionID`: 16 bytes random hex (`secrets.token_hex(16)`)
  - `IssuedAt`: Current epoch timestamp integer
  - `ExpiresAt`: $\text{IssuedAt} + 2,592,000$ ($30$ days)
  - `UserAgentFingerprint`: Truncated SHA-256 hash of `User-Agent` ($16$ characters)
  - `HMAC Signature`: $\text{HMAC-SHA256}_{\text{SECRET\_KEY}}(\text{Payload})$
* **Cookie Flags**:
  ```http
  Set-Cookie: turboshare_session=v1.e3b0c44298fc1c149afbf4c8996fb924.1772314000.1774906000.8a3f9d12.3c8b...; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict; Secure
  ```
* **Crash Resilience**: Because the session token is statelessly signed with `TURBOSHARE_SECRET_KEY`, sessions persist seamlessly across server reboots without database dependency.

### 5.3 URL-Safe Bookmarked Key Auto-Login (303 PRG Protocol)
To enable mobile smartphone access without requiring password entry on touchscreens, TurboShare supports a zero-leak bookmark authentication protocol:
1. Mobile user navigates to bookmarked URL: `GET /api/auth?key=ts_live_...`
2. Server validates key in constant time via `hmac.compare_digest`.
3. If valid, server sets `turboshare_session` cookie and issues `HTTP 303 See Other` to `Location: /`.
4. Server includes `Referrer-Policy: no-referrer` and `Cache-Control: no-store, no-cache`.
5. Browser stores the cookie and navigates to clean root `/`, immediately scrubbing the secret access key from browser history, address bar, and outgoing referer headers.

### 5.4 Sliding-Window Rate Limiter & Exponential Tarpitting
* **Sliding Window**: $900$ seconds ($15$ minutes)
* **Threshold**: $5$ failed attempts per client IP
* **Exponential Tarpit Delay**:
  $$D(k) = \min(1.0 \times 2^{k-1}, 16.0) \text{ seconds for attempt } k \in [1, 5]$$
  - Failure 1: $1.0\text{s}$ delay $\to$ HTTP 401
  - Failure 2: $2.0\text{s}$ delay $\to$ HTTP 401
  - Failure 3: $4.0\text{s}$ delay $\to$ HTTP 401
  - Failure 4: $8.0\text{s}$ delay $\to$ HTTP 401
  - Failure 5: $16.0\text{s}$ delay $\to$ HTTP 401
  - Failure $\ge 6$: Immediate HTTP 429 Too Many Requests (`Retry-After: <seconds>`)
* **Success Reset**: Valid authentication immediately resets the failure history for the client IP.

### 5.5 Isolated Trust-Aware Client IP Extraction
```python
def extract_client_ip(handler, trust_proxy_headers=False) -> str:
    peer_ip = handler.client_address[0]
    if not trust_proxy_headers:
        return peer_ip
    
    # Only inspect proxy headers if peer is verified loopback
    if peer_ip in ("127.0.0.1", "::1", "localhost"):
        cf_ip = handler.headers.get("CF-Connecting-IP")
        if cf_ip and _is_valid_ip(cf_ip.strip()):
            return cf_ip.strip()
        xff = handler.headers.get("X-Forwarded-For")
        if xff:
            hops = [h.strip() for h in xff.split(",") if h.strip()]
            if hops and _is_valid_ip(hops[0]):
                return hops[0]
        x_real = handler.headers.get("X-Real-IP")
        if x_real and _is_valid_ip(x_real.strip()):
            return x_real.strip()
            
    return peer_ip
```

### 5.6 Privilege Hierarchy & Role-Based Access Control (RBAC)

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              PRIVILEGE MATRIX                               │
├───────────────────┬──────────────┬──────────────┬─────────────┬─────────────┤
│ Endpoint / Action │ Public Unauth│ Guest Session│ Admin Authed│ Direct Host │
├───────────────────┼──────────────┼──────────────┼─────────────┼─────────────┤
│ / (Web Dashboard) │ Login View   │ Shared View  │ Full View   │ Full View   │
│ /api/auth (Login) │ Allowed      │ Allowed      │ Allowed     │ Allowed     │
│ /api/list         │ Blocked(401) │ Sandboxed    │ Sandboxed   │ Sandboxed   │
│ /download         │ Blocked(401) │ Sandboxed    │ Sandboxed   │ Sandboxed   │
│ /api/upload       │ Blocked(401) │ Sandboxed    │ Sandboxed   │ Sandboxed   │
│ /api/zip          │ Blocked(401) │ Sandboxed    │ Sandboxed   │ Sandboxed   │
│ /api/browse_host  │ Blocked(401) │ Blocked(403) │ Allowed*    │ Allowed     │
│ /api/set_path     │ Blocked(401) │ Blocked(403) │ Allowed     │ Allowed     │
│ /api/create_folder│ Blocked(401) │ Sandboxed    │ Allowed     │ Allowed     │
│ /api/open_folder  │ Blocked(401) │ Web View(200)│ Web View    │ OS Explorer │
│ /api/pick_folder  │ Blocked(401) │ Blocked(403) │ Web Fallback│ OS Dialog   │
└───────────────────┴──────────────┴──────────────┴─────────────┴─────────────┘
* Full drive browsing (/api/browse_host) over remote tunnel requires explicit ALLOW_FULL_DRIVE_REMOTE=true.
```

---

## 6. Formal Mathematical Security Invariants

### Invariant 1: Zero Unauthenticated Exposure
$$\forall \text{Route } R \notin \{\text{Static Assets}, \text{/api/auth}, \text{/login}\}: \text{ValidSession}(\text{Cookie}) = \text{False} \implies \text{Status}(R) \in \{401, 403\}$$
*Guarantees no internal files, drive metrics, or directory structures can be read without a cryptographically verified session.*

### Invariant 2: Strict Path Confinement
$$\forall P \in \text{Paths}, \forall B \in \text{Bases}: \text{safe\_path}(B, P) = T \implies \text{commonpath}([B, T]) = B \land \text{NullBytes}(P) = 0 \land \text{NTFS\_ADS}(P) = 0$$
*Guarantees no request can escape designated base directories regardless of encoding tricks, alternate data streams, or symbolic links.*

### Invariant 3: Constant-Time Verification
$$\forall S_1, S_2 \in \Sigma^*: \text{ExecutionTime}(\text{verify}(S_1, S_2)) = C \pm \varepsilon$$
*Guarantees side-channel timing attacks cannot leak credential bytes or signature bytes.*

### Invariant 4: Non-Repudiable Rate Bounding
$$\forall \text{IP } I, \Delta t \le 900\text{s}: \text{FailedAttempts}(I, \Delta t) \ge 6 \implies \text{Status}(I) = 429 \land \text{TarpitDelay}(I) \ge 16.0\text{s}$$
*Guarantees brute-force dictionary attacks cannot exceed 480 attempts per 24 hours per IP.*

### Invariant 5: Stateless Crash Recovery
$$\text{VerifyToken}(T, \text{SecretKey}) = \text{True} \iff T \text{ is valid and unexpired}, \quad \text{independent of server process restarts}$$
*Guarantees sessions persist across reboots without database corruption or in-memory session loss.*

### Invariant 6: Memory-Bounded Compression Stream
$$\forall \text{Dir } D \text{ with size } S(D): \text{HeapAllocation}(\text{ZipStream}(D)) \le M_{\text{chunk}} \ll S(D)$$
*Guarantees ZIP generation operates in $O(1)$ memory overhead, eliminating out-of-memory denial of service.*

---

## 7. Penetration Testing Test Suite Specification (Categories A–F)

### Category A: Authentication & Access Control Integrity

#### Test Case SEC-AUTH-001: Missing Authentication Token Rejection
* **Objective**: Verify unauthenticated requests to protected endpoints return HTTP 401.
* **Input**: `GET /api/browse_host`, `GET /api/list?tab=recv`, `POST /api/set_path` with empty cookies.
* **Expected Output**: HTTP Status `401 Unauthorized`, JSON `{"error": "unauthorized", "login_required": true}`.
* **CLI Verification**:
  ```bash
  curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8080/api/browse_host
  # Expected: 401
  ```

#### Test Case SEC-AUTH-002: Forged HMAC Session Signature Rejection
* **Objective**: Verify tampered session token payloads are rejected.
* **Input**: Cookie `turboshare_session=v1.forged_session.1772314000.1774906000.8a3f9d12.0000000000000000000000000000000000000000000000000000000000000000`.
* **Expected Output**: HTTP Status `401 Unauthorized`.
* **CLI Verification**:
  ```bash
  curl -s -b "turboshare_session=v1.forged.1772314000.1774906000.8a3f9d12.000000" http://127.0.0.1:8080/api/list
  # Expected: 401
  ```

#### Test Case SEC-AUTH-003: Expired Session Token Rejection
* **Objective**: Verify session tokens with timestamps past expiration are rejected.
* **Input**: Valid HMAC token with `expires_at = now - 3600`.
* **Expected Output**: HTTP Status `401 Unauthorized`.

#### Test Case SEC-AUTH-004: URL-Safe Bookmarked Key Auto-Login & 303 Redirect
* **Objective**: Verify bookmarked access key generates session cookie and executes 303 PRG clean redirect.
* **Input**: `GET /api/auth?key=VALID_ACCESS_KEY`
* **Expected Output**:
  - HTTP Status `303 See Other`
  - Header `Location: /`
  - Header `Set-Cookie: turboshare_session=...; HttpOnly; SameSite=Strict`
  - Header `Referrer-Policy: no-referrer`
  - Header `Cache-Control: no-store, no-cache`
* **CLI Verification**:
  ```bash
  curl -s -I "http://127.0.0.1:8080/api/auth?key=ts_live_secret" | grep -E "(303 See Other|Set-Cookie|Location: /)"
  ```

#### Test Case SEC-AUTH-005: Invalid Access Key Rejection & Tarpit
* **Objective**: Verify invalid access key fails and incurs tarpit delay.
* **Input**: `GET /api/auth?key=INVALID_KEY_GUESS`
* **Expected Output**: HTTP Status `401 Unauthorized`, response latency $\ge 1.0\text{s}$.

---

### Category B: Filesystem Traversal & Path Confinement

#### Test Case SEC-TRAV-001: Standard Dot-Dot Directory Traversal (`../`)
* **Objective**: Verify relative directory traversal attempts cannot escape the active folder.
* **Input**: `GET /download?tab=recv&path=../../../../Windows/win.ini`
* **Expected Output**: HTTP Status `403 Forbidden` or `404 Not Found`.

#### Test Case SEC-TRAV-002: URL-Encoded Traversal Sequences
* **Objective**: Verify encoded traversal tokens (`%2e%2e%2f`, `%252e%252e%252f`) are decoded and blocked.
* **Input**: `GET /download?tab=recv&path=%2e%2e%2f%2e%2e%2fWindows%2fwin.ini`
* **Expected Output**: HTTP Status `403 Forbidden` or `404 Not Found`.

#### Test Case SEC-TRAV-003: Null Byte Poisoning (`%00`)
* **Objective**: Verify null bytes injected into path parameters are rejected prior to filesystem invocation.
* **Input**: `GET /download?tab=recv&path=safe_file.png%00.exe`
* **Expected Output**: HTTP Status `400 Bad Request` or `403 Forbidden`.

#### Test Case SEC-TRAV-004: NTFS Alternate Data Streams (ADS)
* **Objective**: Verify NTFS stream access (`::$DATA`) is neutralized.
* **Input**: `GET /download?tab=recv&path=sensitive.txt::$DATA`
* **Expected Output**: HTTP Status `403 Forbidden` or `404 Not Found`.

#### Test Case SEC-TRAV-005: UNC Path Traversal Injection
* **Objective**: Verify Windows Universal Naming Convention (UNC) paths cannot traverse network shares or escape volume roots.
* **Input**: `POST /api/set_path` with `{"path": "\\\\10.0.0.1\\c$\\Windows"}`
* **Expected Output**: HTTP Status `400 Bad Request` or `403 Forbidden`.

---

### Category C: Brute-Force, Tarpitting & Rate-Limiter Evasion

#### Test Case SEC-RATE-001: Exponential Delay Tarpitting Verification
* **Objective**: Verify progressive delay penalties on consecutive failed login attempts.
* **Input**: 5 failed login attempts from a single IP.
* **Expected Latencies**:
  - Attempt 1: $[1.0\text{s}, 1.5\text{s}] \to 401$
  - Attempt 2: $[2.0\text{s}, 2.5\text{s}] \to 401$
  - Attempt 3: $[4.0\text{s}, 4.5\text{s}] \to 401$
  - Attempt 4: $[8.0\text{s}, 8.5\text{s}] \to 401$
  - Attempt 5: $[16.0\text{s}, 16.5\text{s}] \to 401$

#### Test Case SEC-RATE-002: Lockout Enforcement (HTTP 429)
* **Objective**: Verify 6th failed login within 15 minutes is blocked immediately.
* **Input**: 6th failed request.
* **Expected Output**: HTTP Status `429 Too Many Requests`, Header `Retry-After: <seconds>`.

#### Test Case SEC-RATE-003: Rate Counter Reset on Valid Authentication
* **Objective**: Verify successful authentication clears failure counter for the client IP.
* **Input**: 3 failed attempts, 1 successful login, followed by 1 failed attempt.
* **Expected Latency**: Subsequent failure incurs base $1.0\text{s}$ delay rather than 4th-level delay.

---

### Category D: Proxy Header Spoofing & Network Isolation

#### Test Case SEC-PROXY-001: Untrusted Direct Socket Header Spoofing
* **Objective**: Verify client sending `X-Forwarded-For: 127.0.0.1` on direct LAN connection is identified by actual physical IP.
* **Input**: Socket from `192.168.1.100` sending `X-Forwarded-For: 127.0.0.1`.
* **Expected Resolution**: Client IP is identified as `192.168.1.100`.

#### Test Case SEC-PROXY-002: Rotating Header Evasion Defense
* **Objective**: Verify attacker mutating `X-Forwarded-For` per request cannot bypass rate limiting when direct proxy headers are untrusted.
* **Input**: 6 failed attempts from same physical connection mutating `X-Forwarded-For: 10.0.0.1` ... `10.0.0.6`.
* **Expected Output**: 6th attempt is blocked with HTTP 429.

#### Test Case SEC-PROXY-003: Legitimate Localhost Tunnel Header Extraction
* **Objective**: Verify that when `TRUST_PROXY_HEADERS=true` and socket is `127.0.0.1`, `CF-Connecting-IP` is extracted correctly.
* **Input**: Socket from `127.0.0.1` sending `CF-Connecting-IP: 203.0.113.195`.
* **Expected Resolution**: Client IP resolved to `203.0.113.195`.

---

### Category E: Denial of Service, Large Payloads & Stream Exhaustion

#### Test Case SEC-DOS-001: Large Upload Disk Quota Pre-Flight Check
* **Objective**: Verify uploads exceeding free disk space safety margin are rejected with HTTP 507.
* **Input**: Chunk upload exceeding free drive capacity.
* **Expected Output**: HTTP Status `507 Insufficient Storage`.

#### Test Case SEC-DOS-002: Memory-Bounded ZIP Stream Verification
* **Objective**: Verify ZIP compression streams directly without loading full archive into RAM.
* **Input**: Download ZIP of multi-gigabyte directory structure.
* **Expected Invariant**: Heap memory delta during transfer remains $< 10\text{ MB}$.

---

### Category F: Host OS Integration & Privilege Sandboxing

#### Test Case SEC-PRIV-001: Remote Explorer Spawning Neutralization
* **Objective**: Verify remote clients cannot trigger `explorer.exe` or GUI spawning on host PC.
* **Input**: Remote tunnel client calls `POST /api/open_folder`.
* **Expected Output**: Server does not launch subprocess; returns JSON `{"status": "ok", "is_local": false, "message": "Folder opened on host PC; viewing in browser"}`.

#### Test Case SEC-PRIV-002: Remote Drive Browser Sandboxing
* **Objective**: Verify remote clients cannot browse arbitrary drive roots (`C:\`) unless `ALLOW_FULL_DRIVE_REMOTE=true`.
* **Input**: Remote client calls `/api/browse_host?path=C:\Windows`.
* **Expected Output**: HTTP Status `403 Forbidden` or sandboxed view restricted to shared folder.

---

## 8. Command-Line Verification Manual

The following bash / PowerShell commands provide independent terminal verification:

```bash
# 1. Verify Unauthenticated Root API Rejection
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8080/api/browse_host
# Must output: 401

# 2. Verify Path Traversal Defense
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8080/download?tab=recv&path=../../../../Windows/win.ini"
# Must output: 401 (if unauthed) or 403/404 (if authed)

# 3. Verify Null-Byte Rejection
curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8080/download?tab=recv&path=test.txt%00.png"
# Must output: 400 or 403

# 4. Verify URL-Safe Auto-Login & 303 Clean Redirect
curl -s -I "http://127.0.0.1:8080/api/auth?key=ts_live_testkey"
# Must return: HTTP/1.1 303 See Other, Location: /, Set-Cookie: turboshare_session=...

# 5. Verify Rate-Limiting Lockout (Run 6 invalid attempts)
for i in {1..6}; do
  curl -s -o /dev/null -w "%{http_code}\n" "http://127.0.0.1:8080/api/auth?key=badkey_$i"
done
# 6th output must be: 429

# 6. Execute Full Automated Python Penetration Test Suite
python test_security.py
```

---

## 9. Security Invariants, Operational Policies & Remote Drive Access Liability Disclaimers

### 9.1 Multi-User & Zero-Trust Deployment Guidelines
When hosting TurboShare on untrusted public networks or exposing it globally:
1. **Never Expose Plaintext Passwords**: Ensure `TURBOSHARE_PASSWORD_HASH` in `.env` is a salted PBKDF2 hash.
2. **Tunnel Authentication Layering**: For sensitive environments, place the reverse tunnel behind an edge identity provider (e.g. Cloudflare Access with Email OTP / GitHub OAuth).
3. **Dedicated Storage Partitioning**: Designate a dedicated non-system directory (e.g. `D:\TurboShare`) for transfers rather than drive roots (`C:\`).

### 9.2 Remote Drive Access Liability Disclaimer

> **IMPORTANT OPERATIONAL & LIABILITY NOTICE**:  
> TurboShare provides high-performance file transfer and remote file management capabilities. Operating TurboShare with public reverse tunnels (Cloudflare Tunnel, Pinggy, ngrok) grants authorized remote sessions access to configured directories on the host operating system.  
> 
> The system administrator / host PC operator assumes sole legal and technical responsibility for:
> - Safeguarding master authentication passphrases, secret encryption keys, and bookmarked access URLs.
> - Verifying directory permissions for configured receive (`UPLOAD_DIR`) and share (`HOST_SHARE`) directories.
> - Enabling or disabling full drive navigation (`ALLOW_FULL_DRIVE_REMOTE`).
> - Complying with applicable data protection, privacy, and cybersecurity regulations.
> 
> TurboShare, its authors, and contributors disclaim all liability for any direct, indirect, incidental, or consequential damages resulting from unauthorized access, credential disclosure, data loss, disk exhaustion, or system compromise arising from misconfigured tunnels or compromised client endpoints.

---
*End of TurboShare Adversarial Cybersecurity Audit Specification.*
