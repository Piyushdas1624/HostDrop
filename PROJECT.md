# Project: HostDrop Universal Cross-Platform Transformation

## Architecture
HostDrop is a universal, 100% cross-platform high-speed cross-device file transfer engine and local/remote access hub running across Windows, Linux, macOS, and Android Termux with zero external dependencies.
- **Core Server & Engine**: `hostdrop.py` (Single-file multi-threaded HTTP server, OS drive & filesystem abstraction, cross-platform file manager integration, tunnel manager, embedded UI).
- **Authentication & Security Engine**: `auth.py` (PBKDF2-HMAC-SHA256 password hashing, rate limiting, session management, strict localhost sandbox isolation).
- **Shell Launchers**: `run_hostdrop.sh` (POSIX universal launcher for Linux, macOS, and Android Termux) & `Run_HostDrop.bat` (Windows launcher).
- **Packaging**: PEP 517/621 `pyproject.toml` and `setup.py` packaging `hostdrop` and `auth` modules with `hostdrop = hostdrop:main` CLI console entrypoint.
- **Testing & Verification**: `test_hostdrop.py` (functional & platform tests), `test_security.py` (adversarial penetration tests), and cross-platform mock verification.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Termux Detection & Mounts | Detect Android Termux (`$TERMUX_VERSION`, `/data/data/com.termux`, `$PREFIX`) and expose `/sdcard`, `~/storage/shared`, `~/storage/downloads`, and Termux home `~`. | M1 | Survey §R1 |
| 2 | Dynamic Drive & Mount Navigation | Windows dynamic volume labels via `GetVolumeInformationW`; Linux & macOS `/`, `~`, `/Volumes`, `/media`, `/run/media`, `/mnt`; correct POSIX badges and "Up" button navigation. | M1 | Survey §R1 |
| 3 | Cross-Platform OS File Explorer | Launch `explorer.exe` (Windows), `open` (macOS), `xdg-open` (Linux), `termux-open` (Termux). Maintain strict `is_physical_localhost()` 403 Forbidden sandboxing against remote tunnels. | M1 | Survey §R1 |
| 4 | Native Folder Picker & Tunnel Discovery | Abstract `pick_folder_native()` (Windows, macOS osascript, Linux zenity/kdialog). Detect cloudflared in Linux, macOS Homebrew, Termux, Windows paths. Harden Pinggy OpenSSH (`-o UserKnownHostsFile=/dev/null -T`). | M1 | Survey §R1 |
| 5 | Platform Default Inbox Folders | Default directory detection: Windows `D:\HostDrop` or `Downloads`, Linux/macOS `~/HostDrop`, Android Termux `/sdcard/HostDrop`. | M1 | Survey §R1 |
| 6 | Universal POSIX Shell Launcher | Create `run_hostdrop.sh` for Linux, macOS, Android Termux with Python detection, receive folder prompt, browser launch (`termux-open-url`, `open`, `xdg-open`), and executable mode. | M2 | Survey §R2 |
| 7 | Windows Batch Launcher Maintenance | Retain and ensure `Run_HostDrop.bat` operates seamlessly with `hostdrop.py`. | M2 | Survey §R2 |
| 8 | Modern Pip Packaging (PEP 517/621) | Create `pyproject.toml` and `setup.py` for `hostdrop` v1.0.0, packaging `hostdrop` and `auth` with 0 required dependencies and optional extras (`qrcode[pil]`, `psutil`). | M3 | Survey §R3 |
| 9 | Universal Pure Python Wheel | Build `hostdrop-1.0.0-py3-none-any.whl` installable via `pip install .` or `pip install hostdrop`. | M3 | Survey §R3 |
| 10 | Global CLI Console Entrypoint | Console script `hostdrop = hostdrop:main` providing global CLI access with arguments and default configurations. | M3 | Survey §R3 |
| 11 | Git Remote Synchronization | Update git remote origin to `https://github.com/Piyushdas1624/hostdrop.git`. | M4 | Survey §R4 |
| 12 | Multi-Platform Documentation in README | Update `README.md` with multi-platform quickstart (Windows, Linux & macOS, Android Termux), PyPI & platform badges, and repository URLs. | M4 | Survey §R4 |
| 13 | Cross-Platform Branch Mock Test Suite | Construct mock unit tests verifying Windows, Linux, macOS, and Termux drive discovery, explorer dispatch, and tunnel discovery on Windows without physical hardware. | M5 | Survey §R5 |
| 14 | Zero-Regression Verification & Forensic Audit | Verify all 66+ existing baseline tests in `test_hostdrop.py` and `test_security.py` pass with 0 errors and independent forensic integrity validation. | M5 | Survey §R5 |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| 1 | M1: Universal Cross-Platform Host Engine | Abstract OS specifics in `hostdrop.py`: Termux detection, filesystem/drive navigation, OS file explorer spawner, 403 tunnel sandbox, cross-platform tunnels, platform default inboxes. | none | DONE |
| 2 | M2: Universal Shell Launchers | Create `run_hostdrop.sh` for Linux/macOS/Termux with Python detection, inbox folder selection, and browser launch. Maintain `Run_HostDrop.bat`. | M1 | DONE |
| 3 | M3: Modern Pip Packaging & CLI Entrypoint | Create PEP 517/621 `pyproject.toml` and `setup.py`, `hostdrop = hostdrop:main` entrypoint, pure Python wheel build, zero required dependencies. | M1 | DONE |
| 4 | M4: Repository Remote & Cross-Platform Documentation | Update git remote origin to `https://github.com/Piyushdas1624/hostdrop.git`, update `README.md` quickstarts (Win, Linux/Mac, Termux), badges, and URLs. | none | DONE |
| 5 | M5: Multi-Platform Verification & Test Audit | Add cross-platform mock unit tests, verify `pip install .` and `hostdrop` CLI launch, verify all 66+ tests pass, and complete forensic audit. | M1, M2, M3, M4 | IN_PROGRESS |

## Interface Contracts
### `hostdrop.py` Engine & OS Abstractions
- `is_termux() -> bool`: Returns `True` if running in Android Termux environment.
- `get_host_drives() -> list[dict]`:
  - Each item: `{"name": str, "label": str, "letter": str, "path": str, "total": int, "used": int, "free": int, "percent": float, "is_system": bool}`.
  - Windows: letters `"C"`, `"D"`, dynamic volume labels (`"Work (D:)"`), drive capacity.
  - Linux & macOS: `"/"` (`"Root (/)"`), `"~"` (`"Home (~)"`), `/Volumes/*`, `/media/*`, `/run/media/$USER/*`, `/mnt/*`.
  - Android Termux: `"/sdcard"` (`"Internal Storage (/sdcard)"`), `~/storage/shared`, `~/storage/downloads`, Termux home `~`.
- `open_in_os_explorer(target_dir: str, client_ip: str) -> dict`:
  - Enforces `is_physical_localhost(client_ip)`. Returns 403 Forbidden with `"viewing in browser"` message for remote clients.
  - Local clients: Spawns `explorer.exe` (Win), `open` (Mac), `termux-open` (Termux), `xdg-open` (Linux).
- `TunnelManager.get_cloudflared_path() -> Optional[str]`:
  - Searches standard Linux, macOS Homebrew, Termux, and Windows paths.
- `pick_folder_native() -> tuple[Optional[str], Optional[str]]`:
  - Windows PowerShell/Tkinter, macOS osascript/Tkinter, Linux zenity/kdialog/Tkinter, Termux fallback. Aliased to `pick_folder_powershell`.
- `main()`:
  - Console script entry point. Supports CLI flags (`-h`, `--help`, `--port`, `--tunnel`, `[folder]`).
  - Default folder: Termux `/sdcard/HostDrop`, Linux/macOS `~/HostDrop`, Windows `D:\HostDrop` if available else `Downloads`.

### Packaging & Launchers
- `pyproject.toml` specifies `tool.setuptools.py-modules = ["hostdrop", "auth"]` and `project.scripts.hostdrop = "hostdrop:main"`.
- `run_hostdrop.sh`: POSIX shell script, launches `python3 hostdrop.py [folder]` or `python hostdrop.py [folder]`.

## Code Layout
- `hostdrop.py`: Universal host server, filesystem browser, drive manager, OS integration, HTTP request routing.
- `auth.py`: Cryptographic authentication, rate limiting, session registry, localhost isolation filter.
- `run_hostdrop.sh`: POSIX shell launcher for Linux, macOS, and Android Termux.
- `Run_HostDrop.bat`: Windows batch launcher.
- `pyproject.toml`: Modern PEP 517/621 packaging metadata and configuration.
- `setup.py`: Standard setuptools entrypoint for backward compatibility.
- `README.md`: Cross-platform documentation, quickstart guides, and repository links.
- `test_hostdrop.py`: Functional, API, UI, and cross-platform mock tests.
- `test_security.py`: Security and penetration tests.
