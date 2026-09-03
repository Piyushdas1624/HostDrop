# Project: TurboShare Security & Memorable Passcode Upgrade

## Architecture
TurboShare is a high-speed cross-device file transfer hub with built-in encrypted global remote access (Cloudflare Tunnels and Pinggy SSH fallback).
- **Core Server & UI**: `turboshare.py` (Single-file multi-threaded HTTP server with embedded dark-mode UI templates, SSE file progress, and system tray integration).
- **Authentication & Cryptography**: `auth.py` (PBKDF2-HMAC-SHA256 password hashing with 600,000 iterations, sliding-window rate limiting with exponential tarpitting, HMAC-SHA256 session tokens with `.sessions.json` revocation registry, and `.env` persistence).
- **Testing Infrastructure**: `test_turboshare.py` (functional and UI tests) and `test_security.py` (adversarial security and penetration tests).
- **Documentation**: `README.md` (installation, architecture, remote tunneling, and offline LAN guarantees).

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Memorable Passcode Generator | Generate 8–10+ char `word-word-NN` passcode (e.g. `star-falcon-42`) using CSPRNG (`secrets`) with $\ge 30.0$ bits entropy, excluding ambiguous glyphs `0/O/1/l/I`. | M1 | ORIGINAL_REQUEST §R1 |
| 2 | PBKDF2 & Dual Persistence | Salted PBKDF2-HMAC-SHA256 (600,000 iterations) hash and dual `.env` persistence (`TURBOSHARE_PASSCODE` + `TURBOSHARE_PASSWORD_HASH`) on first launch only; preserve on reboot; custom passwords take precedence. | M1 | ORIGINAL_REQUEST §R1 |
| 3 | Banner & Dashboard Passcode Display | Display active memorable passcode in startup terminal banner and host dashboard header/modal for easy copying. | M1 | ORIGINAL_REQUEST §R1 |
| 4 | Non-Intrusive Passcode Tip | Add friendly recommendation tip in `#securityModal` and terminal banner: *"Tip: We recommend setting your own personal passcode, though your auto-generated code is active and secure."* with 0 annoying popups. | M2 | ORIGINAL_REQUEST §R2 |
| 5 | Transparent Tunneling Guide | Comprehensive README update: Cloudflare Tunnel auto-initialization, Pinggy SSH zero-download fallback with Windows `ssh.exe`, `winget install --id Cloudflare.cloudflared`, and 100% Offline LAN Guarantee. | M3 | ORIGINAL_REQUEST §R3 |
| 6 | Adversarial Penetration Tests | Verify passcode entropy/tarpitting, HMAC sessions, host isolation against proxy headers, NTFS traversal defenses, and add automated test cases in `test_security.py` (all 37 tests pass). | M4 | ORIGINAL_REQUEST §R4 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | M1: Memorable Passcode Engine & Persistence | Implement `word-word-NN` generator, 600k PBKDF2 iterations, `.env` persistence & reload, terminal banner display, and host-only passcode retrieval endpoint. | none | DONE |
| 2 | M2: Non-Intrusive Passcode Change Encouragement (UI & Banner) | Embed Active Passcode card, 1-click clipboard copy, and non-intrusive tip banner in `#securityModal` & terminal banner with zero emojis. | M1 | DONE |
| 3 | M3: Transparent Tunneling & Fallback Documentation | Update `README.md` with Cloudflare Tunnel auto-init, Pinggy SSH zero-config fallback, `winget` installation, and 100% Offline LAN Guarantee. | none | DONE |
| 4 | M4: Adversarial Security Audit & Automated Test Cases | Add automated tests in `test_security.py` covering memorable passcode format, entropy, `.env` persistence, host isolation, and NTFS traversal safety. | M1, M2, M3 | DONE |

## Interface Contracts
### `auth.py` ↔ `turboshare.py`
- `auth.generate_passcode() -> str`: Returns `word-word-NN` format string (e.g. `star-falcon-42`), suffix digits in `[2-9]`, no `0/O/1/l/I`.
- `auth.SecurityConfig`:
  - `self.raw_password`: Populated from `.env` key `TURBOSHARE_PASSCODE` if present, or newly generated passcode on first launch.
  - `self.password_hash`: Salted `pbkdf2_sha256$600000$<salt>$<dk>`.
  - `auth.get_master_password() -> str`: Returns `self.raw_password` or `"[Stored hashed in .env]"`.
- `turboshare.py`:
  - Injects `auth.get_master_password()` only for `is_physical_localhost()`.
  - Serves `GET /api/host_security_info` (403 Forbidden for remote/tunnels).
- `auth.is_physical_localhost(handler) -> bool`:
  - Enforces fail-closed socket check (`127.*`, `::1`, `localhost`, `::ffff:127.*`).
  - Blocks reverse proxy headers (`Forwarded`, `CF-Connecting-IP`, `X-Forwarded-For`, `X-Real-IP`, `True-Client-IP`) case-insensitively.

## Code Layout
- `auth.py`: Password generation, PBKDF2 hashing, `.env` persistence, rate limiter, session token generation/verification, host isolation perimeter.
- `turboshare.py`: HTTP request handling, host isolation verification (`is_physical_localhost()`), HTML/CSS/JS template rendering, terminal startup banner.
- `README.md`: User documentation, quickstart, tunneling architecture, security overview, offline LAN guarantees.
- `test_security.py`: Security and penetration unit tests (37 tests).
- `test_turboshare.py`: Functional, API, and UI regression tests (29 tests).
