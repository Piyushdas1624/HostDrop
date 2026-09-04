import os
import sys
import io
import json
import socket
import shutil
import zipfile
import tempfile
import mimetypes
import urllib.parse
import html
import subprocess
import threading
import time
import datetime
import string
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ThreadingHTTPServer.allow_reuse_address = True

# ── Force UTF-8 Console Output on Windows ──────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def is_termux() -> bool:
    """Detect if HostDrop is running inside an Android Termux environment."""
    return bool(
        os.environ.get("TERMUX_VERSION")
        or os.path.exists("/data/data/com.termux")
        or (os.environ.get("PREFIX") and "com.termux" in os.environ.get("PREFIX", ""))
        or hasattr(sys, "getandroidapilevel")
    )


def get_windows_volume_label(drive_path: str) -> str:
    """Retrieve Windows filesystem volume label using GetVolumeInformationW."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001)  # SEM_FAILCRITICALERRORS: suppress OS drive error dialogs
        buf = ctypes.create_unicode_buffer(1024)
        fs_buf = ctypes.create_unicode_buffer(1024)
        success = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(drive_path),
            buf,
            ctypes.sizeof(buf),
            None,
            None,
            None,
            fs_buf,
            ctypes.sizeof(fs_buf)
        )
        if success:
            val = buf.value.strip()
            if val:
                return val
    except Exception:
        pass
    return ""

# ── Optional Helper Libraries ───────────────────────────────────────────────────
try:
    import psutil
except ImportError:
    psutil = None

try:
    import qrcode
except ImportError:
    qrcode = None

# ── Authentication & Security Engine ──────────────────────────────────────────
try:
    import auth
except ImportError:
    auth = None

import re
import atexit
import getpass

# ── Global State & Invariant Configuration ─────────────────────────────────────
STATE_LOCK = threading.Lock()
UPLOAD_DIR = ""   # Where incoming files sent to the PC are stored (Inbox tab)
HOST_SHARE = ""   # Folder shared by PC for others to browse/download (Library tab)
SERVER_PORT = 8080

# Global Tunnel State
GLOBAL_TUNNEL_URL = ""
GLOBAL_TUNNEL_PROVIDER = "none"
TUNNEL_PROC = None
REQUIRE_AUTH = True
REQUIRE_AUTH_ON_LAN = False
REMOTE_FULL_DRIVE_ACCESS = False

# Resource & DoS Protection Limits
MAX_UPLOAD_SIZE = 50 * 1024 * 1024 * 1024       # 50 GB per file
MAX_ZIP_SIZE = 10 * 1024 * 1024 * 1024          # 10 GB max folder zip export
MAX_ZIP_FILES = 10_000                          # Max entries in a single zip archive
MAX_ZIP_DEPTH = 25                              # Max folder recursion depth
MIN_FREE_DISK_BUFFER = 500 * 1024 * 1024        # 500 MB required free disk margin

# Windows Reserved Device Filenames
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

try:
    CURRENT_USER = getpass.getuser()
except Exception:
    CURRENT_USER = os.environ.get("USERNAME") or os.environ.get("USER") or ""
USER_HOME = os.path.expanduser("~")


# ═══════════════════════════════════════════════════════════════════════════════
#  GLOBAL TUNNEL ORCHESTRATOR (Cloudflare Tunnel & Pinggy SSH)
# ═══════════════════════════════════════════════════════════════════════════════
class TunnelManager:
    @staticmethod
    def get_cloudflared_path():
        p = shutil.which("cloudflared")
        if p:
            return p
        candidates = []
        # Windows candidate locations (WinGet, Scoop, Program Files, local .bin)
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            candidates.append(os.path.join(local_app_data, "Microsoft", "WinGet", "Links", "cloudflared.exe"))
        user_profile = os.environ.get("USERPROFILE", os.path.expanduser("~"))
        if user_profile:
            candidates.append(os.path.join(user_profile, "scoop", "shims", "cloudflared.exe"))
        candidates.extend([
            r"C:\Program Files (x86)\cloudflared\cloudflared.exe",
            r"C:\Program Files\cloudflared\cloudflared.exe",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), ".bin", "cloudflared.exe")
        ])
        # Linux & Unix candidate locations
        candidates.extend([
            "/usr/local/bin/cloudflared",
            "/usr/bin/cloudflared",
            "/bin/cloudflared",
            os.path.expanduser("~/.local/bin/cloudflared")
        ])
        # macOS candidate locations (Homebrew on Apple Silicon & Intel)
        candidates.extend([
            "/opt/homebrew/bin/cloudflared",
            "/usr/local/bin/cloudflared"
        ])
        # Android Termux candidate locations
        termux_prefix = os.environ.get("PREFIX", "")
        if termux_prefix:
            candidates.append(os.path.join(termux_prefix, "bin", "cloudflared"))
        candidates.extend([
            "/data/data/com.termux/files/usr/bin/cloudflared",
            "/data/data/com.termux/files/home/.local/bin/cloudflared"
        ])
        for c in candidates:
            if c and os.path.isfile(c):
                return c
        return None

    @staticmethod
    def start(port=8080, provider="auto"):
        global GLOBAL_TUNNEL_URL, GLOBAL_TUNNEL_PROVIDER, TUNNEL_PROC
        if provider == "none":
            return ""

        cf_path = TunnelManager.get_cloudflared_path()
        if provider in ("auto", "cloudflare") and cf_path:
            cmd = [cf_path, "tunnel", "--url", f"http://127.0.0.1:{port}"]
            try:
                TUNNEL_PROC = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
                cf_regex = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

                def monitor_cf():
                    global GLOBAL_TUNNEL_URL, GLOBAL_TUNNEL_PROVIDER
                    for line in TUNNEL_PROC.stderr:
                        m = cf_regex.search(line)
                        if m and not GLOBAL_TUNNEL_URL:
                            with STATE_LOCK:
                                GLOBAL_TUNNEL_URL = m.group(0)
                                GLOBAL_TUNNEL_PROVIDER = "cloudflare"
                            print(f"\n  [+] Cloudflare Tunnel established: {GLOBAL_TUNNEL_URL}")
                            if auth:
                                print(f"  [+] Global Magic Link: {GLOBAL_TUNNEL_URL}/api/auth?key={auth.get_access_key()}\n")
                            break

                t = threading.Thread(target=monitor_cf, daemon=True)
                t.start()
                t.join(timeout=30)
                if GLOBAL_TUNNEL_URL:
                    return GLOBAL_TUNNEL_URL
                if TUNNEL_PROC:
                    try:
                        TUNNEL_PROC.terminate()
                        TUNNEL_PROC.wait(timeout=2)
                    except Exception:
                        try:
                            TUNNEL_PROC.kill()
                        except Exception:
                            pass
                    TUNNEL_PROC = None
            except Exception:
                if TUNNEL_PROC:
                    try:
                        TUNNEL_PROC.terminate()
                        TUNNEL_PROC.wait(timeout=2)
                    except Exception:
                        try:
                            TUNNEL_PROC.kill()
                        except Exception:
                            pass
                    TUNNEL_PROC = None

        # Fallback to Pinggy SSH
        ssh_bin = shutil.which("ssh")
        if not ssh_bin:
            termux_prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
            for cand in [
                r"C:\Windows\System32\OpenSSH\ssh.exe",
                os.path.join(termux_prefix, "bin", "ssh"),
                "/data/data/com.termux/files/usr/bin/ssh",
                "/usr/bin/ssh",
                "/usr/local/bin/ssh"
            ]:
                if os.path.isfile(cand):
                    ssh_bin = cand
                    break
        if provider in ("auto", "pinggy") and ssh_bin:
            cmd = [
                ssh_bin, "-p", "443", "-T",
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=30",
                "-R", f"0:localhost:{port}",
                "a.pinggy.io"
            ]
            try:
                TUNNEL_PROC = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                pinggy_regex = re.compile(r"https://[a-zA-Z0-9-]+\.a?\.?pinggy\.link")

                def monitor_pinggy():
                    global GLOBAL_TUNNEL_URL, GLOBAL_TUNNEL_PROVIDER
                    for line in TUNNEL_PROC.stdout:
                        m = pinggy_regex.search(line)
                        if m and not GLOBAL_TUNNEL_URL:
                            with STATE_LOCK:
                                GLOBAL_TUNNEL_URL = m.group(0)
                                GLOBAL_TUNNEL_PROVIDER = "pinggy"
                            print(f"\n  [+] Pinggy Tunnel established: {GLOBAL_TUNNEL_URL}")
                            if auth:
                                print(f"  [+] Global Magic Link: {GLOBAL_TUNNEL_URL}/api/auth?key={auth.get_access_key()}\n")
                            break

                t = threading.Thread(target=monitor_pinggy, daemon=True)
                t.start()
                t.join(timeout=30)
                if GLOBAL_TUNNEL_URL:
                    return GLOBAL_TUNNEL_URL
                if TUNNEL_PROC:
                    try:
                        TUNNEL_PROC.terminate()
                        TUNNEL_PROC.wait(timeout=2)
                    except Exception:
                        try:
                            TUNNEL_PROC.kill()
                        except Exception:
                            pass
                    TUNNEL_PROC = None
            except Exception:
                if TUNNEL_PROC:
                    try:
                        TUNNEL_PROC.terminate()
                        TUNNEL_PROC.wait(timeout=2)
                    except Exception:
                        try:
                            TUNNEL_PROC.kill()
                        except Exception:
                            pass
        if provider != "none" and not GLOBAL_TUNNEL_URL and not TUNNEL_PROC:
            print("  [-] Global Remote Access notice: Neither 'cloudflared' nor 'ssh' was found on your system.")
            print("      HostDrop is operating in 100% Offline LAN / Wi-Fi mode.")
            print("      To enable global remote access, install cloudflared or OpenSSH.")

        return ""

    @staticmethod
    def stop():
        global TUNNEL_PROC
        if TUNNEL_PROC:
            try:
                TUNNEL_PROC.terminate()
                TUNNEL_PROC.wait(timeout=2)
            except Exception:
                try:
                    TUNNEL_PROC.kill()
                except Exception:
                    pass
            TUNNEL_PROC = None

atexit.register(TunnelManager.stop)



# ═══════════════════════════════════════════════════════════════════════════════
#  NETWORK DISCOVERY & ADAPTER CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════════
def get_network_interfaces():
    """
    Enumerate all IPv4 network adapters, classifying them by priority:
    1. Wi-Fi (Wireless LAN)
    2. Mobile Hotspot (192.168.137.x or 'hotspot')
    3. Direct Cable P2P (169.254.x.x APIPA) & Wired Ethernet LAN
    8. Generic LAN / Other
    9. Virtual Switches (WSL, Hyper-V, VMware, Docker)
    10. Bluetooth PAN
    """
    interfaces = []
    seen_ips = set()

    if psutil:
        try:
            for name, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == socket.AF_INET and not a.address.startswith("127."):
                        ip = a.address
                        if ip in seen_ips:
                            continue
                        seen_ips.add(ip)
                        lo = name.lower()
                        
                        if "vethernet" in lo or "switch" in lo or "wsl" in lo or "hyper" in lo or "docker" in lo or "vmware" in lo:
                            kind, label, desc, pri = "virtual", "Virtual / WSL", "Internal Virtual Switch", 9
                        elif "wi-fi" in lo or "wireless" in lo or "wlan" in lo or (sys.platform == "darwin" and lo == "en0"):
                            kind, label, desc, pri = "wifi", "Wi-Fi Network", "Home/Office Wireless LAN", 1
                        elif ip.startswith("192.168.137.") or "hotspot" in lo or "host" in lo or "ap0" in lo or "rndis" in lo or "tether" in lo:
                            kind, label, desc, pri = "hotspot", "Mobile Hotspot", "Tethered Hotspot Devices", 2
                        elif "ethernet" in lo or "eth" in lo or "lan" in lo or lo.startswith("en"):
                            if ip.startswith("169.254."):
                                kind, label, desc, pri = "ethernet-direct", "Direct Cable (P2P)", "High-Speed Direct Link (90-115 MB/s)", 3
                            else:
                                kind, label, desc, pri = "ethernet", "Ethernet LAN", "Wired Gigabit Network (60-110 MB/s)", 3
                        elif "bluetooth" in lo:
                            kind, label, desc, pri = "bluetooth", "Bluetooth PAN", "Bluetooth Tethering", 10
                        else:
                            kind, label, desc, pri = "lan", "Local Network", "Connected Network Interface", 8

                        interfaces.append({
                            "ip": ip,
                            "name": name,
                            "kind": kind,
                            "label": label,
                            "desc": desc,
                            "priority": pri
                        })
        except Exception:
            pass

    # Fallback to standard socket resolution if psutil is unavailable or returned empty
    if not interfaces:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127.") and ip not in seen_ips:
                    interfaces.append({
                        "ip": ip,
                        "name": "Local Network",
                        "kind": "lan",
                        "label": "Local Network",
                        "desc": "Standard Network Adapter",
                        "priority": 5
                    })
                    seen_ips.add(ip)
        except Exception:
            pass

    interfaces.sort(key=lambda x: x["priority"])
    return interfaces


def get_local_ip_set():
    """Return a set of all IPv4 strings belonging to local host interfaces."""
    ips = {"127.0.0.1", "::1", "localhost"}
    for iface in get_network_interfaces():
        ips.add(iface["ip"])
    return ips


# ═══════════════════════════════════════════════════════════════════════════════
#  FILESYSTEM & HOST INTEGRATION HELPERS
# ═══════════════════════════════════════════════════════════════════════════════
def safe_int(val, default: int = 0) -> int:
    """Safely convert a value to an integer without raising ValueError or TypeError."""
    try:
        if val is None:
            return default
        if isinstance(val, (list, tuple)):
            if not val:
                return default
            val = val[0]
        return int(val)
    except (ValueError, TypeError):
        return default


def disk_info(path):
    """Calculate free, total, used percentage and formatted metrics for a path."""
    if not path:
        return {
            "free_gb": "?",
            "total_gb": "?",
            "used_gb": "?",
            "used_pct": 0,
            "used_percent": 0.0,
            "free_bytes": 0,
            "total_bytes": 0,
            "used_bytes": 0
        }
    try:
        total, used, free = shutil.disk_usage(path)
        used_pct_val = round((used / total) * 100, 1) if total > 0 else 0.0
        return {
            "free_gb": f"{free / (1024**3):.1f}",
            "total_gb": f"{total / (1024**3):.1f}",
            "used_gb": f"{used / (1024**3):.1f}",
            "used_pct": int(used * 100 // total) if total > 0 else 0,
            "used_percent": used_pct_val,
            "free_bytes": free,
            "total_bytes": total,
            "used_bytes": used
        }
    except Exception:
        return {
            "free_gb": "?",
            "total_gb": "?",
            "used_gb": "?",
            "used_pct": 0,
            "used_percent": 0.0,
            "free_bytes": 0,
            "total_bytes": 0,
            "used_bytes": 0
        }


def sanitize_path_for_client(path: str, is_admin: bool = False) -> str:
    """
    Redact host username and sensitive home path structures for remote/guest clients.
    Exposes full paths only to verified authenticated administrators.
    """
    if not path:
        return ""
    if is_admin:
        return path

    norm = os.path.normpath(path)
    if USER_HOME and norm.startswith(USER_HOME):
        rel = norm[len(USER_HOME):].lstrip("/\\")
        return f"~/{rel}".replace("\\", "/")

    if CURRENT_USER and CURRENT_USER in norm:
        norm = norm.replace(f"\\Users\\{CURRENT_USER}", "\\Users\\[User]")
        norm = norm.replace(f"/Users/{CURRENT_USER}", "/Users/[User]")
        norm = norm.replace(f"\\home\\{CURRENT_USER}", "\\home\\[User]")
        norm = norm.replace(f"/home/{CURRENT_USER}", "/home/[User]")

    return norm


def safe_path(base_dir, rel):
    """
    Strict Path Traversal & Safe Path Normalization Engine (Hardened).
    Defends against:
    - Relative directory traversal (../, ..\\, mixed slashes, multi-dot)
    - URL-encoded, double-encoded, multi-encoded traversal (%2e%2e%2f, %252e%252e%252f)
    - URL-encoded & raw null-byte string poisoning (\\0, %00, %2500)
    - NTFS Alternate Data Streams (::$DATA, file.txt:stream, %3a, %253a)
    - UNC network paths (\\\\server\\share, //server/share, \\\\?\\)
    - Absolute and root-prefixed paths (/etc/passwd, C:\\Windows, \\Windows)
    - Windows reserved device names (CON, PRN, AUX, NUL, COM1-9, LPT1-9)
    - Symlink and directory junction escapes outside base_dir
    """
    if not base_dir or rel is None:
        return None

    rel_str = str(rel)

    # 1. Null-byte rejection before decoding
    if "\0" in rel_str or "\0" in str(base_dir) or "%00" in rel_str.lower():
        return None

    # 2. Recursive URL unquoting (up to 5 iterations or until fixed-point)
    clean_rel = rel_str
    for _ in range(5):
        prev = clean_rel
        clean_rel = urllib.parse.unquote(clean_rel)
        if clean_rel == prev:
            break

    # Re-verify null byte poisoning after full unquoting
    if "\0" in clean_rel or "%00" in clean_rel.lower():
        return None

    # 3. NTFS Alternate Data Streams (ADS) and drive colon rejection
    if ":" in clean_rel:
        return None

    # 4. Strip whitespace
    stripped = clean_rel.strip()
    if not stripped and rel_str:
        return None

    # 5. Reject absolute / root-prefixed / UNC paths
    if stripped.startswith(("/", "\\")):
        return None

    # 6. Slash normalization and stripping
    rel_clean = stripped.replace("\\", "/")
    norm_rel = os.path.normpath(rel_clean)

    if norm_rel == ".." or norm_rel.startswith("../") or norm_rel.startswith("..\\") or norm_rel.split("/")[0] == "..":
        return None

    # 7. Check Windows reserved device names across all path segments
    parts = norm_rel.replace("\\", "/").split("/")
    for p in parts:
        if not p:
            continue
        seg = p.rstrip(". ")
        base_name = seg.split(".")[0].strip().upper()
        if base_name in WINDOWS_RESERVED_NAMES:
            return None

    try:
        base_abs = os.path.abspath(base_dir)
        base_real = os.path.realpath(base_abs)

        if norm_rel == ".":
            return base_real

        full_path = os.path.abspath(os.path.join(base_real, norm_rel))

        if os.path.exists(full_path):
            full_real = os.path.realpath(full_path)
        else:
            parent = os.path.dirname(full_path)
            parent_real = os.path.realpath(parent)
            if os.path.commonpath([base_real, parent_real]) != base_real:
                return None
            full_real = full_path

        if os.path.commonpath([base_real, full_real]) != base_real:
            return None

        return full_real
    except (ValueError, OSError):
        return None


def get_host_drives():
    """
    Enumerate all logical storage drives on the host PC or mobile device with capacity and free space.
    - Windows: queries C:\\, D:\\, etc. via GetLogicalDrives and GetVolumeInformationW.
    - Android Termux: exposes internal storage (/sdcard), downloads, Termux home (~), OTG mounts, and root (/).
    - Linux & macOS: queries root (/), user home (~), /Volumes, /media, /run/media, and /mnt.
    """
    drives = []

    def make_entry(path, name, label, letter, is_system=False):
        total = 0
        used = 0
        free = 0
        percent = 0.0
        free_gb = "?"
        total_gb = "?"
        used_gb = "?"
        used_pct = 0
        try:
            u = shutil.disk_usage(path)
            total = u.total
            used = u.used
            free = u.free
            if total > 0:
                percent = round((used / total) * 100, 1)
                used_pct = int(used * 100 // total)
            free_gb = f"{free / (1024**3):.1f}"
            total_gb = f"{total / (1024**3):.1f}"
            used_gb = f"{used / (1024**3):.1f}"
        except Exception:
            pass

        return {
            "path": path,
            "name": name,
            "label": label,
            "letter": letter,
            "total": total,
            "used": used,
            "free": free,
            "percent": percent,
            "free_gb": free_gb,
            "total_gb": total_gb,
            "used_gb": used_gb,
            "used_pct": used_pct,
            "used_percent": percent,
            "is_system": is_system
        }

    # 1. Android Termux Implementation
    if is_termux():
        seen = set()
        # Internal Storage (/sdcard or ~/storage/shared)
        sd_candidates = ["/sdcard", "/storage/emulated/0", os.path.expanduser("~/storage/shared")]
        primary_sd = next((p for p in sd_candidates if os.path.exists(p)), "/sdcard")
        drives.append(make_entry(primary_sd, "Internal Storage", "Internal Storage (/sdcard)", "SD", is_system=False))
        seen.add(os.path.normpath(primary_sd))

        # Downloads (/sdcard/Download or ~/storage/downloads)
        dl_candidates = [os.path.join(primary_sd, "Download"), os.path.expanduser("~/storage/downloads")]
        primary_dl = next((p for p in dl_candidates if os.path.exists(p)), os.path.join(primary_sd, "Download"))
        if os.path.normpath(primary_dl) not in seen:
            drives.append(make_entry(primary_dl, "Downloads", "Downloads (/sdcard/Download)", "DL", is_system=False))
            seen.add(os.path.normpath(primary_dl))

        # Termux Home (~)
        home_path = os.path.expanduser("~")
        if os.path.normpath(home_path) not in seen and os.path.exists(home_path):
            drives.append(make_entry(home_path, "Termux Home", "Termux Home (~)", "TH", is_system=True))
            seen.add(os.path.normpath(home_path))

        # MicroSD / USB OTG mounts (/storage/XXXX-XXXX)
        if os.path.exists("/storage"):
            try:
                for sub in os.listdir("/storage"):
                    if sub in ("emulated", "self") or sub.startswith("."):
                        continue
                    sp = os.path.join("/storage", sub)
                    if os.path.isdir(sp) and os.path.normpath(sp) not in seen:
                        drives.append(make_entry(sp, f"Storage ({sub})", f"Storage ({sub})", "SD", is_system=False))
                        seen.add(os.path.normpath(sp))
            except Exception:
                pass

        # System Root (/) if readable
        try:
            if os.path.exists("/") and os.access("/", os.R_OK) and os.path.normpath("/") not in seen:
                drives.append(make_entry("/", "System Root", "Root (/)", "/", is_system=False))
                seen.add(os.path.normpath("/"))
        except Exception:
            pass

    # 2. Windows Implementation
    elif sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(0x0001)  # SEM_FAILCRITICALERRORS: suppress OS drive error dialogs
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    drive_path = f"{letter}:\\"
                    vol_name = get_windows_volume_label(drive_path)
                    if vol_name:
                        lbl = f"{vol_name} ({letter}:)"
                        nm = f"{vol_name} ({letter}:)"
                    else:
                        lbl = f"OS ({letter}:)" if letter.upper() == "C" else f"Data ({letter}:)" if letter.upper() == "D" else f"Local Disk ({letter}:)"
                        nm = f"Local Disk ({letter}:)"
                    drives.append(make_entry(drive_path, nm, lbl, letter, is_system=(letter.upper() == "C")))
                bitmask >>= 1
        except Exception:
            for letter in string.ascii_uppercase:
                drive_path = f"{letter}:\\"
                if os.path.exists(drive_path):
                    vol_name = get_windows_volume_label(drive_path)
                    if vol_name:
                        lbl = f"{vol_name} ({letter}:)"
                        nm = f"{vol_name} ({letter}:)"
                    else:
                        lbl = f"OS ({letter}:)" if letter.upper() == "C" else f"Data ({letter}:)" if letter.upper() == "D" else f"Local Disk ({letter}:)"
                        nm = f"Local Disk ({letter}:)"
                    drives.append(make_entry(drive_path, nm, lbl, letter, is_system=(letter.upper() == "C")))

    # 3. Linux & macOS Implementation
    else:
        seen = set()
        # Root (/)
        if os.path.exists("/"):
            drives.append(make_entry("/", "Root (/)", "Root (/)", "/", is_system=True))
            seen.add(os.path.normpath("/"))

        # User Home (~)
        home_path = os.path.expanduser("~")
        if os.path.exists(home_path) and os.path.normpath(home_path) not in seen:
            drives.append(make_entry(home_path, "Home", "Home (~)", "~", is_system=False))
            seen.add(os.path.normpath(home_path))

        # Mount directories to scan
        mount_roots = ["/Volumes", "/media", "/mnt"]
        curr_user = CURRENT_USER or os.environ.get("USER", "")
        if curr_user:
            mount_roots.extend([f"/media/{curr_user}", f"/run/media/{curr_user}"])

        for root_dir in mount_roots:
            if os.path.exists(root_dir):
                try:
                    for sub in os.listdir(root_dir):
                        sp = os.path.join(root_dir, sub)
                        if os.path.islink(sp):
                            continue
                        if os.path.isdir(sp) and os.path.normpath(sp) not in seen:
                            label = sub
                            drives.append(make_entry(sp, label, label, "/", is_system=False))
                            seen.add(os.path.normpath(sp))
                except Exception:
                    pass

    return drives


def browse_host_directory(path=""):
    """
    Traverse the host filesystem safely, filtering protected directories
    and returning breadcrumbs, subdirectories, disk space, and drives list.
    """
    path = (path or "").strip().strip("'\"")
    drives = get_host_drives()

    if not path or path.lower() in ("roots", "drives", "root"):
        return {
            "is_root": True,
            "current_path": "",
            "parent_path": "",
            "drives": drives,
            "subdirs": []
        }

    target = os.path.abspath(path)
    if not os.path.exists(target) or not os.path.isdir(target):
        return {
            "is_root": False,
            "error": "Directory does not exist or is inaccessible",
            "current_path": target,
            "parent_path": "",
            "drives": drives,
            "subdirs": []
        }

    parent = os.path.dirname(target)
    if parent == target or target == "/":
        parent = ""  # Reached top-level drive root / storage overview

    subdirs = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name
                        # Filter system-protected / reserved Windows folders
                        if sys.platform == "win32":
                            if name.startswith("$") or name in (
                                "System Volume Information", "Recovery", "$WinREAgent",
                                "Config.Msi", "MSOCache", "hiberfil.sys", "pagefile.sys"
                            ):
                                continue
                        else:
                            # Filter virtual filesystem mounts on POSIX when browsing root
                            if target == "/" and name in ("proc", "sys", "dev"):
                                continue
                        subdirs.append({
                            "name": name,
                            "path": entry.path,
                            "isDir": True
                        })
                except (PermissionError, OSError):
                    continue
    except PermissionError:
        err_msg = "Permission denied accessing folder"
        if is_termux() or "/sdcard" in target or "/storage" in target:
            err_msg = "Permission denied. Run 'termux-setup-storage' in Termux to grant storage access."
        return {
            "is_root": False,
            "error": err_msg,
            "current_path": target,
            "parent_path": parent,
            "drives": drives,
            "subdirs": []
        }
    except Exception as e:
        return {
            "is_root": False,
            "error": str(e),
            "current_path": target,
            "parent_path": parent,
            "drives": drives,
            "subdirs": []
        }

    subdirs.sort(key=lambda x: x["name"].lower())
    d_info = disk_info(target)

    return {
        "is_root": False,
        "current_path": target,
        "parent_path": parent,
        "disk": d_info,
        "free_gb": d_info.get("free_gb"),
        "total_gb": d_info.get("total_gb"),
        "used_gb": d_info.get("used_gb"),
        "used_pct": d_info.get("used_pct"),
        "used_percent": d_info.get("used_percent"),
        "drives": drives,
        "subdirs": subdirs
    }


def pick_folder_native(timeout_sec=120):
    """
    Launch native OS folder selection dialog across Windows, macOS, and Linux.
    - Windows: PowerShell STA FolderBrowserDialog (with Tkinter fallback)
    - macOS: AppleScript (osascript choose folder, with Tkinter fallback)
    - Linux: zenity or kdialog (with Tkinter fallback)
    - Android Termux / Headless: Returns (None, "unsupported_platform") for in-browser modal fallback
    """
    if is_termux():
        return None, "unsupported_platform"

    # 1. Windows: PowerShell STA FolderBrowserDialog
    if sys.platform == "win32":
        ps_script = (
            "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null;"
            "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
            "$dialog.Description = 'Select folder for HostDrop File Transfer Hub';"
            "$dialog.ShowNewFolderButton = $true;"
            "$topForm = New-Object System.Windows.Forms.Form;"
            "$topForm.TopMost = $true;"
            "$topForm.Width = 0;"
            "$topForm.Height = 0;"
            "$topForm.StartPosition = [System.Windows.Forms.FormStartPosition]::CenterScreen;"
            "$result = $dialog.ShowDialog($topForm);"
            "$topForm.Dispose();"
            "if ($result -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dialog.SelectedPath } else { Write-Output '' }"
        )
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA", "-Command", ps_script]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            selected = proc.stdout.strip()
            if selected and os.path.isdir(selected):
                return selected, None
            return None, "cancelled"
        except subprocess.TimeoutExpired:
            return None, "dialog_timeout"
        except Exception:
            pass

    # 2. macOS: AppleScript osascript choose folder
    elif sys.platform == "darwin":
        osa_script = 'POSIX path of (choose folder with prompt "Select folder for HostDrop File Transfer Hub")'
        cmd = ["osascript", "-e", osa_script]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
            if proc.returncode == 0:
                selected = proc.stdout.strip()
                if selected and os.path.isdir(selected):
                    return selected, None
                return None, "cancelled"
            else:
                err_text = proc.stderr.lower()
                if "user canceled" in err_text or "-128" in err_text:
                    return None, "cancelled"
        except subprocess.TimeoutExpired:
            return None, "dialog_timeout"
        except Exception:
            pass

    # 3. Linux / POSIX: zenity or kdialog
    else:
        if shutil.which("zenity"):
            cmd = ["zenity", "--file-selection", "--directory", "--title=Select folder for HostDrop File Transfer Hub"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                if proc.returncode == 0:
                    selected = proc.stdout.strip()
                    if selected and os.path.isdir(selected):
                        return selected, None
                    return None, "cancelled"
                return None, "cancelled"
            except subprocess.TimeoutExpired:
                return None, "dialog_timeout"
            except Exception:
                pass
        elif shutil.which("kdialog"):
            cmd = ["kdialog", "--getexistingdirectory", os.path.expanduser("~"), "--title", "Select folder for HostDrop File Transfer Hub"]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
                if proc.returncode == 0:
                    selected = proc.stdout.strip()
                    if selected and os.path.isdir(selected):
                        return selected, None
                    return None, "cancelled"
                return None, "cancelled"
            except subprocess.TimeoutExpired:
                return None, "dialog_timeout"
            except Exception:
                pass

    # 4. Universal GUI Fallback: Tkinter (if display server / desktop is active)
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        try:
            root.wm_attributes("-topmost", 1)
        except Exception:
            pass
        path = filedialog.askdirectory(title="Select folder for HostDrop File Transfer Hub")
        root.destroy()
        if path and os.path.isdir(path):
            return path, None
        return None, "cancelled"
    except Exception:
        pass

    return None, "unsupported_platform"


pick_folder_powershell = pick_folder_native


def open_in_os_explorer(target_dir, client_ip):
    """
    Spawn the host OS file explorer (explorer.exe / open / termux-open / xdg-open) in the foreground.
    Differentiates between localhost and remote clients.
    """
    is_local = client_ip in get_local_ip_set()
    
    if not target_dir or not os.path.exists(target_dir):
        return {
            "success": False,
            "status": "error",
            "error": "directory_not_found",
            "is_local": is_local,
            "is_local_client": is_local,
            "message": "Directory not found on host PC"
        }

    norm = os.path.normpath(target_dir)
    try:
        if is_termux():
            cmd = ["termux-open", norm]
        elif sys.platform == "win32":
            cmd = ["explorer.exe", norm]
        elif sys.platform == "darwin":
            cmd = ["open", norm]
        else:
            cmd = ["xdg-open", norm]

        # Check if launcher binary is available before attempting to spawn
        launcher = cmd[0]
        if launcher != "explorer.exe" and not shutil.which(launcher):
            raise FileNotFoundError(f"{launcher} not available")

        subprocess.Popen(cmd)

        if is_local:
            if is_termux():
                msg = "Opened folder in File Manager"
            elif sys.platform == "win32":
                msg = "Opened folder in File Explorer"
            elif sys.platform == "darwin":
                msg = "Opened folder in Finder"
            else:
                msg = "Opened folder in File Manager"
        else:
            msg = "Folder opened on Host PC display (viewing in browser)"

        return {
            "success": True,
            "status": "ok",
            "is_local": is_local,
            "is_local_client": is_local,
            "path": norm,
            "message": msg
        }
    except FileNotFoundError:
        return {
            "success": False,
            "status": "error",
            "error": "file_manager_unavailable",
            "is_local": is_local,
            "is_local_client": is_local,
            "message": "No graphical file manager available in this environment."
        }
    except Exception as e:
        return {
            "success": False,
            "status": "error",
            "error": str(e),
            "is_local": is_local,
            "is_local_client": is_local,
            "message": f"Could not launch OS explorer: {e}"
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  HIGH-CRAFT UI TEMPLATE (Linear / Raycast / Claude Obsidian Dark Edition)
# ═══════════════════════════════════════════════════════════════════════════════
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, viewport-fit=cover">
<title>HostDrop &mdash; High-Speed LAN Transfer Hub</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
/* ═══════════════════════════════════════════════════════════════════════════════
   DESIGN TOKENS & SURFACE HIERARCHY (Linear / Raycast / Claude Obsidian Theme)
   ═══════════════════════════════════════════════════════════════════════════════ */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  /* Surface Ladder */
  --canvas: #090a0c;
  --surface-1: #111215;
  --surface-2: #16171b;
  --surface-3: #1c1d22;
  --surface-4: #22232a;
  --surface-hover: #1f2027;
  --surface-overlay: rgba(9, 10, 12, 0.85);

  /* Hairlines & Borders */
  --border-subtle: rgba(255, 255, 255, 0.05);
  --border-standard: rgba(255, 255, 255, 0.09);
  --border-hover: rgba(255, 255, 255, 0.18);
  --border-focus: rgba(255, 255, 255, 0.35);
  --border-accent: rgba(79, 127, 255, 0.50);

  /* WCAG AA High Contrast Typography */
  --text-primary: #ededed;
  --text-secondary: #9ca0a8;
  --text-tertiary: #8e929b;
  --text-disabled: #4a4d56;
  --text-inverse: #090a0c;

  /* Accent & Semantic Palette */
  --accent: #ededed;
  --brand-blue: #4f7fff;
  --brand-blue-glow: rgba(79, 127, 255, 0.25);
  --status-success: #10b981;
  --status-success-bg: rgba(16, 185, 129, 0.12);
  --status-warning: #f59e0b;
  --status-warning-bg: rgba(245, 158, 11, 0.12);
  --status-error: #ef4444;
  --status-error-bg: rgba(239, 68, 68, 0.12);
  --status-info: #38bdf8;
  --status-info-bg: rgba(56, 189, 248, 0.12);

  /* Network Badges */
  --net-wifi-fg: #60a5fa;
  --net-wifi-bg: rgba(96, 165, 250, 0.10);
  --net-wifi-border: rgba(96, 165, 250, 0.25);
  
  --net-hotspot-fg: #fbbf24;
  --net-hotspot-bg: rgba(251, 191, 36, 0.10);
  --net-hotspot-border: rgba(251, 191, 36, 0.25);

  --net-ethernet-fg: #34d399;
  --net-ethernet-bg: rgba(52, 211, 153, 0.10);
  --net-ethernet-border: rgba(52, 211, 153, 0.25);

  --net-p2p-fg: #2dd4bf;
  --net-p2p-bg: rgba(45, 212, 191, 0.12);
  --net-p2p-border: rgba(45, 212, 191, 0.30);

  /* Typography Stacks */
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;

  /* Spatial & Elevation */
  --radius-xs: 4px;
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
  --radius-xl: 16px;
  --radius-pill: 9999px;

  --shadow-panel: 0 1px 2px rgba(0, 0, 0, 0.4);
  --shadow-raised: 0 4px 16px rgba(0, 0, 0, 0.5), 0 1px 2px rgba(0, 0, 0, 0.4);
  --shadow-modal: 0 20px 50px rgba(0, 0, 0, 0.75), 0 0 0 1px rgba(255, 255, 255, 0.08);
  --shadow-accent-glow: 0 0 30px rgba(79, 127, 255, 0.25);
}

html, body {
  overflow-x: hidden;
  max-width: 100vw;
  width: 100%;
  margin: 0;
  padding: 0;
  box-sizing: border-box;
}

body {
  background-color: var(--canvas);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
  min-height: 100dvh;
  overflow-x: hidden;
  max-width: 100vw;
  width: 100%;
  display: flex;
  flex-direction: column;
}

/* Tabular Numerics for Zero-Jitter UI */
.tabular-nums, .mono, .ts-num {
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
  font-feature-settings: "tnum" 1;
}

/* Universal SVG Icon Style */
.icon {
  width: 16px;
  height: 16px;
  stroke-width: 1.75;
  stroke: currentColor;
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
  display: inline-block;
  vertical-align: middle;
  flex-shrink: 0;
}

/* ═══════════════════════════════════════════════════════════════════════════════
   HEADER & BRANDING
   ═══════════════════════════════════════════════════════════════════════════════ */
.header {
  background: var(--surface-1);
  border-bottom: 1px solid var(--border-standard);
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  overflow: hidden;
  max-width: 100%;
}

.header-inner {
  max-width: 1320px;
  margin: 0 auto;
  padding: 12px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  user-select: none;
}

.brand-icon {
  width: 32px;
  height: 32px;
  background: var(--surface-3);
  border: 1px solid var(--border-hover);
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--brand-blue);
  box-shadow: 0 2px 8px rgba(79, 127, 255, 0.2);
}
.brand-icon svg { width: 18px; height: 18px; }

.brand-title {
  font-size: 15px;
  font-weight: 600;
  letter-spacing: -0.02em;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.status-pill {
  font-size: 11px;
  font-weight: 500;
  color: var(--status-success);
  background: var(--status-success-bg);
  border: 1px solid rgba(16, 185, 129, 0.25);
  border-radius: var(--radius-pill);
  padding: 2px 8px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.status-pill.host-badge {
  color: var(--status-success);
  background: var(--status-success-bg);
  border-color: rgba(16, 185, 129, 0.25);
}

.status-pill.guest-badge {
  color: #38bdf8;
  background: rgba(56, 189, 248, 0.12);
  border-color: rgba(56, 189, 248, 0.25);
}

.status-dot {
  width: 6px;
  height: 6px;
  background: var(--status-success);
  border-radius: 50%;
  box-shadow: 0 0 8px var(--status-success);
  animation: pulseDot 2s infinite ease-in-out;
}

@keyframes pulseDot {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.85); }
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ═══════════════════════════════════════════════════════════════════════════════
   NETWORK LINKS RIBBON (Horizontal Wheel Scroll, Touch Gestures, Grid Toggle)
   ═══════════════════════════════════════════════════════════════════════════════ */
.network-bar {
  background: var(--surface-1);
  border-bottom: 1px solid var(--border-subtle);
  position: relative;
}

.network-bar-inner {
  max-width: 1320px;
  margin: 0 auto;
  padding: 8px 24px;
  display: flex;
  align-items: center;
  gap: 12px;
}

.net-scroll-wrapper {
  position: relative;
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  overflow: hidden;
}

.net-scroll-container {
  display: flex;
  align-items: center;
  gap: 10px;
  overflow-x: auto;
  scrollbar-width: thin;
  scrollbar-color: var(--border-hover) transparent;
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
  scroll-snap-type: x mandatory;
  width: 100%;
  padding: 4px 2px;
}
.net-scroll-container::-webkit-scrollbar { height: 4px; }
.net-scroll-container::-webkit-scrollbar-track { background: transparent; }
.net-scroll-container::-webkit-scrollbar-thumb { background: var(--border-hover); border-radius: 9999px; }

.net-scroll-container.grid-mode {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  overflow-x: visible;
  gap: 12px;
  padding: 8px 0;
}

.net-item {
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  padding: 8px 12px;
  min-width: 220px;
  max-width: 320px;
  flex-shrink: 0;
  cursor: pointer;
  position: relative;
  transition: all 0.15s ease;
  user-select: none;
  scroll-snap-align: start;
}
.net-item:hover {
  background: var(--surface-3);
  border-color: var(--border-hover);
  transform: translateY(-1px);
}
.net-item:active { transform: translateY(0); }

.net-item-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
  gap: 8px;
}

.net-kind-badge {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 2px 6px;
  border-radius: var(--radius-xs);
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.net-kind-badge.wifi { color: var(--net-wifi-fg); background: var(--net-wifi-bg); border: 1px solid var(--net-wifi-border); }
.net-kind-badge.hotspot { color: var(--net-hotspot-fg); background: var(--net-hotspot-bg); border: 1px solid var(--net-hotspot-border); }
.net-kind-badge.ethernet { color: var(--net-ethernet-fg); background: var(--net-ethernet-bg); border: 1px solid var(--net-ethernet-border); }
.net-kind-badge.ethernet-direct { color: var(--net-p2p-fg); background: var(--net-p2p-bg); border: 1px solid var(--net-p2p-border); }
.net-kind-badge.virtual { color: var(--text-tertiary); background: var(--surface-4); border: 1px solid var(--border-subtle); }
.net-kind-badge.lan { color: var(--text-secondary); background: var(--surface-3); border: 1px solid var(--border-standard); }

.net-item-url {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.net-item-desc {
  font-size: 11px;
  color: var(--text-tertiary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.net-copied-badge {
  position: absolute;
  inset: 0;
  background: var(--status-success);
  color: #ffffff;
  border-radius: var(--radius-md);
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  font-size: 12px;
  gap: 6px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
  z-index: 5;
}
.net-item.copied .net-copied-badge { opacity: 1; }

.scroll-chevron {
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-sm);
  color: var(--text-secondary);
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex-shrink: 0;
  transition: all 0.15s ease;
}
.scroll-chevron:hover {
  background: var(--surface-3);
  color: var(--text-primary);
  border-color: var(--border-hover);
}

/* ═══════════════════════════════════════════════════════════════════════════════
   GUEST DEVICE GUIDANCE BANNER
   ═══════════════════════════════════════════════════════════════════════════════ */
.guest-banner {
  background: linear-gradient(135deg, rgba(56, 189, 248, 0.08) 0%, rgba(79, 127, 255, 0.05) 100%);
  border: 1px solid rgba(56, 189, 248, 0.25);
  border-radius: var(--radius-lg);
  padding: 14px 16px;
  margin-bottom: 16px;
  width: 100%;
}

.guest-banner-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  gap: 8px;
}

.guest-banner-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--status-info);
  display: flex;
  align-items: center;
  gap: 8px;
}

.guest-badge-pill {
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 2px 8px;
  border-radius: var(--radius-pill);
  background: rgba(56, 189, 248, 0.15);
  color: #38bdf8;
  border: 1px solid rgba(56, 189, 248, 0.3);
}

.guest-steps-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.guest-step-card {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  background: var(--surface-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 10px 12px;
}

.step-number {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: var(--brand-blue);
  color: #ffffff;
  font-weight: 700;
  font-size: 11px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}

.step-content {
  flex: 1;
  min-width: 0;
}

.step-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 2px;
}

.step-desc {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.4;
}

/* ═══════════════════════════════════════════════════════════════════════════════
   BUTTONS & CONTROLS
   ═══════════════════════════════════════════════════════════════════════════════ */
.btn {
  font-family: var(--font-sans);
  font-size: 12px;
  font-weight: 500;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 14px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-standard);
  background: var(--surface-2);
  color: var(--text-primary);
  cursor: pointer;
  user-select: none;
  transition: all 0.15s ease;
  text-decoration: none;
  min-height: 36px;
}
.btn:hover {
  background: var(--surface-3);
  border-color: var(--border-hover);
}
.btn:active { transform: scale(0.98); }

.btn-primary {
  background: var(--brand-blue);
  border-color: var(--brand-blue);
  color: #ffffff;
  box-shadow: 0 1px 3px rgba(79, 127, 255, 0.3);
}
.btn-primary:hover {
  background: #3b6ae8;
  border-color: #3b6ae8;
  color: #ffffff;
}

.btn-ghost {
  background: transparent;
  border-color: transparent;
  color: var(--text-secondary);
}
.btn-ghost:hover {
  background: var(--surface-2);
  border-color: var(--border-subtle);
  color: var(--text-primary);
}

.btn-sm {
  padding: 5px 10px;
  font-size: 11px;
  min-height: 32px;
}

.icon-btn {
  width: 34px;
  height: 34px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s ease;
  flex-shrink: 0;
}
.icon-btn:hover {
  background: var(--surface-3);
  color: var(--text-primary);
  border-color: var(--border-hover);
}

.icon-btn-micro {
  width: 24px;
  height: 24px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--radius-xs);
  color: var(--text-tertiary);
  cursor: pointer;
  transition: all 0.15s ease;
}
.icon-btn-micro:hover {
  background: var(--surface-3);
  color: var(--text-primary);
  border-color: var(--border-subtle);
}
.icon-btn-micro svg { width: 14px; height: 14px; stroke: currentColor; fill: none; stroke-width: 2; }

/* ═══════════════════════════════════════════════════════════════════════════════
   MAIN WORKBENCH LAYOUT
   ═══════════════════════════════════════════════════════════════════════════════ */
.app-container {
  max-width: 1320px;
  margin: 0 auto;
  padding: 20px 24px;
  display: grid;
  grid-template-columns: 340px 1fr;
  gap: 20px;
  flex: 1;
  width: 100%;
}

.sidebar {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.card {
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-lg);
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-shadow: var(--shadow-panel);
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.card-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.card-helper-text {
  font-size: 11px;
  color: var(--text-tertiary);
  line-height: 1.4;
}

.tab-badge {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: var(--radius-pill);
  background: var(--surface-3);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
}

.target-box {
  background: var(--surface-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.target-path-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-width: 0;
}

.target-path {
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  min-width: 0;
  flex: 1;
}

.storage-meter {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.storage-meter-labels {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-tertiary);
}

.storage-track {
  width: 100%;
  height: 4px;
  background: var(--surface-4);
  border-radius: var(--radius-pill);
  overflow: hidden;
}

.storage-fill {
  height: 100%;
  background: var(--brand-blue);
  border-radius: var(--radius-pill);
  transition: width 0.3s ease;
}
.storage-fill.warn { background: var(--status-warning); }

.card-button-row {
  display: flex;
  gap: 6px;
}
.card-button-row .btn { flex: 1; }

/* ═══════════════════════════════════════════════════════════════════════════════
   DROPZONE & PROGRESS
   ═══════════════════════════════════════════════════════════════════════════════ */
.dropzone {
  border: 2px dashed var(--border-hover);
  border-radius: var(--radius-md);
  padding: 20px 16px;
  text-align: center;
  background: var(--surface-2);
  cursor: pointer;
  transition: all 0.2s ease;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.dropzone:hover, .dropzone.dragover {
  border-color: var(--brand-blue);
  background: rgba(79, 127, 255, 0.05);
}

.dropzone-icon {
  width: 40px;
  height: 40px;
  border-radius: var(--radius-md);
  background: var(--surface-3);
  border: 1px solid var(--border-standard);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--brand-blue);
}
.dropzone-icon svg { width: 20px; height: 20px; }

.dropzone-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.dropzone-desc {
  font-size: 11px;
  color: var(--text-tertiary);
}

.upload-actions-mobile {
  display: flex;
  gap: 8px;
  margin-top: 4px;
}

.transfer-card {
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  padding: 10px 12px;
  display: none;
  flex-direction: column;
  gap: 6px;
}
.transfer-card.active { display: flex; }

.transfer-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 11px;
}

.transfer-file-name {
  font-weight: 500;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 200px;
}

.progress-track {
  width: 100%;
  height: 6px;
  background: var(--surface-4);
  border-radius: var(--radius-pill);
  overflow: hidden;
}

.progress-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--brand-blue) 0%, #38bdf8 100%);
  width: 0%;
  transition: width 0.15s linear;
}

.transfer-meta {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-tertiary);
}

/* ═══════════════════════════════════════════════════════════════════════════════
   FILE EXPLORER WORKBENCH
   ═══════════════════════════════════════════════════════════════════════════════ */
.main-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.explorer-card {
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-lg);
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-panel);
  overflow: hidden;
}

.explorer-header {
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-standard);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--surface-1);
}

.explorer-tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  background: var(--surface-2);
  padding: 3px;
  border-radius: var(--radius-md);
  border: 1px solid var(--border-subtle);
}

.tab-btn {
  font-family: var(--font-sans);
  font-size: 12px;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  border: none;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 6px;
  transition: all 0.15s ease;
  user-select: none;
}
.tab-btn:hover { color: var(--text-primary); }
.tab-btn.active {
  background: var(--surface-4);
  color: var(--text-primary);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.2);
}

.tab-helper-bar {
  padding: 6px 16px;
  background: var(--surface-2);
  border-bottom: 1px solid var(--border-subtle);
  font-size: 11px;
  color: var(--text-tertiary);
  display: flex;
  align-items: center;
  gap: 6px;
}

.explorer-toolbar {
  padding: 10px 16px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: var(--surface-1);
}

.search-box {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  padding: 4px 10px;
  max-width: 260px;
  flex: 1;
}
.search-box svg { width: 14px; height: 14px; color: var(--text-tertiary); flex-shrink: 0; }
.search-input {
  background: transparent;
  border: none;
  outline: none;
  font-size: 11px;
  color: var(--text-primary);
  width: 100%;
}

.explorer-breadcrumbs {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  overflow-x: auto;
  scrollbar-width: none;
  white-space: nowrap;
}
.crumb-item {
  color: var(--text-secondary);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
}
.crumb-item:hover { color: var(--text-primary); }
.crumb-sep { color: var(--text-disabled); font-size: 10px; }

.table-wrapper {
  overflow-x: auto;
  max-height: calc(100vh - 340px);
  min-height: 280px;
}

.file-table {
  width: 100%;
  border-collapse: collapse;
  text-align: left;
  font-size: 12px;
}

.file-table th {
  padding: 8px 16px;
  font-size: 11px;
  font-weight: 500;
  color: var(--text-tertiary);
  border-bottom: 1px solid var(--border-subtle);
  background: var(--surface-1);
  position: sticky;
  top: 0;
  z-index: 10;
}

.file-table td {
  padding: 8px 16px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}

.file-row {
  cursor: pointer;
  transition: background 0.1s ease;
}
.file-row:hover { background: var(--surface-2); }

.file-name-cell {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--text-primary);
  font-weight: 500;
}

.file-icon-box {
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  background: var(--surface-3);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-secondary);
  flex-shrink: 0;
}
.file-icon-box svg { width: 16px; height: 16px; stroke: currentColor; fill: none; stroke-width: 1.75; }
.file-icon-box.folder { color: #fbbf24; background: rgba(251, 191, 36, 0.1); }
.file-icon-box.image { color: #38bdf8; background: rgba(56, 189, 248, 0.1); }
.file-icon-box.video { color: #a78bfa; background: rgba(167, 139, 250, 0.1); }
.file-icon-box.audio { color: #f472b6; background: rgba(244, 114, 182, 0.1); }
.file-icon-box.zip { color: #fb923c; background: rgba(251, 146, 60, 0.1); }
.file-icon-box.code { color: #34d399; background: rgba(52, 211, 153, 0.1); }

.file-actions-cell {
  text-align: right;
  white-space: nowrap;
}

.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 16px;
  gap: 12px;
  color: var(--text-tertiary);
}

.empty-icon {
  width: 48px;
  height: 48px;
  border-radius: var(--radius-md);
  background: var(--surface-2);
  border: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-disabled);
}
.empty-icon svg { width: 24px; height: 24px; }

/* ═══════════════════════════════════════════════════════════════════════════════
   FULL-WINDOW DRAG OVERLAY
   ═══════════════════════════════════════════════════════════════════════════════ */
.window-drop-overlay {
  position: fixed;
  inset: 0;
  background: rgba(9, 10, 12, 0.9);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
}
.window-drop-overlay.active {
  opacity: 1;
  pointer-events: auto;
}

.window-drop-box {
  background: var(--surface-1);
  border: 2px dashed var(--brand-blue);
  border-radius: var(--radius-xl);
  padding: 48px 64px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  text-align: center;
  box-shadow: 0 0 50px rgba(79, 127, 255, 0.2);
}

/* ═══════════════════════════════════════════════════════════════════════════════
   MODAL DIALOGS & HOST FOLDER NAVIGATOR OVERHAUL
   ═══════════════════════════════════════════════════════════════════════════════ */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  z-index: 500;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  opacity: 0;
  pointer-events: none;
  transition: opacity 0.2s ease;
}
.modal-overlay.open {
  opacity: 1;
  pointer-events: auto;
}

.modal-content {
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-xl);
  width: 100%;
  max-width: 560px;
  box-shadow: var(--shadow-modal);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transform: scale(0.96);
  transition: transform 0.2s ease;
}
.modal-overlay.open .modal-content {
  transform: scale(1);
}

#hostBrowserModal .modal-content {
  max-width: 680px;
  width: 100%;
  max-height: 85vh;
  height: 640px;
  box-shadow: 0 24px 60px rgba(0, 0, 0, 0.8), 0 0 0 1px rgba(255, 255, 255, 0.08);
}

.modal-header {
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-standard);
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: var(--surface-1);
  flex-shrink: 0;
}

.modal-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 8px;
}

.modal-body {
  padding: 16px 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 75vh;
  overflow-y: auto;
}

.modal-footer {
  padding: 12px 20px;
  border-top: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  background: var(--surface-2);
  flex-shrink: 0;
}

/* Mobile Bottom Sheet Grab Handle */
.bottom-sheet-handle-bar {
  display: none;
  width: 100%;
  padding: 8px 0 2px 0;
  text-align: center;
  cursor: grab;
  flex-shrink: 0;
}

.bottom-sheet-handle {
  width: 36px;
  height: 4px;
  border-radius: var(--radius-pill);
  background: rgba(255, 255, 255, 0.22);
  margin: 0 auto;
}

/* QR Interface Switcher Chips */
.qr-interface-chips {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  padding-bottom: 4px;
  scrollbar-width: none;
}
.qr-interface-chips::-webkit-scrollbar { display: none; }

.qr-chip {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-pill);
  font-size: 11px;
  font-weight: 500;
  color: var(--text-secondary);
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.15s ease;
  user-select: none;
}

.qr-chip:hover {
  background: var(--surface-3);
  color: var(--text-primary);
  border-color: var(--border-hover);
}

.qr-chip.active {
  background: rgba(56, 189, 248, 0.15);
  color: #38bdf8;
  border-color: rgba(56, 189, 248, 0.4);
  box-shadow: 0 0 10px rgba(56, 189, 248, 0.2);
}

/* Drive Ribbon Wrapper & Chevrons */
.drive-ribbon-wrapper {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
}

.drive-ribbon-arrow {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  color: var(--text-secondary);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: all 0.15s ease;
  z-index: 2;
  padding: 0;
}

.drive-ribbon-arrow:hover {
  background: var(--surface-3);
  color: var(--text-primary);
  border-color: var(--border-hover);
  transform: translateY(-1px);
}

.drive-ribbon-arrow.disabled {
  opacity: 0.25;
  cursor: default;
  pointer-events: none;
  transform: none;
}

.drive-ribbon-arrow svg {
  width: 16px;
  height: 16px;
  stroke: currentColor;
  stroke-width: 2;
  fill: none;
  stroke-linecap: round;
  stroke-linejoin: round;
}

/* Drive Cards Ribbon */
.drive-cards-ribbon {
  display: flex;
  gap: 8px;
  overflow-x: auto;
  padding: 2px 0 6px 0;
  scroll-snap-type: x mandatory;
  -webkit-overflow-scrolling: touch;
  scrollbar-width: none;
  scroll-behavior: smooth;
  flex: 1;
  min-width: 0;
}
.drive-cards-ribbon::-webkit-scrollbar { display: none; }

.drive-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  padding: 8px 12px;
  min-width: 150px;
  flex: 1 0 150px;
  max-width: 220px;
  cursor: pointer;
  user-select: none;
  transition: all 0.15s ease;
  flex-shrink: 0;
  scroll-snap-align: start;
}

.drive-card:hover {
  background: var(--surface-3);
  border-color: var(--border-hover);
  transform: translateY(-1px);
}

.drive-card:active {
  transform: scale(0.98);
}

.drive-card.active {
  background: var(--surface-3);
  border-color: var(--brand-blue);
  box-shadow: 0 0 0 1px var(--brand-blue), 0 2px 12px var(--brand-blue-glow);
}

.drive-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.drive-card-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  min-width: 0;
}
.drive-card-title span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.drive-card.active .drive-card-title { color: var(--brand-blue); }

.drive-card-badge {
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 1px 5px;
  border-radius: var(--radius-pill);
  background: rgba(255, 255, 255, 0.08);
  color: var(--text-secondary);
  border: 1px solid var(--border-subtle);
  flex-shrink: 0;
}
.drive-card.active .drive-card-badge {
  background: rgba(79, 127, 255, 0.15);
  color: var(--brand-blue);
  border-color: rgba(79, 127, 255, 0.35);
}

.drive-card-meter {
  width: 100%;
  height: 4px;
  background: var(--surface-4);
  border-radius: var(--radius-pill);
  overflow: hidden;
}

.drive-card-meter-fill {
  height: 100%;
  border-radius: var(--radius-pill);
  background: var(--brand-blue);
  transition: width 0.3s ease;
}
.drive-card-meter-fill.warning { background: var(--status-warning); }
.drive-card-meter-fill.danger { background: var(--status-error); }

.drive-card-info {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 10px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  font-variant-numeric: tabular-nums;
}

/* Breadcrumbs Bar */
.modal-nav-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  padding: 4px 6px;
  min-height: 42px;
}

.modal-btn-up, .breadcrumb-edit-btn {
  width: 32px;
  height: 32px;
  min-height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  background: var(--surface-3);
  border: 1px solid var(--border-subtle);
  color: var(--text-secondary);
  cursor: pointer;
  flex-shrink: 0;
  transition: all 0.12s ease;
}
.modal-btn-up:hover:not(:disabled), .breadcrumb-edit-btn:hover {
  background: var(--surface-4);
  color: var(--text-primary);
  border-color: var(--border-hover);
}
.modal-btn-up:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.breadcrumb-trail-container {
  display: flex;
  align-items: center;
  gap: 3px;
  overflow-x: auto;
  white-space: nowrap;
  scrollbar-width: none;
  -webkit-overflow-scrolling: touch;
  flex: 1;
  min-width: 0;
  padding: 0 4px;
}
.breadcrumb-trail-container::-webkit-scrollbar { display: none; }

.crumb-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  min-height: 28px;
  border-radius: var(--radius-sm);
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font-family: var(--font-sans);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.12s ease;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  user-select: none;
}
.crumb-btn:hover { background: var(--surface-3); color: var(--text-primary); }
.crumb-btn.active { color: var(--text-primary); font-weight: 600; cursor: default; }

.crumb-divider {
  color: var(--text-disabled);
  font-size: 11px;
  display: inline-flex;
  align-items: center;
  user-select: none;
}

/* Quick Filter Toolbar */
.modal-filter-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.modal-filter-box {
  position: relative;
  flex: 1;
  display: flex;
  align-items: center;
}

.modal-filter-box .filter-icon {
  position: absolute;
  left: 10px;
  width: 14px;
  height: 14px;
  color: var(--text-tertiary);
  pointer-events: none;
}

.modal-filter-input {
  width: 100%;
  min-height: 38px;
  padding: 6px 30px 6px 32px;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  color: var(--text-primary);
  font-size: 12px;
  font-family: var(--font-sans);
  outline: none;
  transition: all 0.15s ease;
}
.modal-filter-input:focus {
  border-color: var(--brand-blue);
  box-shadow: 0 0 0 2px var(--brand-blue-glow);
}

.modal-filter-clear-btn {
  position: absolute;
  right: 4px;
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  border-radius: var(--radius-xs);
  color: var(--text-tertiary);
  cursor: pointer;
}
.modal-filter-clear-btn:hover { color: var(--text-primary); background: var(--surface-3); }
.modal-filter-clear-btn svg { width: 14px; height: 14px; stroke: currentColor; fill: none; stroke-width: 2; }

.modal-filter-badge {
  font-size: 11px;
  font-family: var(--font-mono);
  color: var(--text-tertiary);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
  background: var(--surface-3);
  padding: 2px 6px;
  border-radius: var(--radius-pill);
}

/* Inline New Folder Creator */
.inline-folder-creator {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--surface-3);
  border: 1px solid var(--brand-blue);
  border-radius: var(--radius-md);
  padding: 6px 10px;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.3);
}

.inline-folder-input {
  flex: 1;
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-sm);
  padding: 5px 10px;
  font-size: 12px;
  color: var(--text-primary);
  outline: none;
  font-family: var(--font-sans);
}
.inline-folder-input:focus { border-color: var(--brand-blue); }

/* Folder Tree Container & Rows */
.modal-tree-container {
  flex: 1;
  min-height: 180px;
  max-height: 280px;
  overflow-y: auto;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  display: flex;
  flex-direction: column;
  -webkit-overflow-scrolling: touch;
  outline: none;
}

.folder-tree-item {
  padding: 9px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--border-subtle);
  cursor: pointer;
  font-size: 13px;
  color: var(--text-primary);
  min-height: 40px;
  transition: background 0.1s ease;
  user-select: none;
}
.folder-tree-item:last-child { border-bottom: none; }
.folder-tree-item:hover, .folder-tree-item.focused { background: var(--surface-3); }
.folder-tree-item:active { background: var(--surface-4); }

.folder-tree-item-name {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 500;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ═══════════════════════════════════════════════════════════════════════════════
   FAQ ACCORDION & SEARCH STYLES (R4)
   ═══════════════════════════════════════════════════════════════════════════════ */
.faq-container {
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 100%;
}

.faq-search-wrapper {
  position: relative;
  width: 100%;
  margin-bottom: 4px;
}

.faq-search-wrapper .search-input {
  width: 100%;
  padding: 10px 14px 10px 36px;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  color: var(--text-primary);
  font-size: 12px;
  outline: none;
  transition: all 0.15s ease;
}
.faq-search-wrapper .search-input:focus {
  border-color: var(--brand-blue);
  box-shadow: 0 0 0 2px var(--brand-blue-glow);
}

.faq-search-wrapper svg {
  position: absolute;
  left: 11px;
  top: 50%;
  transform: translateY(-50%);
  width: 15px;
  height: 15px;
  color: var(--text-tertiary);
  pointer-events: none;
}

.faq-category-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.faq-pill {
  font-size: 11px;
  font-weight: 500;
  padding: 5px 10px;
  background: var(--surface-2);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-pill);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s ease;
  user-select: none;
  min-height: 28px;
  display: inline-flex;
  align-items: center;
}
.faq-pill:hover {
  background: var(--surface-3);
  color: var(--text-primary);
  border-color: var(--border-hover);
}
.faq-pill.active {
  background: var(--brand-blue-glow);
  color: var(--brand-blue);
  border-color: var(--brand-blue);
}

.faq-item {
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  overflow: hidden;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.faq-item:hover {
  border-color: var(--border-hover);
}
.faq-item.open {
  border-color: var(--border-accent);
  background: var(--surface-3);
}

.faq-question {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  cursor: pointer;
  user-select: none;
  gap: 12px;
  min-height: 44px;
}

.faq-q-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-weight: 600;
  font-size: 12px;
  color: var(--text-primary);
  text-align: left;
  flex: 1;
}

.faq-icon {
  width: 16px;
  height: 16px;
  color: var(--brand-blue);
  flex-shrink: 0;
}
.faq-item.open .faq-icon {
  color: #60a5fa;
}

.chevron {
  width: 16px;
  height: 16px;
  color: var(--text-tertiary);
  transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
  flex-shrink: 0;
}
.faq-item.open .chevron {
  transform: rotate(180deg);
  color: var(--text-primary);
}

.faq-answer {
  display: none;
  padding: 0 14px 14px 40px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  border-top: 1px solid transparent;
}
.faq-item.open .faq-answer {
  display: block;
  border-top: 1px solid var(--border-subtle);
  padding-top: 10px;
}
.faq-answer p {
  margin-bottom: 8px;
}
.faq-answer p:last-child {
  margin-bottom: 0;
}
.faq-answer ul, .faq-answer ol {
  margin-left: 18px;
  margin-bottom: 8px;
}
.faq-answer li {
  margin-bottom: 4px;
}
.faq-answer code {
  font-family: var(--font-mono);
  font-size: 11px;
  padding: 2px 5px;
  background: var(--surface-4);
  border-radius: var(--radius-xs);
  color: #38bdf8;
  border: 1px solid var(--border-subtle);
}

.faq-mode-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 8px;
  margin: 8px 0;
}
.faq-mode-card {
  background: var(--surface-4);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-sm);
  padding: 8px 10px;
}
.mode-title {
  font-weight: 600;
  font-size: 11px;
  margin-bottom: 2px;
}
.mode-desc {
  font-size: 11px;
  color: var(--text-secondary);
}

/* ═══════════════════════════════════════════════════════════════════════════════
   TOAST NOTIFICATIONS
   ═══════════════════════════════════════════════════════════════════════════════ */
.toast-container {
  position: fixed;
  bottom: 24px;
  right: 24px;
  z-index: 2000;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
  max-width: 360px;
  width: 100%;
}

.toast-item {
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-md);
  padding: 10px 14px;
  box-shadow: var(--shadow-raised);
  display: flex;
  align-items: center;
  gap: 10px;
  pointer-events: auto;
  transform: translateY(20px);
  opacity: 0;
  transition: all 0.25s cubic-bezier(0.16, 1, 0.3, 1);
}
.toast-item.show {
  transform: translateY(0);
  opacity: 1;
}
.toast-item.toast-success { border-color: rgba(16, 185, 129, 0.4); }
.toast-item.toast-error { border-color: rgba(239, 68, 68, 0.4); }
.toast-item.toast-info { border-color: rgba(56, 189, 248, 0.4); }

.toast-icon {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.toast-icon svg { width: 16px; height: 16px; }
.toast-success .toast-icon { color: var(--status-success); }
.toast-error .toast-icon { color: var(--status-error); }
.toast-info .toast-icon { color: var(--status-info); }

.toast-content {
  flex: 1;
  min-width: 0;
}

.toast-title {
  font-size: 11px;
  font-weight: 600;
  color: var(--text-primary);
}

.toast-desc {
  font-size: 11px;
  color: var(--text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ═══════════════════════════════════════════════════════════════════════════════
   RESPONSIVE MEDIA QUERIES
   ═══════════════════════════════════════════════════════════════════════════════ */
@media (max-width: 860px) {
  .app-container {
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
    padding: 16px;
    min-width: 0;
  }
  .sidebar, .main-content, .card, .explorer-card {
    min-width: 0;
    max-width: 100%;
  }
  .btn, .icon-btn { min-height: 44px; }
  .tab-btn { min-height: 44px; padding: 0 14px; }
  .upload-actions-mobile { display: flex; gap: 8px; }
  .modal-overlay { align-items: flex-end; padding: 0; }
  .modal-content { max-width: 100%; border-radius: var(--radius-xl) var(--radius-xl) 0 0; max-height: 90dvh; }

  #hostBrowserModal.modal-overlay {
    align-items: flex-end;
    padding: 0;
  }
  #hostBrowserModal .modal-content {
    max-width: 100%;
    width: 100%;
    height: 88dvh;
    max-height: 88dvh;
    border-radius: var(--radius-xl) var(--radius-xl) 0 0;
    border-bottom: none;
    transform: translateY(100%);
    transition: transform 0.28s cubic-bezier(0.32, 0.72, 0, 1);
  }
  #hostBrowserModal.open .modal-content {
    transform: translateY(0);
  }
  .bottom-sheet-handle-bar {
    display: block;
  }
  #hostBrowserModal .modal-header {
    position: sticky;
    top: 0;
    z-index: 10;
  }
  #hostBrowserModal .drive-card {
    min-width: 140px;
    min-height: 52px;
    padding: 8px 12px;
  }
  #hostBrowserModal .modal-nav-bar {
    min-height: 46px;
  }
  #hostBrowserModal .modal-btn-up, #hostBrowserModal .breadcrumb-edit-btn {
    width: 36px;
    height: 36px;
    min-height: 36px;
  }
  #hostBrowserModal .crumb-btn {
    min-height: 38px;
    padding: 6px 10px;
    font-size: 13px;
  }
  #hostBrowserModal .modal-filter-input {
    min-height: 44px;
    font-size: 13px;
  }
  #hostBrowserModal .folder-tree-item {
    min-height: 48px;
    padding: 12px 14px;
    font-size: 13px;
  }
  #hostBrowserModal .modal-footer {
    position: sticky;
    bottom: 0;
    z-index: 10;
    padding: 12px 16px;
    padding-bottom: max(12px, env(safe-area-inset-bottom));
  }
  #hostBrowserModal .modal-footer .btn-primary {
    min-height: 48px;
    font-size: 14px;
    font-weight: 600;
    flex: 1;
  }
  #hostBrowserModal .modal-footer .btn-ghost {
    min-height: 44px;
  }
}

@media (max-width: 600px) {
  .header-inner {
    padding: 8px 12px;
    gap: 8px;
  }
  .status-pill {
    display: none !important;
  }
  .header-actions {
    gap: 6px;
    flex-shrink: 0;
  }
  .network-bar-inner {
    padding: 6px 12px;
    max-width: 100%;
  }
  .net-item {
    min-width: 180px;
  }
  .net-scroll-container.grid-mode {
    grid-template-columns: 1fr;
  }
  .explorer-header {
    padding: 10px 12px;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .explorer-tabs {
    width: 100%;
    display: grid;
    grid-template-columns: 1fr 1fr;
  }
  .tab-btn {
    justify-content: center;
  }
  .explorer-toolbar {
    padding: 8px 12px;
    flex-direction: column;
    align-items: stretch;
    gap: 8px;
  }
  .search-box {
    max-width: 100%;
    min-width: 0;
    width: 100%;
  }
}

@media (max-width: 480px) {
  .app-container {
    padding: 10px 8px;
    gap: 12px;
  }
  .card {
    padding: 12px 10px;
  }
  .header-inner {
    padding: 8px 10px;
    gap: 6px;
  }
  .brand-title {
    font-size: 14px;
  }
  .brand-icon {
    width: 28px;
    height: 28px;
  }
  .brand-icon svg {
    width: 16px;
    height: 16px;
  }
  
  /* Collapse text labels in header action buttons to icons only */
  .header-actions .btn .btn-label,
  .header-actions .btn-label,
  .header-actions .btn-text {
    display: none;
  }
  .header-actions .btn {
    padding: 0;
    width: 44px;
    height: 44px;
    min-width: 44px;
    min-height: 44px;
    justify-content: center;
  }
  .header-actions .btn svg {
    margin: 0;
  }
  .header-actions {
    gap: 4px;
  }

  /* Hide scroll chevrons on mobile in favor of touch-drag swipe */
  .network-bar-inner {
    padding: 6px 8px;
    gap: 6px;
  }
  .scroll-chevron,
  .drive-ribbon-arrow {
    display: none !important;
  }
  .net-item {
    min-width: 160px;
    padding: 6px 10px;
  }
  .net-item-url {
    font-size: 11px;
  }
  .net-scroll-container.grid-mode .net-item {
    min-width: 0;
    width: 100%;
  }

  /* Responsive Card Buttons */
  .card-button-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    width: 100%;
  }
  .card-button-row .btn {
    flex: 1 1 calc(50% - 6px);
    min-width: 0;
    min-height: 44px;
    font-size: 11px;
    padding: 6px 4px;
    justify-content: center;
    text-align: center;
  }

  /* File Explorer Responsiveness */
  .explorer-header .header-actions {
    width: 100%;
    display: flex;
    justify-content: space-between;
    gap: 6px;
  }
  .explorer-header .header-actions .btn {
    flex: 1;
    min-height: 44px;
  }
  .table-wrapper {
    width: 100%;
    max-width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
  }
  .file-table {
    min-width: 280px;
    font-size: 11px;
  }
  .file-table th, .file-table td {
    padding: 6px 8px;
  }
  .file-name-cell {
    min-width: 0;
    max-width: 140px;
    gap: 6px;
  }
  .file-name-cell span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .file-icon-box {
    width: 24px;
    height: 24px;
  }
  .file-icon-box svg {
    width: 14px;
    height: 14px;
  }

  /* Modal Responsiveness */
  .modal-header {
    padding: 10px 14px;
  }
  .modal-body {
    padding: 12px 14px;
    gap: 10px;
  }
  .modal-footer {
    padding: 10px 14px;
    flex-wrap: wrap;
  }
  .drives-chip-bar {
    gap: 6px;
    padding-bottom: 2px;
  }
  .drive-chip {
    padding: 4px 8px;
    font-size: 10px;
    min-height: 44px;
  }
}

@media (max-width: 360px) {
  .app-container {
    padding: 6px 4px;
    gap: 8px;
  }
  .card {
    padding: 10px 8px;
  }
  .header-inner {
    padding: 6px 8px;
    gap: 4px;
  }
  .brand-title {
    font-size: 13px;
  }
  .network-bar-inner {
    padding: 4px 6px;
  }
  .net-item {
    min-width: 140px;
    padding: 5px 8px;
  }
  .card-button-row .btn {
    flex: 1 1 100%;
    min-height: 44px;
  }
  .tab-btn {
    font-size: 10px;
    padding: 5px 2px;
    min-height: 44px;
  }
}

/* Security & Remote Sessions UI Elements */
.badge-count {
  background: #0284c7;
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  padding: 1px 6px;
  border-radius: 999px;
  margin-left: 4px;
}
.remote-auth-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  background: rgba(16, 185, 129, 0.12);
  border: 1px solid rgba(16, 185, 129, 0.3);
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 12px;
  color: #34d399;
  font-weight: 500;
}
.remote-auth-pill .btn-logout {
  background: rgba(239, 68, 68, 0.2);
  border: 1px solid rgba(239, 68, 68, 0.4);
  color: #fca5a5;
  border-radius: 6px;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
}
.remote-auth-pill .btn-logout:hover {
  background: rgba(239, 68, 68, 0.4);
  color: #fff;
}
.pulse-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #10b981;
  box-shadow: 0 0 8px #10b981;
  animation: pulseAnim 2s infinite;
}
@keyframes pulseAnim {
  0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
  70% { transform: scale(1); box-shadow: 0 0 0 6px rgba(16, 185, 129, 0); }
  100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
}
.sec-card-box {
  background: var(--surface-1);
  border: 1px solid var(--border-subtle);
  border-radius: 12px;
  padding: 14px 16px;
}
.sessions-scroll-table {
  max-height: 240px;
  overflow-y: auto;
  border: 1px solid var(--border-standard);
  border-radius: 8px;
  background: var(--surface-2);
}
.sessions-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}
.sessions-table th {
  background: var(--surface-3);
  color: var(--text-tertiary);
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  padding: 8px 12px;
  text-align: left;
  position: sticky;
  top: 0;
  z-index: 1;
}
.sessions-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-secondary);
}
.sessions-table tr:last-child td {
  border-bottom: none;
}
.session-status-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
}
.session-status-badge.active {
  background: rgba(34, 197, 94, 0.15);
  color: #4ade80;
  border: 1px solid rgba(34, 197, 94, 0.3);
}
.session-status-badge.revoked {
  background: rgba(239, 68, 68, 0.15);
  color: #fca5a5;
  border: 1px solid rgba(239, 68, 68, 0.3);
}
.btn-danger-outline {
  background: transparent;
  border: 1px solid rgba(239, 68, 68, 0.4);
  color: #f87171;
  border-radius: 6px;
  cursor: pointer;
  padding: 4px 10px;
  font-size: 11px;
  font-weight: 600;
  transition: all 0.2s;
}
.btn-danger-outline:hover {
  background: rgba(239, 68, 68, 0.15);
  border-color: #ef4444;
}
</style>
</head>
<body>

<!-- Global Header -->
<header class="header">
  <div class="header-inner">
    <div class="brand">
      <div class="brand-icon">
        <svg viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
      </div>
      <div class="brand-title">
        HostDrop
        <span class="status-pill host-badge" id="hostStatusPill" title="Host PC Only: 127.0.0.1:__PORT__ (Cannot be accessed by other devices)">
          <span class="status-dot"></span>
          Host Computer &bull; 127.0.0.1:__PORT__ (Host Only)
        </span>
        <span class="status-pill guest-badge" id="guestStatusPill" style="display: none;">
          <span class="status-dot" style="background: #38bdf8; box-shadow: 0 0 8px #38bdf8;"></span>
          Connected to PC &bull; :__PORT__
        </span>
      </div>
    </div>

    <div class="header-actions">
      <div id="authStatusBadge"></div>
      <button class="btn btn-ghost btn-sm host-only-btn" id="btnSecurityModal" onclick="openSecurityModal()" title="View remote sessions, revoke access, and change password" style="display: none;">
        <svg class="icon" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></svg>
        <span class="btn-label">Security &amp; Sessions</span>
        <span class="badge-count" id="headerSessionCount" style="display: none;">0</span>
      </button>
      <button class="btn btn-ghost btn-sm" onclick="showGeneralQR()" title="Show Web QR Code">
        <svg class="icon" viewBox="0 0 24 24"><rect width="6" height="6" x="3" y="3" rx="1.5"/><rect width="6" height="6" x="15" y="3" rx="1.5"/><rect width="6" height="6" x="3" y="15" rx="1.5"/><path d="M15 15h2v2h-2z"/><path d="M19 15h2v6h-6v-2h4v-4z"/><path d="M7 7h.01"/><path d="M17 7h.01"/><path d="M7 17h.01"/></svg>
        <span class="btn-label">QR Connect</span>
      </button>
      <button class="btn btn-ghost btn-sm" onclick="openModal('guideModal')" title="How to connect & Beginner FAQ">
        <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        <span class="btn-label">Guide</span>
      </button>
      <button class="icon-btn" onclick="refreshAll()" title="Refresh Hub">
        <svg class="icon" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
      </button>
    </div>
  </div>
</header>

<!-- Network Links Ribbon (Horizontal Wheel Scroll, Touch Swipe, Grid Toggle) -->
<section class="network-bar">
  <div class="network-bar-header" style="max-width: var(--page-max-width); margin: 0 auto 6px auto; padding: 0 12px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
    <div style="display: flex; align-items: center; gap: 6px; font-size: 11px; font-weight: 600; color: var(--text-secondary); text-transform: uppercase; letter-spacing: 0.5px;">
      <span class="status-dot" style="background: #22c55e; box-shadow: 0 0 6px #22c55e;"></span>
      <span>Network Addresses for Other Devices (Phones, Tablets & PCs)</span>
    </div>
    <div class="host-notice-text" style="font-size: 10px; color: var(--text-tertiary); display: flex; align-items: center; gap: 6px;">
      <span>Host PC:</span>
      <code class="mono" style="color: var(--text-secondary); background: var(--surface-2); padding: 1px 6px; border-radius: 4px; border: 1px solid var(--border-standard);">127.0.0.1:__PORT__</code>
      <span style="color: #f87171;">(Host Only &bull; No QR)</span>
    </div>
  </div>
  <div class="network-bar-inner">
    <button class="scroll-chevron" id="chevronLeft" onclick="scrollRibbon(-260)" title="Scroll Left">
      <svg class="icon" viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
    </button>

    <div class="net-scroll-wrapper">
      <div class="net-scroll-container" id="netScrollContainer">
        __NET_ITEMS__
      </div>
    </div>

    <button class="scroll-chevron" id="chevronRight" onclick="scrollRibbon(260)" title="Scroll Right">
      <svg class="icon" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
    </button>

    <button class="icon-btn" id="gridToggleBtn" onclick="toggleNetworkGridMode()" title="Toggle Ribbon / Grid View">
      <svg class="icon" viewBox="0 0 24 24"><rect width="7" height="7" x="3" y="3" rx="1"/><rect width="7" height="7" x="14" y="3" rx="1"/><rect width="7" height="7" x="14" y="14" rx="1"/><rect width="7" height="7" x="3" y="14" rx="1"/></svg>
    </button>
  </div>
</section>

<!-- Main Workbench Grid -->
<main class="app-container">

  <!-- Left Sidebar Controls -->
  <aside class="sidebar">

    <!-- Card 1: Inbox Storage (Where Sent Files Go) -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">
          <svg class="icon" style="color: var(--brand-blue);" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Inbox Folder on PC
        </div>
        <span class="tab-badge">Where Sent Files Go</span>
      </div>

      <div class="card-helper-text">
        Files sent from your phone or other computers are saved here on the PC.
      </div>

      <div class="target-box">
        <div class="target-path-row">
          <span class="target-path" id="recvPathText">__RECV_PATH__</span>
          <button class="icon-btn-micro" onclick="copyAddress(document.getElementById('recvPathText').textContent)" title="Copy Path">
            <svg viewBox="0 0 24 24"><rect width="13" height="13" x="9" y="9" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          </button>
        </div>

        <div class="storage-meter">
          <div class="storage-meter-labels">
            <span>Free Storage</span>
            <span class="tabular-nums" id="recvDiskLabel">__RECV_FREE_GB__ GB Free (__RECV_USED_PCT__% used)</span>
          </div>
          <div class="storage-track">
            <div class="storage-fill __RECV_WARN_CLASS__" id="recvDiskBar" style="width: __RECV_USED_PCT__%;"></div>
          </div>
        </div>
      </div>

      <div class="card-button-row">
        <button class="btn btn-sm" onclick="openHostBrowserModal('recv')" title="Browse folders in browser">
          <svg class="icon" viewBox="0 0 24 24"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5c0-1.1.9-2 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>
          Change Folder
        </button>
        <button class="btn btn-sm" id="recvNativePickerBtn" onclick="triggerNativePicker('recv')" title="Launch Windows Folder Dialog on Host">
          <svg class="icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
          Windows Dialog
        </button>
        <button class="btn btn-sm btn-ghost" id="recvOpenExplorerBtn" onclick="openInExplorer('recv')" title="Open folder in Windows Explorer">
          <svg class="icon" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          Open in Windows Explorer
        </button>
      </div>
    </div>

    <!-- Card 2: Shared Library (Files from PC) -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">
          <svg class="icon" style="color: #fbbf24;" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
          Share Folder from PC
        </div>
        <span class="tab-badge">Shared Library</span>
      </div>

      <div class="card-helper-text">
        Pick a folder on your PC (e.g. Movies, Games, or Documents) if you want connected devices to download it.
      </div>

      <div class="target-box">
        <div class="target-path-row">
          <span class="target-path" id="sharePathText">__SHARE_PATH__</span>
          <button class="icon-btn-micro" onclick="copyAddress(document.getElementById('sharePathText').textContent)" title="Copy Path">
            <svg viewBox="0 0 24 24"><rect width="13" height="13" x="9" y="9" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          </button>
        </div>

        <div class="storage-meter" id="shareStorageMeter" style="display: __SHARE_METER_DISPLAY__;">
          <div class="storage-meter-labels">
            <span>Free Storage</span>
            <span class="tabular-nums" id="shareDiskLabel">__SHARE_FREE_GB__ GB Free (__SHARE_USED_PCT__% used)</span>
          </div>
          <div class="storage-track">
            <div class="storage-fill __SHARE_WARN_CLASS__" id="shareDiskBar" style="width: __SHARE_USED_PCT__%;"></div>
          </div>
        </div>
      </div>

      <div class="card-button-row">
        <button class="btn btn-sm" onclick="openHostBrowserModal('share')" title="Select folder to share">
          <svg class="icon" viewBox="0 0 24 24"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5c0-1.1.9-2 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>
          Select Folder to Share
        </button>
        <button class="btn btn-sm" id="shareNativePickerBtn" onclick="triggerNativePicker('share')" title="Launch Windows Folder Dialog on Host">
          <svg class="icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
          Windows Dialog
        </button>
        <button class="btn btn-sm btn-ghost" id="shareOpenExplorerBtn" onclick="openInExplorer('share')" title="Open folder in Windows Explorer">
          <svg class="icon" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          Open in Windows Explorer
        </button>
      </div>
    </div>

    <!-- Card 3: Send Files to PC Dropzone -->
    <div class="card">
      <div class="card-header">
        <div class="card-title">
          <svg class="icon" style="color: var(--brand-blue);" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Send Files to PC
        </div>
      </div>

      <div class="card-helper-text">
        Drop photos, videos, or folders here to transfer them directly to the PC's Inbox.
      </div>

      <div class="dropzone" id="sidebarDropzone" onclick="document.getElementById('fileInput').click()">
        <input type="file" id="fileInput" multiple style="display: none;" onchange="handleFileSelect(this.files)">
        <input type="file" id="folderInput" webkitdirectory directory multiple style="display: none;" onchange="handleFileSelect(this.files)">
        <div class="dropzone-icon">
          <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        </div>
        <div class="dropzone-title">Drop files or folders anywhere, or tap to choose</div>
        <div class="dropzone-desc">Instant 2-way transfer &bull; Auto-resumes if disconnected</div>
        
        <div class="upload-actions-mobile">
          <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); document.getElementById('fileInput').click()">
            Send Files
          </button>
          <button class="btn btn-sm" onclick="event.stopPropagation(); document.getElementById('folderInput').click()">
            Send Folder
          </button>
        </div>
      </div>

      <!-- Active Transfer Progress Box -->
      <div class="transfer-card" id="transferCard">
        <div class="transfer-header">
          <span class="transfer-file-name" id="transFileName">Uploading...</span>
          <span class="tab-badge tabular-nums" id="transPercent">0%</span>
        </div>
        <div class="progress-track">
          <div class="progress-fill" id="transProgressFill"></div>
        </div>
        <div class="transfer-meta tabular-nums">
          <span id="transSpeed">0.0 MB/s</span>
          <span id="transStatus">Preparing...</span>
        </div>
      </div>
    </div>

  </aside>

  <!-- Right File Explorer Workbench -->
  <section class="main-content">

    <!-- Guest Welcome & Guidance Banner (shown dynamically on remote devices) -->
    <div class="guest-banner" id="guestBanner" style="display: none;">
      <div class="guest-banner-header">
        <div class="guest-banner-title">
          <svg class="icon" viewBox="0 0 24 24"><path d="M5 12h14"/><path d="m12 5 7 7-7 7"/></svg>
          Connected to PC Hub
        </div>
        <span class="guest-badge-pill">Guest Device</span>
      </div>
      <div class="guest-steps-grid">
        <div class="guest-step-card">
          <div class="step-number">1</div>
          <div class="step-content">
            <div class="step-title">Send Files to the PC</div>
            <div class="step-desc">Tap "Send Files" or "Send Folder" to transfer photos, videos, or documents directly into the PC's Inbox.</div>
          </div>
        </div>
        <div class="guest-step-card">
          <div class="step-number">2</div>
          <div class="step-content">
            <div class="step-title">Download Files from PC Library</div>
            <div class="step-desc">Switch to the "Library (Shared by PC)" tab to browse and download files shared by the host computer.</div>
          </div>
        </div>
      </div>
    </div>

    <div class="explorer-card">

      <!-- Explorer Top Bar with Dual Tabs -->
      <div class="explorer-header">
        <div class="explorer-tabs">
          <button class="tab-btn active" id="tabRecvBtn" onclick="switchTab('recv')">
            <svg class="icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Inbox (Sent to PC)
            <span class="tab-badge" id="recvCountBadge">0</span>
          </button>
          <button class="tab-btn" id="tabShareBtn" onclick="switchTab('share')">
            <svg class="icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
            Library (Shared by PC)
            <span class="tab-badge" id="shareCountBadge">0</span>
          </button>
        </div>

        <div class="header-actions">
          <button class="btn btn-sm" onclick="downloadCurrentFolderZip()" title="Download current folder as ZIP archive">
            <svg class="icon" viewBox="0 0 24 24"><rect width="8" height="4" x="8" y="3" rx="1"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            <span class="btn-label">Download ZIP</span>
          </button>
          <button class="btn btn-sm" onclick="saveCurrentFolderStructure()" title="Save folder structure directly to local disk (or ZIP fallback)">
            <svg class="icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/><polyline points="12 11 12 17 15 14"/><line x1="9" y1="14" x2="12" y2="17"/></svg>
            <span class="btn-label">Save as Folder</span>
          </button>
          <button class="icon-btn" onclick="refreshActiveDirectory()" title="Refresh file list">
            <svg class="icon" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
          </button>
        </div>
      </div>

      <!-- Tab Contextual Helper Subtext -->
      <div class="tab-helper-bar" id="tabHelperBar">
        Files transferred to this computer from connected devices
      </div>

      <!-- Explorer Sub-Toolbar (Search and Breadcrumb) -->
      <div class="explorer-toolbar">
        <div class="search-box">
          <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input type="text" class="search-input" id="tableSearchInput" placeholder="Filter files in folder..." oninput="handleSearchFilter(this.value)">
        </div>

        <div class="explorer-breadcrumbs" id="explorerBreadcrumbs">
          <span class="crumb-item" onclick="navigateBreadcrumb('')">Inbox Root</span>
        </div>
      </div>

      <!-- File Table -->
      <div class="table-wrapper">
        <table class="file-table" id="fileTable">
          <thead>
            <tr>
              <th style="width: 45%;">Name</th>
              <th style="width: 20%;">Size</th>
              <th style="width: 35%; text-align: right;">Action</th>
            </tr>
          </thead>
          <tbody id="fileTableBody">
            <!-- Dynamic File Items Injected via JS -->
          </tbody>
        </table>

        <!-- Empty State -->
        <div class="empty-state" id="emptyState" style="display: none;">
          <div class="empty-icon">
            <svg viewBox="0 0 24 24"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/></svg>
          </div>
          <div style="font-weight: 600; color: var(--text-primary);">This folder is currently empty</div>
          <div style="font-size: 11px;">Drop files or folders anywhere to send them to this PC.</div>
        </div>
      </div>

    </div>
  </section>

</main>

<!-- Full-Window Drag-and-Drop Overlay -->
<div class="window-drop-overlay" id="windowDropOverlay">
  <div class="window-drop-box">
    <div class="dropzone-icon" style="width: 64px; height: 64px;">
      <svg style="width: 32px; height: 32px;" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
    </div>
    <div style="font-size: 18px; font-weight: 600; color: var(--text-primary);">Drop files or folders anywhere to send to PC</div>
    <div style="font-size: 12px; color: var(--text-secondary);">Saving to: <span class="mono" id="dropTargetName">Inbox Folder on PC</span></div>
  </div>
</div>

<!-- Master Passcode Authentication Modal -->
<div class="modal-overlay" id="authModal" role="dialog" aria-modal="true" aria-labelledby="authModalTitle" style="display: none;">
  <div class="modal-content" style="max-width: 440px;">
    <div class="modal-header">
      <div class="modal-title">
        <svg class="icon" style="color: #60a5fa;" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
        <span id="authModalTitle">HostDrop Security Passcode</span>
      </div>
      <button class="icon-btn-micro" onclick="closeModal('authModal')" aria-label="Close dialog">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="modal-body" style="padding: 18px 20px; gap: 14px;">
      <div style="font-size: 13px; color: var(--text-secondary); line-height: 1.5;">
        Enter the <strong>Master App Passcode</strong> or your <strong>Bookmark Key</strong> to unlock remote storage controls and full access.
      </div>
      <div>
        <label for="authPasswordInput" style="display: block; font-size: 11px; font-weight: 600; text-transform: uppercase; color: var(--text-tertiary); margin-bottom: 6px; letter-spacing: 0.5px;">Passcode or Key</label>
        <input type="password" id="authPasswordInput" placeholder="Enter passcode..." autocomplete="current-password" class="modal-path-input" style="width: 100%; box-sizing: border-box; font-family: var(--font-mono); font-size: 14px; padding: 10px 12px;" onkeydown="if(event.key==='Enter') submitAuthLogin()">
      </div>
      <div id="authErrorMsg" style="display: none; padding: 8px 12px; border-radius: 6px; background: rgba(239, 68, 68, 0.15); border: 1px solid rgba(239, 68, 68, 0.3); color: #fca5a5; font-size: 12px;"></div>
      <div style="display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px;">
        <button class="btn btn-secondary btn-sm" onclick="closeModal('authModal')">Cancel</button>
        <button class="btn btn-primary btn-sm" id="authSubmitBtn" onclick="submitAuthLogin()">Unlock Access</button>
      </div>
    </div>
  </div>
</div>

<!-- Host Security & Sessions Management Modal (Localhost Host Only) -->
<div class="modal-overlay" id="securityModal" role="dialog" aria-modal="true" aria-labelledby="securityModalTitle">
  <div class="modal-content" style="max-width: 680px; max-height: 85vh; display: flex; flex-direction: column;">
    <div class="modal-header">
      <div class="modal-title">
        <svg class="icon" style="color: #38bdf8;" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><circle cx="12" cy="12" r="3"/></svg>
        <span id="securityModalTitle">Host Security &amp; Remote Sessions</span>
      </div>
      <button class="icon-btn-micro" onclick="closeModal('securityModal')" aria-label="Close dialog">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="modal-body" style="padding: 16px 20px; gap: 18px; overflow-y: auto; flex: 1;">
      
      <!-- Section 0: Active Master Passcode & Security Tip -->
      <div class="sec-card-box" id="activePasscodeCard">
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 8px;">
          <div>
            <div style="font-size: 14px; font-weight: 600; color: var(--text-primary); display: flex; align-items: center; gap: 6px;">
              <span>Active Master Passcode</span>
              <span id="passcodeTypeBadge" style="font-size: 10px; padding: 2px 6px; border-radius: 4px; background: rgba(56,189,248,0.15); color: #38bdf8; font-weight: 500;">PBKDF2-HMAC-SHA256</span>
            </div>
            <div style="font-size: 11px; color: var(--text-secondary);">Used by remote devices to unlock HostDrop</div>
          </div>
          <div style="display: flex; align-items: center; gap: 8px;">
            <code id="hostPasscodeDisplay" style="font-family: var(--font-mono); font-size: 14px; font-weight: 700; color: #38bdf8; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); padding: 4px 10px; border-radius: 6px; letter-spacing: 0.5px;">Loading...</code>
            <button class="btn btn-secondary btn-sm" id="btnCopyPasscode" onclick="copyHostPasscode()" title="Copy Passcode">
              <svg class="icon" viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
              <span id="btnCopyPasscodeText">Copy</span>
            </button>
          </div>
        </div>
        <div id="hostPasscodeTip" style="font-size: 12px; color: var(--text-tertiary); background: rgba(255,255,255,0.03); border-left: 3px solid #38bdf8; padding: 6px 10px; border-radius: 0 4px 4px 0; margin-top: 6px;">
          Tip: We recommend setting your own personal passcode, though your auto-generated code is active and secure.
        </div>
      </div>

      <!-- Section 1: Active Remote Sessions -->
      <div class="sec-card-box">
        <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 12px;">
          <div>
            <div style="font-size: 14px; font-weight: 600; color: var(--text-primary);">Active Remote Sessions</div>
            <div style="font-size: 11px; color: var(--text-secondary);">Devices connected via Cloudflare Tunnel or Remote Passcode</div>
          </div>
          <div style="display: flex; gap: 8px;">
            <button class="btn btn-secondary btn-sm" onclick="refreshSessionsList()">
              <svg class="icon" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
              <span>Refresh</span>
            </button>
            <button class="btn-danger-outline btn-sm" onclick="revokeAllSessions()">Revoke All</button>
          </div>
        </div>

        <div class="sessions-scroll-table">
          <table class="sessions-table">
            <thead>
              <tr>
                <th>Device</th>
                <th>IP &amp; Location</th>
                <th>Issued</th>
                <th>Last Active</th>
                <th>Status</th>
                <th style="text-align: right;">Action</th>
              </tr>
            </thead>
            <tbody id="sessionsTableBody">
              <tr><td colspan="6" style="text-align: center; color: var(--text-tertiary); padding: 24px;">Loading active sessions...</td></tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Section 2: Change Master Passcode -->
      <div class="sec-card-box">
        <div style="font-size: 14px; font-weight: 600; color: var(--text-primary); margin-bottom: 4px;">Update Master Passcode</div>
        <div style="font-size: 11px; color: var(--text-secondary); margin-bottom: 14px;">Change the password required for remote access outside this computer</div>
        
        <form onsubmit="handleHostPasswordChange(event)" style="display: flex; flex-direction: column; gap: 10px;">
          <div style="display: flex; gap: 8px; flex-wrap: wrap;">
            <input type="password" id="newMasterPasswordInput" placeholder="Enter new passcode (min 6 characters)..." class="modal-path-input" style="flex: 1; min-width: 220px; font-family: var(--font-mono); font-size: 13px; padding: 9px 12px;" required>
            <button type="submit" class="btn btn-primary btn-sm" id="btnChangePasswordSubmit">Save Passcode</button>
          </div>
          <label style="display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-secondary); cursor: pointer;">
            <input type="checkbox" id="chkRevokeOnPassChange" checked style="accent-color: var(--accent);">
            <span>Automatically revoke all existing remote sessions on password change</span>
          </label>
        </form>
      </div>

    </div>
  </div>
</div>

<!-- In-Browser Host Folder Navigator Modal (Linear / Apple Files Aesthetic) -->
<div class="modal-overlay" id="hostBrowserModal" role="dialog" aria-modal="true" aria-labelledby="hostBrowserModalTitle">
  <div class="modal-content">
    <div class="bottom-sheet-handle-bar">
      <div class="bottom-sheet-handle"></div>
    </div>
    
    <div class="modal-header">
      <div class="modal-title">
        <svg class="icon" style="color: var(--brand-blue);" viewBox="0 0 24 24"><path d="m6 14 1.45-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.55 6a2 2 0 0 1-1.94 1.5H4a2 2 0 0 1-2-2V5c0-1.1.9-2 2-2h3.93a2 2 0 0 1 1.66.9l.82 1.2a2 2 0 0 0 1.66.9H18a2 2 0 0 1 2 2v2"/></svg>
        <span id="hostBrowserModalTitle">Select PC Folder</span>
      </div>
      <button class="icon-btn-micro" onclick="closeModal('hostBrowserModal')" aria-label="Close dialog">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>

    <div class="modal-body" style="gap: 10px; padding: 14px 16px; overflow: hidden; display: flex; flex-direction: column; flex: 1;">
      <!-- Drives Cards Ribbon with Scroll Chevrons -->
      <div class="drive-ribbon-wrapper">
        <button class="icon-btn drive-ribbon-arrow left" id="driveRibbonLeft" onclick="scrollDriveRibbon(-1)" title="Scroll left" aria-label="Scroll left">
          <svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
        <div class="drive-cards-ribbon" id="modalDrivesBar" aria-label="Host Storage Drives">
          <!-- Rendered via JS -->
        </div>
        <button class="icon-btn drive-ribbon-arrow right" id="driveRibbonRight" onclick="scrollDriveRibbon(1)" title="Scroll right" aria-label="Scroll right">
          <svg viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
        </button>
      </div>

      <!-- Segmented Breadcrumbs & Action Toolbar -->
      <div class="modal-nav-bar" id="modalNavBar">
        <button class="icon-btn modal-btn-up" id="modalBtnUpLevel" onclick="modalNavigateUp()" title="Up One Level (Parent Folder)" style="width: 32px; height: 32px;">
          <svg class="icon" viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
        </button>
        
        <div class="breadcrumb-trail-container" id="modalBreadcrumbTrail" aria-label="Breadcrumb Path Navigation">
          <!-- Rendered via JS -->
        </div>

        <button class="icon-btn breadcrumb-edit-btn" id="modalBtnToggleManual" onclick="toggleManualPathInput()" title="Toggle manual path typing" style="width: 32px; height: 32px;">
          <svg class="icon" viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
        </button>
      </div>

      <!-- Optional Manual Path Input Field (Hidden by default) -->
      <div id="modalManualPathRow" style="display: none; gap: 8px; align-items: center;">
        <input type="text" class="btn" style="flex: 1; text-align: left; font-family: var(--font-mono); font-size: 11px; cursor: text; min-height: 38px;" id="modalCurrentPathInput" placeholder="Enter absolute directory path...">
        <button class="btn btn-sm" style="min-height: 38px; padding: 0 16px;" onclick="browseModalPath(document.getElementById('modalCurrentPathInput').value)">Go</button>
      </div>

      <!-- Quick-Filter Search Bar + Actions -->
      <div class="modal-filter-toolbar">
        <div class="modal-filter-box">
          <svg class="icon filter-icon" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
          <input type="text" id="modalFolderFilterInput" class="modal-filter-input" placeholder="Filter folders in this directory..." autocomplete="off" spellcheck="false">
          <button id="modalFilterClearBtn" class="modal-filter-clear-btn" style="display: none;" onclick="clearModalFilter()" title="Clear filter" type="button">
            <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
          </button>
        </div>
        <span id="modalFilterMatchCount" class="modal-filter-badge"></span>
        <button class="btn btn-sm" id="modalBtnNewFolder" onclick="toggleInlineFolderCreator(true)" title="Create new subfolder in current directory">
          <svg class="icon" viewBox="0 0 24 24"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>
          <span class="btn-new-folder-label">New Folder</span>
        </button>
      </div>

      <!-- Inline Sleek New Folder Creator -->
      <div class="inline-folder-creator" id="modalInlineFolderCreator" style="display: none;">
        <svg class="icon" style="color: #fbbf24;" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
        <input type="text" id="inlineNewFolderName" class="inline-folder-input" placeholder="New folder name..." autocomplete="off">
        <button class="btn btn-primary btn-sm" onclick="submitInlineNewFolder()" style="min-height: 32px; padding: 0 12px;">Create</button>
        <button class="btn btn-ghost btn-sm" onclick="toggleInlineFolderCreator(false)" style="min-height: 32px; padding: 0 10px;" title="Dismiss new folder creation">Dismiss</button>
      </div>

      <!-- Subfolder Tree List / Viewport -->
      <div class="modal-tree-container" id="modalFolderTreeList" tabindex="0" role="listbox" aria-label="Subdirectories">
        <!-- Injected via JS -->
      </div>
    </div>

    <div class="modal-footer">
      <button class="btn btn-ghost btn-sm" id="modalNativePickerBtn" onclick="triggerNativePicker(browserModalTarget)" title="Open standard Windows Explorer folder picker on Host">
        <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/><rect x="13" y="13" width="8" height="8" rx="1"/></svg>
        <span>Windows Dialog</span>
      </button>
      <button class="btn btn-ghost btn-sm" onclick="closeModal('hostBrowserModal')">Cancel</button>
      <button class="btn btn-primary btn-sm" id="modalConfirmBtn" onclick="confirmModalFolderSelection()">Select This Folder</button>
    </div>
  </div>
</div>

<!-- Connect Device & Network Access Modal -->
<div class="modal-overlay" id="qrModal">
  <div class="modal-content" style="max-width: 480px;">
    <div class="modal-header">
      <div class="modal-title" style="display: flex; align-items: center; gap: 8px;">
        <svg class="icon" style="color: var(--brand-blue);" viewBox="0 0 24 24"><rect width="6" height="6" x="3" y="3" rx="1.5"/><rect width="6" height="6" x="15" y="3" rx="1.5"/><rect width="6" height="6" x="3" y="15" rx="1.5"/><path d="M15 15h2v2h-2z"/><path d="M19 15h2v6h-6v-2h4v-4z"/><path d="M7 7h.01"/><path d="M17 7h.01"/><path d="M7 17h.01"/></svg>
        <span id="qrModalTitle">Connect Other Devices</span>
      </div>
      <button class="icon-btn-micro" onclick="closeModal('qrModal')" aria-label="Close">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>

    <div class="modal-body" style="gap: 14px; padding: 16px 20px;">
      <!-- Section 1: Network IP for Other Devices (Phones / Laptops) -->
      <div class="qr-network-section" id="qrNetworkSection">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
          <div style="display: flex; align-items: center; gap: 6px;">
            <span class="status-dot" style="background: #22c55e; box-shadow: 0 0 8px #22c55e;"></span>
            <span style="font-size: 12px; font-weight: 600; color: var(--text-primary);">Network IP (For Other Devices)</span>
          </div>
          <span class="drive-card-badge" id="qrActiveBadge" style="background: rgba(56, 189, 248, 0.15); color: #38bdf8; border-color: rgba(56, 189, 248, 0.3);">Wi-Fi</span>
        </div>

        <!-- Network Interface Switcher Chips (if multiple interfaces exist) -->
        <div class="qr-interface-chips" id="qrInterfaceChips" style="display: flex; gap: 6px; overflow-x: auto; margin-bottom: 12px; padding-bottom: 4px;">
          <!-- Dynamically populated via JS -->
        </div>

        <!-- The Working QR Code for the Network IP -->
        <div style="display: flex; flex-direction: column; align-items: center; gap: 12px; background: var(--surface-2); border: 1px solid var(--border-standard); border-radius: var(--radius-md); padding: 16px;">
          <div style="background: #ffffff; padding: 10px; border-radius: var(--radius-md); display: inline-block; box-shadow: 0 4px 16px rgba(0,0,0,0.4);">
            <img id="qrModalImg" src="" alt="Network QR Code" style="width: 180px; height: 180px; display: block;">
          </div>
          <div style="text-align: center; width: 100%;">
            <div class="mono" id="qrModalUrl" style="font-size: 13px; font-weight: 600; color: var(--text-primary); word-break: break-all; margin-bottom: 4px;"></div>
            <div style="font-size: 11px; color: var(--text-tertiary);">Scan with your phone or tablet camera to open HostDrop on your local network.</div>
          </div>
          <button class="btn btn-primary btn-sm" style="width: 100%; justify-content: center;" onclick="copyAddress(document.getElementById('qrModalUrl').textContent, this)">
            <svg class="icon" viewBox="0 0 24 24"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
            <span>Copy Network Link for Phone</span>
          </button>
        </div>
      </div>

      <!-- Section 2: Host PC Address Only (Localhost) - NO QR CODE -->
      <div class="host-ip-card" id="qrHostSection" style="background: rgba(255, 255, 255, 0.03); border: 1px dashed rgba(255, 255, 255, 0.15); border-radius: var(--radius-md); padding: 12px 14px;">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px;">
          <div style="display: flex; align-items: center; gap: 6px;">
            <svg class="icon" style="color: var(--text-secondary); width: 15px; height: 15px;" viewBox="0 0 24 24"><rect width="20" height="14" x="2" y="3" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>
            <span style="font-size: 12px; font-weight: 600; color: var(--text-secondary);">Host PC IP (Localhost Only)</span>
          </div>
          <span style="font-size: 10px; font-weight: 500; padding: 2px 8px; border-radius: var(--radius-pill); background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3);">Host Only &bull; No QR</span>
        </div>
        <div style="display: flex; align-items: center; justify-content: space-between; gap: 8px;">
          <code class="mono" id="qrHostUrl" style="font-size: 12px; color: var(--text-primary);">http://127.0.0.1:__PORT__</code>
          <button class="btn btn-ghost btn-sm" style="font-size: 11px; padding: 4px 10px;" onclick="copyAddress(document.getElementById('qrHostUrl').textContent, this)">Copy</button>
        </div>
        <div style="font-size: 11px; color: var(--text-tertiary); margin-top: 6px; line-height: 1.4; display: flex; align-items: flex-start; gap: 6px;">
          <svg class="icon" style="width: 14px; height: 14px; color: var(--text-tertiary); flex-shrink: 0; margin-top: 1px;" viewBox="0 0 24 24"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
          <span><strong>Host Device Only:</strong> This address works exclusively in a browser on this host computer. It <em>cannot</em> be accessed by phones or other devices on your network. (No QR code generated).</span>
        </div>
      </div>
    </div>

    <div class="modal-footer" style="justify-content: flex-end; padding: 12px 20px;">
      <button class="btn btn-ghost btn-sm" onclick="closeModal('qrModal')">Close</button>
    </div>
  </div>
</div>

<!-- Comprehensive Beginner FAQ & IP Guide Modal (R4) -->
<div class="modal-overlay" id="guideModal">
  <div class="modal-content" style="max-width: 640px;">
    <div class="modal-header">
      <div class="modal-title">
        <svg class="icon" style="color: var(--brand-blue);" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        HostDrop Connection Guide &amp; Beginner FAQ
      </div>
      <button class="icon-btn-micro" onclick="closeModal('guideModal')">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    
    <div class="modal-body">
      <!-- Live Search Box -->
      <div class="faq-search-wrapper">
        <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        <input type="text" class="search-input" id="faqSearchInput" placeholder="Search answers (e.g. speed, hotspot, IP, resume, where files go)..." oninput="filterFaq(this.value)">
      </div>

      <!-- Category Filter Pills -->
      <div class="faq-category-pills">
        <div class="faq-pill active" onclick="setFaqCategory('all', this)">All Questions</div>
        <div class="faq-pill" onclick="setFaqCategory('network', this)">Network &amp; IPs</div>
        <div class="faq-pill" onclick="setFaqCategory('speed', this)">Speed &amp; Wi-Fi</div>
        <div class="faq-pill" onclick="setFaqCategory('storage', this)">Storage &amp; Folders</div>
        <div class="faq-pill" onclick="setFaqCategory('security', this)">Security &amp; Privacy</div>
        <div class="faq-pill" onclick="setFaqCategory('troubleshooting', this)">Troubleshooting</div>
      </div>

      <!-- 9-Part Accordion FAQ Container -->
      <div class="faq-container">

        <!-- Question 1 -->
        <div class="faq-item" data-category="network" data-keywords="ip address same everyone how work unique host phone connect qr courier apartment">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
              <span>1. Is the IP address the same for everyone? How do IP addresses work?</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p><strong>No, every device on your network has its own unique IP address.</strong> Think of your local Wi-Fi or Mobile Hotspot like an apartment building where every device gets its own apartment number:</p>
            <ul>
              <li>Your PC might be <code>192.168.1.45</code></li>
              <li>Your phone might be <code>192.168.1.12</code></li>
              <li>Your tablet might be <code>192.168.1.18</code></li>
            </ul>
            <p><strong>The IP address shown in HostDrop belongs to your PC (the host computer).</strong> When you want to connect your phone, tablet, or another laptop, you type your <strong>PC's IP address</strong> into the phone's web browser (or simply scan the QR code). You do <em>not</em> type your phone's IP address&mdash;you are telling your phone to visit your PC so they can exchange files!</p>
          </div>
        </div>

        <!-- Question 2 -->
        <div class="faq-item" data-category="network" data-keywords="hotspot wifi cable direct ethernet connection modes 192.168.137.1 169.254 gigabit p2p">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><path d="M4.93 4.93a10 10 0 0 0 0 14.14"/><path d="M7.76 7.76a6 6 0 0 0 0 8.48"/><circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.48"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>
              <span>2. When should I use Mobile Hotspot vs Wi-Fi vs Direct Cable?</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p>HostDrop automatically detects all active connections. Here is when to use each one:</p>
            <div class="faq-mode-grid">
              <div class="faq-mode-card">
                <div class="mode-title" style="color: #fbbf24;">Mobile Hotspot (192.168.137.1)</div>
                <div class="mode-desc"><strong>Best for Anywhere / On-the-Go:</strong> Turn on Windows Mobile Hotspot on your laptop (or your phone's personal hotspot) and connect your other device to it. <em>No router and no internet required!</em> Works in cars, classrooms, outdoors, or hotels.</div>
              </div>
              <div class="faq-mode-card">
                <div class="mode-title" style="color: #60a5fa;">Home/Office Wi-Fi (192.168.1.x)</div>
                <div class="mode-desc"><strong>Best for Everyday Convenience:</strong> Both devices connect to your normal home or office Wi-Fi router. Perfect when your devices are already on the same wireless network.</div>
              </div>
              <div class="faq-mode-card">
                <div class="mode-title" style="color: #34d399;">Direct Cable P2P (169.254.x.x)</div>
                <div class="mode-desc"><strong>Best for Massive Files (90&ndash;115 MB/s):</strong> Plug an Ethernet cable directly between two PCs (no router needed). Both PCs will auto-configure 169.254 addresses, unlocking ultra-fast Gigabit transfers (100 GB in ~15 minutes).</div>
              </div>
            </div>
          </div>
        </div>

        <!-- Question 3 -->
        <div class="faq-item" data-category="speed" data-keywords="speed slow 2.4ghz 5ghz fast boost mbps transfer rate gigabit band frequency">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
              <span>3. Why is transfer speed slow (e.g. 2 MB/s on 2.4 GHz vs 50&ndash;100 MB/s on 5 GHz / Cable)?</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p>Transfer speeds are determined by your wireless frequency band or physical cable:</p>
            <ul>
              <li><strong>2.4 GHz Wi-Fi (2 &ndash; 5 MB/s):</strong> The 2.4 GHz band is crowded with Bluetooth, microwaves, and other Wi-Fi networks. It is physically limited in bandwidth.</li>
              <li><strong>5 GHz Wi-Fi / Hotspot (35 &ndash; 70 MB/s):</strong> 5 GHz has wide, uncongested channels. <em>Pro-Tip:</em> In Windows Settings &rarr; Network &rarr; Mobile Hotspot, change the <strong>Network Band</strong> to <strong>5 GHz</strong>. Your transfer speed will instantly jump 5x to 10x faster!</li>
              <li><strong>Direct Ethernet Cable (90 &ndash; 115 MB/s):</strong> A wired connection reaches the maximum physical speed of Gigabit network ports, moving ~1 GB every 9&ndash;10 seconds.</li>
            </ul>
          </div>
        </div>

        <!-- Question 4 -->
        <div class="faq-item" data-category="storage" data-keywords="where files go save folder location pc downloads inbox explorer target path">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
              <span>4. Where do files I send go on my computer?</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p>All files and folders transferred from phones or other computers are saved directly into your <strong>Inbox Folder on PC</strong> (default: <code>D:\HostDrop</code> or <code>C:\HostDrop</code>).</p>
            <p>You can see the current folder path in the <strong>"Inbox Folder on PC"</strong> card in the left sidebar. To reveal your files instantly, click the <strong>"Open in Windows Explorer"</strong> button on your PC.</p>
          </div>
        </div>

        <!-- Question 5 -->
        <div class="faq-item" data-category="storage" data-keywords="folder upload send entire subfolders directory recursive hierarchy tree structure">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
              <span>5. Can I send entire folders and nested subfolders?</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p><strong>Yes, absolutely!</strong> HostDrop preserves full folder structures recursively:</p>
            <ul>
              <li><strong>From PC:</strong> Click <strong>"Send Folder"</strong> or simply drag-and-drop any directory directly onto the web page.</li>
              <li><strong>From Mobile / Chrome:</strong> Choose "Send Folder" to select albums or multi-file folders.</li>
            </ul>
            <p>All nested subdirectories, subfolders, and files will be recreated identically on the PC.</p>
          </div>
        </div>

        <!-- Question 6 -->
        <div class="faq-item" data-category="storage" data-keywords="interrupted resume chunk smart resume connection drop wifi flicker paused">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
              <span>6. What happens if a transfer gets interrupted?</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p><strong>Zero corruption, zero wasted time.</strong> HostDrop features smart byte-level chunk resumption.</p>
            <p>If your Wi-Fi flickers, your phone goes to sleep, or the browser closes: simply re-select or drop the same file again. HostDrop will check the PC's storage, recognize the exact bytes already received, and <strong>resume from the exact byte where it stopped</strong> without re-uploading from the start.</p>
          </div>
        </div>

        <!-- Question 7 -->
        <div class="faq-item" data-category="security" data-keywords="install app friends guest no app browser qr code safari chrome android iphone">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><line x1="12" y1="18" x2="12.01" y2="18"/></svg>
              <span>7. Do my friends or family need to install any app?</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p><strong>No apps, no plugins, and no user accounts required.</strong></p>
            <p>Any guest device with a standard web browser (Chrome, Safari, Edge, Firefox, Samsung Internet) can connect instantly. Just scan the <strong>QR Connect</strong> code with their phone camera or share the web link. It works seamlessly across iPhone, Android, iPad, Mac, Windows, and Linux.</p>
          </div>
        </div>

        <!-- Question 8 -->
        <div class="faq-item" data-category="security" data-keywords="privacy cloud internet security offline local safe tracking lan private">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
              <span>8. Are files uploaded to the internet or cloud? Is it private?</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p><strong>100% Private Local Area Network (LAN) Transfer &mdash; Zero Cloud.</strong></p>
            <p>Your files move strictly across your local Wi-Fi, Hotspot, or Ethernet cable directly from one device to the other. Your data never touches the internet, third-party cloud servers, or external tracking. You can even unplug your internet cable or turn off mobile data, and HostDrop will continue transferring at maximum local speed!</p>
          </div>
        </div>

        <!-- Question 9 -->
        <div class="faq-item" data-category="troubleshooting" data-keywords="troubleshoot connection problem firewall cannot connect ap isolation not loading port">
          <div class="faq-question" onclick="toggleFaq(this)">
            <div class="faq-q-title">
              <svg class="icon faq-icon" viewBox="0 0 24 24"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
              <span>9. Troubleshooting connection problems (Quick checklist)</span>
            </div>
            <svg class="icon chevron" viewBox="0 0 24 24"><polyline points="6 9 12 15 18 9"/></svg>
          </div>
          <div class="faq-answer">
            <p>If your phone or guest device cannot open the page, check these 4 common items:</p>
            <ol>
              <li><strong>Same Network:</strong> Ensure both your PC and phone are on the exact same Wi-Fi network (or connected to your PC's Mobile Hotspot).</li>
              <li><strong>Windows Firewall:</strong> When HostDrop started, Windows may have asked for network permission. Make sure <em>"Private Networks"</em> is allowed. In Windows Defender Firewall &rarr; <em>Allow an app through firewall</em> &rarr; ensure Python / HostDrop is checked for Private networks.</li>
              <li><strong>Public Wi-Fi "AP Isolation":</strong> Some hotel, coffee shop, or school Wi-Fi networks block devices from talking to each other. <em>Fix:</em> Turn on Windows Mobile Hotspot on your PC and connect your phone directly to the PC's hotspot instead!</li>
              <li><strong>Exact Port in URL:</strong> Verify you typed the port number <code>:8080</code> at the end of the address (e.g. <code>http://192.168.1.45:8080</code>).</li>
            </ol>
          </div>
        </div>

      </div>
    </div>
    
    <div class="modal-footer">
      <button class="btn btn-primary btn-sm" onclick="closeModal('guideModal')">Got it</button>
    </div>
  </div>
</div>

<!-- Toast Container -->
<div class="toast-container" id="toastContainer"></div>

<!-- ═══════════════════════════════════════════════════════════════════════════════
     JAVASCRIPT CLIENT APPLICATION ENGINE
     ═══════════════════════════════════════════════════════════════════════════════ -->
<script>
/* Global App State */
let activeTab = 'recv';
let curRecvPath = '';
let curSharePath = '';
let currentItems = [];
let isTransferring = false;
let browserModalTarget = 'recv';
let activeModalBrowsePath = '';
let activeFaqCategory = 'all';

/* Network Interfaces injected from backend */
window.hostNetInterfaces = __NET_INTERFACES_JSON__;
window.hostOS = "__HOST_OS__";
let modalBrowseError = null;

/* Host vs Guest Role Adaptive State */
const isHostClient = ['localhost', '127.0.0.1', '::1', ''].includes(window.location.hostname);
function isLocalhost() { return isHostClient; }

function applyClientRole() {
  const hostPill = document.getElementById('hostStatusPill');
  const guestPill = document.getElementById('guestStatusPill');
  const guestBanner = document.getElementById('guestBanner');

  if (isHostClient) {
    if (hostPill) hostPill.style.display = 'inline-flex';
    if (guestPill) guestPill.style.display = 'none';
    if (guestBanner) guestBanner.style.display = 'none';
  } else {
    if (hostPill) hostPill.style.display = 'none';
    if (guestPill) guestPill.style.display = 'inline-flex';
    if (guestBanner) guestBanner.style.display = 'block';
  }

  applyPlatformAdaptations();
}

function applyPlatformAdaptations() {
  const hostOS = window.hostOS || 'windows';
  let openLabel = 'Open in File Explorer';
  let pickerLabel = 'Windows Dialog';
  let openTitle = 'Open folder in Windows Explorer';
  let pickerTitle = 'Open native folder picker on Host';

  if (hostOS === 'macos') {
    openLabel = 'Open in Finder';
    openTitle = 'Open folder in macOS Finder';
    pickerLabel = 'System Dialog';
  } else if (hostOS === 'linux' || hostOS === 'termux') {
    openLabel = 'Open in File Manager';
    openTitle = 'Open folder in system file manager';
    pickerLabel = 'System Dialog';
  }

  // Update button texts and titles
  ['recvOpenExplorerBtn', 'shareOpenExplorerBtn'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      if (!isHostClient) {
        el.innerHTML = '<svg class="icon" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg> View in Browser';
        el.title = 'Switch active tab to view this folder in browser';
      } else {
        el.innerHTML = `<svg class="icon" viewBox="0 0 24 24"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg> ${openLabel}`;
        el.title = openTitle;
      }
    }
  });

  ['recvNativePickerBtn', 'shareNativePickerBtn', 'modalNativePickerBtn'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      if (!isHostClient) {
        el.style.display = 'none';
      } else {
        el.style.display = '';
        el.innerHTML = `<svg class="icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg> <span>${pickerLabel}</span>`;
        el.title = pickerTitle;
      }
    }
  });
}

/* Ribbon Horizontal Wheel Scroll & Chevrons */
const netScrollContainer = document.getElementById('netScrollContainer');
if (netScrollContainer) {
  netScrollContainer.addEventListener('wheel', e => {
    if (!netScrollContainer.classList.contains('grid-mode')) {
      if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
        e.preventDefault();
        netScrollContainer.scrollLeft += e.deltaY * 0.8;
      }
    }
  }, { passive: false });
}

function scrollRibbon(delta) {
  if (netScrollContainer) {
    netScrollContainer.scrollBy({ left: delta, behavior: 'smooth' });
  }
}

function toggleNetworkGridMode() {
  if (netScrollContainer) {
    netScrollContainer.classList.toggle('grid-mode');
  }
}

/* Tab Switching (Inbox vs Library) */
function switchTab(tab) {
  activeTab = tab;
  document.getElementById('tabRecvBtn').classList.toggle('active', tab === 'recv');
  document.getElementById('tabShareBtn').classList.toggle('active', tab === 'share');
  
  const helperEl = document.getElementById('tabHelperBar');
  if (helperEl) {
    helperEl.textContent = tab === 'recv' 
      ? 'Files transferred to this computer from connected devices'
      : 'Files shared by this computer available for you to download';
  }

  document.getElementById('dropTargetName').textContent = tab === 'recv' ? 'Inbox Folder on PC' : 'Share Folder from PC';
  const curPath = tab === 'recv' ? curRecvPath : curSharePath;
  loadDirectory(tab, curPath, true);
}

/* Directory Loading & Rendering */
async function loadDirectory(tab, relPath, showLoading = false) {
  const tableBody = document.getElementById('fileTableBody');
  const emptyState = document.getElementById('emptyState');
  
  try {
    const res = await fetch('/api/list?tab=' + tab + '&path=' + encodeURIComponent(relPath));
    if (res.status === 401) {
      openAuthModal();
      return;
    }
    const data = await res.json();
    if (data.login_required || data.error === 'unauthorized') {
      openAuthModal();
      return;
    }
    currentItems = data.items || [];

    // Update Tab Badges
    if (tab === 'recv') {
      document.getElementById('recvCountBadge').textContent = currentItems.length;
      curRecvPath = relPath;
    } else {
      document.getElementById('shareCountBadge').textContent = currentItems.length;
      curSharePath = relPath;
    }

    renderBreadcrumbs(relPath);
    renderTableItems(currentItems);
  } catch (e) {
    console.error('Failed to load directory:', e);
  }
}

function renderBreadcrumbs(relPath) {
  const bc = document.getElementById('explorerBreadcrumbs');
  bc.innerHTML = '';
  
  const rootCrumb = document.createElement('span');
  rootCrumb.className = 'crumb-item';
  rootCrumb.textContent = activeTab === 'recv' ? 'Inbox Root' : 'Library Root';
  rootCrumb.onclick = () => navigateBreadcrumb('');
  bc.appendChild(rootCrumb);

  if (!relPath) return;

  const parts = relPath.replace(/\\\\/g, '/').split('/').filter(Boolean);
  let acc = '';
  for (let i = 0; i < parts.length; i++) {
    const sep = document.createElement('span');
    sep.className = 'crumb-sep';
    sep.textContent = '>';
    bc.appendChild(sep);

    acc += (acc ? '/' : '') + parts[i];
    const itemPath = acc;
    const item = document.createElement('span');
    item.className = 'crumb-item';
    item.textContent = parts[i];
    item.onclick = () => navigateBreadcrumb(itemPath);
    bc.appendChild(item);
  }
}

function navigateBreadcrumb(path) {
  if (activeTab === 'recv') curRecvPath = path;
  else curSharePath = path;
  loadDirectory(activeTab, path, true);
}

function renderTableItems(items) {
  const tableBody = document.getElementById('fileTableBody');
  const emptyState = document.getElementById('emptyState');
  tableBody.innerHTML = '';

  if (!items || items.length === 0) {
    emptyState.style.display = 'flex';
    return;
  }
  emptyState.style.display = 'none';

  items.forEach(item => {
    const tr = document.createElement('tr');
    tr.className = 'file-row';

    // File Icon classification
    let iconBoxClass = 'file-icon-box';
    let iconSvg = '<svg viewBox="0 0 24 24"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/></svg>';
    let actionBtnHtml = '';

    if (item.isDir) {
      iconBoxClass += ' folder';
      iconSvg = '<svg viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>';
      tr.onclick = () => {
        const curPath = activeTab === 'recv' ? curRecvPath : curSharePath;
        const newPath = curPath ? curPath + '/' + item.name : item.name;
        navigateBreadcrumb(newPath);
      };
      actionBtnHtml = `
        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); downloadFolderZip('${encodeURIComponent(item.name)}')" title="Download folder as ZIP archive">
          <svg class="icon" viewBox="0 0 24 24"><rect width="8" height="4" x="8" y="3" rx="1"/><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          ZIP
        </button>
        <button class="btn btn-ghost btn-sm" onclick="event.stopPropagation(); saveAsFolderStructure('${encodeURIComponent(item.name)}')" title="Save folder structure directly to local disk">
          <svg class="icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/><polyline points="12 11 12 17 15 14"/><line x1="9" y1="14" x2="12" y2="17"/></svg>
          Save Folder
        </button>
      `;
    } else {
      const ext = (item.name.split('.').pop() || '').toLowerCase();
      if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg'].includes(ext)) {
        iconBoxClass += ' image';
        iconSvg = '<svg viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>';
      } else if (['mp4', 'mkv', 'avi', 'mov', 'webm'].includes(ext)) {
        iconBoxClass += ' video';
        iconSvg = '<svg viewBox="0 0 24 24"><polygon points="23 7 16 12 23 17 23 7"/><rect width="14" height="14" x="1" y="5" rx="2"/></svg>';
      } else if (['mp3', 'wav', 'flac', 'aac', 'ogg'].includes(ext)) {
        iconBoxClass += ' audio';
        iconSvg = '<svg viewBox="0 0 24 24"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>';
      } else if (['zip', 'rar', '7z', 'tar', 'gz'].includes(ext)) {
        iconBoxClass += ' zip';
        iconSvg = '<svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/><rect width="8" height="4" x="8" y="3" rx="1"/></svg>';
      } else if (['js', 'py', 'ts', 'html', 'css', 'json', 'cpp', 'rs', 'go'].includes(ext)) {
        iconBoxClass += ' code';
        iconSvg = '<svg viewBox="0 0 24 24"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>';
      }

      tr.onclick = () => downloadFile(item.name);
      actionBtnHtml = `
        <button class="btn btn-primary btn-sm" onclick="event.stopPropagation(); downloadFile('${encodeURIComponent(item.name)}')">
          <svg class="icon" viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Download
        </button>
      `;
    }

    const sizeDisplay = item.isDir ? (item.count + ' items') : formatBytes(item.size || 0);

    tr.innerHTML = `
      <td>
        <div class="file-name-cell">
          <div class="${iconBoxClass}">${iconSvg}</div>
          <span title="${escapeHtml(item.name)}">${escapeHtml(item.name)}</span>
        </div>
      </td>
      <td class="tabular-nums">${sizeDisplay}</td>
      <td class="file-actions-cell">${actionBtnHtml}</td>
    `;
    tableBody.appendChild(tr);
  });
}

function handleSearchFilter(query) {
  const q = (query || '').toLowerCase().trim();
  if (!q) {
    renderTableItems(currentItems);
    return;
  }
  const filtered = currentItems.filter(i => i.name.toLowerCase().includes(q));
  renderTableItems(filtered);
}

function refreshActiveDirectory() {
  const curPath = activeTab === 'recv' ? curRecvPath : curSharePath;
  loadDirectory(activeTab, curPath, true);
  showToast('Refreshed file list', 'info');
}

function refreshAll() {
  refreshActiveDirectory();
  fetchStorageMetrics();
}

/* ═══════════════════════════════════════════════════════════════════════════════
   DUAL FOLDER DOWNLOAD ENGINE (ZIP & Web File System Access API) (R3)
   ═══════════════════════════════════════════════════════════════════════════════ */
function downloadFile(name) {
  const curPath = activeTab === 'recv' ? curRecvPath : curSharePath;
  const fullRel = curPath ? curPath + '/' + decodeURIComponent(name) : decodeURIComponent(name);
  window.location.href = '/download?tab=' + activeTab + '&path=' + encodeURIComponent(fullRel);
}

function downloadFolderZip(name) {
  const curPath = activeTab === 'recv' ? curRecvPath : curSharePath;
  const fullRel = curPath ? curPath + '/' + decodeURIComponent(name) : decodeURIComponent(name);
  window.location.href = '/api/zip?tab=' + activeTab + '&path=' + encodeURIComponent(fullRel);
}

function downloadCurrentFolderZip() {
  const curPath = activeTab === 'recv' ? curRecvPath : curSharePath;
  window.location.href = '/api/zip?tab=' + activeTab + '&path=' + encodeURIComponent(curPath);
}

function isFileSystemAccessSupported() {
  return typeof window.showDirectoryPicker === 'function';
}

async function saveAsFolderStructure(folderName) {
  if (!isFileSystemAccessSupported()) {
    showToast('Direct folder writing not supported on this browser. Downloading as ZIP instead...', 'info');
    downloadFolderZip(folderName);
    return;
  }

  const curPath = activeTab === 'recv' ? curRecvPath : curSharePath;
  const fullRelPath = curPath ? (curPath + '/' + decodeURIComponent(folderName)) : decodeURIComponent(folderName);
  const targetFolderName = decodeURIComponent(folderName);

  let rootDirHandle;
  try {
    rootDirHandle = await window.showDirectoryPicker({
      mode: 'readwrite',
      startIn: 'downloads'
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      showToast('Folder download cancelled', 'info');
      return;
    }
    showToast('Could not access folder: ' + err.message + '. Downloading as ZIP...', 'info');
    downloadFolderZip(folderName);
    return;
  }

  showToast(`Saving "${targetFolderName}" folder structure...`, 'info');

  try {
    const targetDirHandle = await rootDirHandle.getDirectoryHandle(targetFolderName, { create: true });
    let savedCount = 0;

    async function recurseDirectory(relPath, currentDirHandle) {
      const res = await fetch('/api/list?tab=' + encodeURIComponent(activeTab) + '&path=' + encodeURIComponent(relPath));
      if (!res.ok) throw new Error(`HTTP ${res.status} fetching directory list`);
      const data = await res.json();
      const items = data.items || [];

      for (const item of items) {
        const itemRel = relPath ? `${relPath}/${item.name}` : item.name;
        if (item.isDir) {
          const subDirHandle = await currentDirHandle.getDirectoryHandle(item.name, { create: true });
          await recurseDirectory(itemRel, subDirHandle);
        } else {
          const fileHandle = await currentDirHandle.getFileHandle(item.name, { create: true });
          const writableStream = await fileHandle.createWritable();
          const fileRes = await fetch('/download?tab=' + encodeURIComponent(activeTab) + '&path=' + encodeURIComponent(itemRel));
          if (!fileRes.ok) throw new Error(`Failed to download ${item.name}`);

          if (fileRes.body && typeof fileRes.body.getReader === 'function') {
            const reader = fileRes.body.getReader();
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              await writableStream.write(value);
            }
          } else {
            const blob = await fileRes.blob();
            await writableStream.write(blob);
          }
          await writableStream.close();
          savedCount++;
        }
      }
    }

    await recurseDirectory(fullRelPath, targetDirHandle);
    showToast(`Successfully saved "${targetFolderName}" (${savedCount} files) with folder hierarchy intact!`, 'success');
  } catch (err) {
    console.error('Recursive folder download error:', err);
    showToast(`Folder download error: ${err.message}. Falling back to ZIP...`, 'error');
    downloadFolderZip(folderName);
  }
}

async function saveCurrentFolderStructure() {
  const curPath = activeTab === 'recv' ? curRecvPath : curSharePath;
  const folderName = curPath ? curPath.split('/').pop() : (activeTab === 'recv' ? 'HostDrop_Inbox' : 'HostDrop_Library');
  
  if (!isFileSystemAccessSupported()) {
    showToast('Direct folder writing not supported on this browser. Downloading as ZIP instead...', 'info');
    downloadCurrentFolderZip();
    return;
  }

  let rootDirHandle;
  try {
    rootDirHandle = await window.showDirectoryPicker({
      mode: 'readwrite',
      startIn: 'downloads'
    });
  } catch (err) {
    if (err.name === 'AbortError') {
      showToast('Folder download cancelled', 'info');
      return;
    }
    showToast('Could not access folder: ' + err.message + '. Downloading as ZIP...', 'info');
    downloadCurrentFolderZip();
    return;
  }

  showToast(`Saving "${folderName}" folder structure...`, 'info');

  try {
    const targetDirHandle = await rootDirHandle.getDirectoryHandle(folderName, { create: true });
    let savedCount = 0;

    async function recurseDirectory(relPath, currentDirHandle) {
      const res = await fetch('/api/list?tab=' + encodeURIComponent(activeTab) + '&path=' + encodeURIComponent(relPath));
      if (!res.ok) throw new Error(`HTTP ${res.status} fetching directory list`);
      const data = await res.json();
      const items = data.items || [];

      for (const item of items) {
        const itemRel = relPath ? `${relPath}/${item.name}` : item.name;
        if (item.isDir) {
          const subDirHandle = await currentDirHandle.getDirectoryHandle(item.name, { create: true });
          await recurseDirectory(itemRel, subDirHandle);
        } else {
          const fileHandle = await currentDirHandle.getFileHandle(item.name, { create: true });
          const writableStream = await fileHandle.createWritable();
          const fileRes = await fetch('/download?tab=' + encodeURIComponent(activeTab) + '&path=' + encodeURIComponent(itemRel));
          if (!fileRes.ok) throw new Error(`Failed to download ${item.name}`);

          if (fileRes.body && typeof fileRes.body.getReader === 'function') {
            const reader = fileRes.body.getReader();
            while (true) {
              const { done, value } = await reader.read();
              if (done) break;
              await writableStream.write(value);
            }
          } else {
            const blob = await fileRes.blob();
            await writableStream.write(blob);
          }
          await writableStream.close();
          savedCount++;
        }
      }
    }

    await recurseDirectory(curPath, targetDirHandle);
    showToast(`Successfully saved "${folderName}" (${savedCount} files) with folder hierarchy intact!`, 'success');
  } catch (err) {
    console.error('Recursive folder download error:', err);
    showToast(`Folder download error: ${err.message}. Falling back to ZIP...`, 'error');
    downloadCurrentFolderZip();
  }
}

/* ═══════════════════════════════════════════════════════════════════════════════
   SMART RESUMABLE CHUNKED UPLOAD ENGINE
   ═══════════════════════════════════════════════════════════════════════════════ */
async function handleFileSelect(files) {
  if (!files || files.length === 0) return;
  const fileArray = Array.from(files);
  
  isTransferring = true;
  const card = document.getElementById('transferCard');
  card.classList.add('active');

  let successCount = 0;
  let failCount = 0;

  for (let i = 0; i < fileArray.length; i++) {
    const file = fileArray[i];
    const relPath = file.webkitRelativePath || file.name;
    document.getElementById('transFileName').textContent = `[${i + 1}/${fileArray.length}] ${file.name}`;
    
    try {
      await uploadFileWithSmartResume(file, relPath);
      successCount++;
    } catch (e) {
      failCount++;
      showToast(`Upload failed for ${file.name}: ${e.message}`, 'error');
    }
  }

  isTransferring = false;
  setTimeout(() => card.classList.remove('active'), 2500);
  if (failCount === 0) {
    document.getElementById('transStatus').textContent = 'All transfers complete!';
    showToast('Upload complete!', 'success');
  } else {
    document.getElementById('transStatus').textContent = `${successCount} transferred, ${failCount} failed`;
    showToast(`Transfers finished: ${failCount} file(s) failed`, 'error');
  }
  refreshActiveDirectory();
  fetchStorageMetrics();
}

async function uploadFileWithSmartResume(file, relPath) {
  const fullTargetRel = curRecvPath ? curRecvPath + '/' + relPath : relPath;
  
  // 1. Check existing byte length on server for smart resumption
  let startOffset = 0;
  try {
    const checkRes = await fetch('/api/check?path=' + encodeURIComponent(fullTargetRel));
    const checkData = await checkRes.json();
    if (checkData.exists && checkData.size > 0) {
      if (checkData.size === file.size) {
        // File already completely transferred
        updateProgressUI(file.size, file.size, 'File already complete');
        return;
      } else if (checkData.size < file.size) {
        startOffset = checkData.size;
        showToast(`Resuming ${file.name} from ${formatBytes(startOffset)}...`, 'info');
      }
    }
  } catch (e) {
    startOffset = 0;
  }

  // 2. Perform resumable stream upload with watchdog timer
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let lastLoaded = startOffset;
    let lastTime = Date.now();

    // 25-Second Watchdog Timer
    let lastActive = Date.now();
    const watchdog = setInterval(() => {
      if (Date.now() - lastActive > 25000) {
        clearInterval(watchdog);
        xhr.abort();
        reject(new Error('Connection timed out'));
      }
    }, 4000);

    xhr.upload.onprogress = e => {
      lastActive = Date.now();
      const currentTotalLoaded = startOffset + e.loaded;
      const now = Date.now();
      const dt = (now - lastTime) / 1000;
      if (dt > 0.4) {
        const speedMB = ((e.loaded - (lastLoaded - startOffset)) / (1024 * 1024)) / dt;
        lastLoaded = currentTotalLoaded;
        lastTime = now;
        document.getElementById('transSpeed').textContent = `${speedMB.toFixed(1)} MB/s`;
      }
      updateProgressUI(currentTotalLoaded, file.size, 'Uploading...');
    };

    xhr.open('POST', '/api/upload?path=' + encodeURIComponent(fullTargetRel) + '&offset=' + startOffset, true);

    xhr.onload = () => {
      clearInterval(watchdog);
      if (xhr.status === 200) {
        updateProgressUI(file.size, file.size, 'Saved');
        resolve();
      } else {
        let errMsg = xhr.statusText || 'Upload failed';
        try {
          const resp = JSON.parse(xhr.responseText);
          if (resp && resp.error) errMsg = resp.error;
          else if (resp && resp.message) errMsg = resp.message;
        } catch (_) {}
        reject(new Error(errMsg));
      }
    };

    xhr.onerror = () => {
      clearInterval(watchdog);
      reject(new Error('Network error during upload'));
    };

    xhr.onabort = () => {
      clearInterval(watchdog);
      reject(new Error('Upload aborted'));
    };

    const payload = startOffset > 0 ? file.slice(startOffset) : file;
    xhr.send(payload);
  });
}

function updateProgressUI(loaded, total, statusText) {
  const pct = total > 0 ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
  document.getElementById('transPercent').textContent = pct + '%';
  document.getElementById('transProgressFill').style.width = pct + '%';
  document.getElementById('transStatus').textContent = `${formatBytes(loaded)} / ${formatBytes(total)} &bull; ${statusText}`;
}

/* ═══════════════════════════════════════════════════════════════════════════════
   FULL-WINDOW DRAG-AND-DROP OVERLAY HANDLERS
   ═══════════════════════════════════════════════════════════════════════════════ */
let dragCounter = 0;
const dropOverlay = document.getElementById('windowDropOverlay');

window.addEventListener('dragenter', e => {
  e.preventDefault();
  dragCounter++;
  if (dropOverlay) dropOverlay.classList.add('active');
});

window.addEventListener('dragover', e => {
  e.preventDefault();
});

window.addEventListener('dragleave', e => {
  e.preventDefault();
  dragCounter--;
  if (dragCounter <= 0 && dropOverlay) {
    dragCounter = 0;
    dropOverlay.classList.remove('active');
  }
});

window.addEventListener('drop', e => {
  e.preventDefault();
  dragCounter = 0;
  if (dropOverlay) dropOverlay.classList.remove('active');
  if (e.dataTransfer && e.dataTransfer.files.length > 0) {
    handleFileSelect(e.dataTransfer.files);
  }
});

/* ═══════════════════════════════════════════════════════════════════════════════
   IN-BROWSER HOST FOLDER NAVIGATOR MODAL (Linear / Apple Files Architecture)
   ═══════════════════════════════════════════════════════════════════════════════ */
let modalCurrentSubdirs = [];
let modalParentPath = '';
let modalIsRoot = false;
let modalFocusedFolderIndex = -1;

function escapeJs(str) {
  return (str || '').replace(/\\/g, '\\\\').replace(/'/g, "\\'");
}

function openHostBrowserModal(target) {
  browserModalTarget = target;
  document.getElementById('hostBrowserModalTitle').textContent = target === 'recv' 
    ? 'Select PC Folder for Inbox' 
    : 'Select PC Folder to Share';
  
  let initialPath = target === 'recv' 
    ? (document.getElementById('recvPathText')?.textContent.trim() || '') 
    : (document.getElementById('sharePathText')?.textContent.trim() || '');
  if (initialPath === 'No folder selected' || initialPath === 'Not configured') initialPath = '';

  toggleInlineFolderCreator(false);
  const manualRow = document.getElementById('modalManualPathRow');
  if (manualRow) manualRow.style.display = 'none';
  const filterInput = document.getElementById('modalFolderFilterInput');
  if (filterInput) filterInput.value = '';

  openModal('hostBrowserModal');
  browseModalPath(initialPath);
  
  setTimeout(() => {
    if (filterInput) filterInput.focus();
    updateDriveArrows();
  }, 100);
}

async function browseModalPath(path) {
  const listEl = document.getElementById('modalFolderTreeList');
  const inputEl = document.getElementById('modalCurrentPathInput');
  
  listEl.innerHTML = '<div style="padding: 24px; color: var(--text-tertiary); text-align: center; display: flex; align-items: center; justify-content: center; gap: 8px;"><svg class="icon" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg><span>Scanning directories...</span></div>';
  
  try {
    const res = await fetch('/api/browse_host?path=' + encodeURIComponent(path || ''));
    if (res.status === 401) {
      closeModal('hostBrowserModal');
      openAuthModal();
      showToast('Authentication required to browse PC storage.', 'info');
      return;
    }
    const data = await res.json();
    
    activeModalBrowsePath = data.current_path || '';
    modalParentPath = (data.parent_path !== undefined) ? data.parent_path : '';
    modalIsRoot = !!data.is_root;
    modalCurrentSubdirs = data.subdirs || [];
    modalBrowseError = data.error || null;
    modalFocusedFolderIndex = -1;

    if (inputEl) inputEl.value = activeModalBrowsePath;

    // Render Drive Cards with visual storage capacity meters
    renderModalDrives(data.drives || []);

    // Render Segmented Breadcrumb Trail
    renderModalBreadcrumbs(activeModalBrowsePath, modalIsRoot);

    // Update "Up One Level" button state
    const upBtn = document.getElementById('modalBtnUpLevel');
    if (upBtn) {
      upBtn.disabled = (modalIsRoot && !activeModalBrowsePath);
    }

    // Filter and render subfolder list
    filterModalFolders();

    if (modalBrowseError) {
      showToast(modalBrowseError, 'error');
    }

  } catch (e) {
    listEl.innerHTML = `<div style="padding: 20px; color: var(--status-error); text-align: center;">Error browsing: ${escapeHtml(e.message)}</div>`;
  }
}

function renderModalDrives(drives) {
  const drivesBar = document.getElementById('modalDrivesBar');
  if (!drivesBar) return;
  drivesBar.innerHTML = '';

  if (!drives || drives.length === 0) return;

  drives.forEach(d => {
    const usedPct = d.used_pct !== undefined ? d.used_pct : (d.used_percent ? Math.round(d.used_percent) : 0);
    const fillClass = usedPct >= 95 ? 'danger' : (usedPct >= 85 ? 'warning' : '');
    
    const drivePathNorm = (d.path || '').toUpperCase().replace(/\\/g, '/');
    const curPathNorm = (activeModalBrowsePath || '').toUpperCase().replace(/\\/g, '/');
    const isSlashRoot = drivePathNorm === '/';
    const pathMatches = isSlashRoot
      ? curPathNorm === '/'
      : (curPathNorm && curPathNorm.startsWith(drivePathNorm));
    const letterMatches = d.letter && /^[A-Za-z]$/.test(d.letter) && curPathNorm.startsWith(d.letter.toUpperCase() + ':');
    const isActive = curPathNorm && (pathMatches || letterMatches);

    const card = document.createElement('div');
    card.className = 'drive-card' + (isActive ? ' active' : '');
    
    const iconSvg = d.is_system
      ? '<svg class="icon" viewBox="0 0 24 24"><rect width="20" height="8" x="2" y="14" rx="2"/><path d="M6 18h.01"/><path d="M10 18h.01"/><path d="M4 14v-4a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v4"/></svg>'
      : '<svg class="icon" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>';

    const badgeLabel = isActive ? 'Active' : (d.letter ? (/^[A-Za-z]$/.test(d.letter) ? d.letter + ':' : d.letter) : 'Disk');

    card.innerHTML = `
      <div class="drive-card-top">
        <div class="drive-card-title">
          ${iconSvg}
          <span>${escapeHtml(d.label || d.name)}</span>
        </div>
        <span class="drive-card-badge">${badgeLabel}</span>
      </div>
      <div class="drive-card-meter">
        <div class="drive-card-meter-fill ${fillClass}" style="width: ${Math.min(100, Math.max(0, usedPct))}%;"></div>
      </div>
      <div class="drive-card-info">
        <span>${escapeHtml(d.free_gb)} GB Free</span>
        <span>${usedPct}% used</span>
      </div>
    `;

    card.onclick = () => browseModalPath(d.path);
    drivesBar.appendChild(card);
  });

  setupDriveRibbonListeners();
  setTimeout(updateDriveArrows, 60);
}

function scrollDriveRibbon(dir) {
  const bar = document.getElementById('modalDrivesBar');
  if (!bar) return;
  const scrollAmount = 220;
  bar.scrollBy({ left: dir * scrollAmount, behavior: 'smooth' });
  setTimeout(updateDriveArrows, 250);
}

function updateDriveArrows() {
  const bar = document.getElementById('modalDrivesBar');
  const btnL = document.getElementById('driveRibbonLeft');
  const btnR = document.getElementById('driveRibbonRight');
  if (!bar || !btnL || !btnR) return;
  const canScrollLeft = bar.scrollLeft > 6;
  const canScrollRight = bar.scrollLeft + bar.clientWidth < bar.scrollWidth - 6;
  btnL.classList.toggle('disabled', !canScrollLeft);
  btnR.classList.toggle('disabled', !canScrollRight);
}

function setupDriveRibbonListeners() {
  const bar = document.getElementById('modalDrivesBar');
  if (bar && !bar._wheelAttached) {
    bar._wheelAttached = true;
    bar.addEventListener('wheel', (e) => {
      if (e.deltaY !== 0) {
        e.preventDefault();
        bar.scrollLeft += e.deltaY;
        updateDriveArrows();
      }
    }, { passive: false });
    bar.addEventListener('scroll', updateDriveArrows);
    window.addEventListener('resize', updateDriveArrows);
  }
}

function parseModalPathSegments(fullPath) {
  if (!fullPath) return [];
  const normalized = fullPath.replace(/\\/g, '/');
  const isWindows = /^[a-zA-Z]:/.test(normalized);
  const segments = [];
  const parts = normalized.split('/').filter(p => p.length > 0);

  if (isWindows && parts.length > 0) {
    const driveLetter = parts[0];
    let accumulated = driveLetter + '\\';
    segments.push({ name: driveLetter + '\\', path: accumulated });
    
    for (let i = 1; i < parts.length; i++) {
      accumulated = accumulated + (accumulated.endsWith('\\') ? '' : '\\') + parts[i];
      segments.push({ name: parts[i], path: accumulated });
    }
  } else {
    let accumulated = '';
    segments.push({ name: '/', path: '/' });
    for (let i = 0; i < parts.length; i++) {
      accumulated += '/' + parts[i];
      segments.push({ name: parts[i], path: accumulated });
    }
  }
  return segments;
}

function renderModalBreadcrumbs(fullPath, isRoot) {
  const trailEl = document.getElementById('modalBreadcrumbTrail');
  if (!trailEl) return;
  trailEl.innerHTML = '';

  if (isRoot || !fullPath) {
    trailEl.innerHTML = '<span class="crumb-btn active">PC Storage Drives</span>';
    return;
  }

  const segments = parseModalPathSegments(fullPath);
  if (segments.length === 0) {
    trailEl.innerHTML = '<span class="crumb-btn active">' + escapeHtml(fullPath) + '</span>';
    return;
  }

  segments.forEach((seg, idx) => {
    const isLast = idx === segments.length - 1;
    if (isLast) {
      const activeCrumb = document.createElement('span');
      activeCrumb.className = 'crumb-btn active';
      activeCrumb.textContent = seg.name;
      activeCrumb.title = seg.path;
      trailEl.appendChild(activeCrumb);
    } else {
      const crumbBtn = document.createElement('button');
      crumbBtn.className = 'crumb-btn';
      crumbBtn.type = 'button';
      crumbBtn.textContent = seg.name;
      crumbBtn.title = seg.path;
      crumbBtn.onclick = () => browseModalPath(seg.path);
      trailEl.appendChild(crumbBtn);

      const divider = document.createElement('span');
      divider.className = 'crumb-divider';
      divider.textContent = '›';
      trailEl.appendChild(divider);
    }
  });

  trailEl.scrollLeft = trailEl.scrollWidth;
}

function modalNavigateUp() {
  if (modalParentPath !== undefined && modalParentPath !== '') {
    browseModalPath(modalParentPath);
  } else if (!modalIsRoot) {
    browseModalPath('');
  }
}

function toggleManualPathInput() {
  const row = document.getElementById('modalManualPathRow');
  if (!row) return;
  const isShown = row.style.display !== 'none';
  row.style.display = isShown ? 'none' : 'flex';
  if (!isShown) {
    const input = document.getElementById('modalCurrentPathInput');
    if (input) {
      input.value = activeModalBrowsePath;
      input.focus();
      input.select();
    }
  }
}

function filterModalFolders() {
  const input = document.getElementById('modalFolderFilterInput');
  const clearBtn = document.getElementById('modalFilterClearBtn');
  const badge = document.getElementById('modalFilterMatchCount');
  const listEl = document.getElementById('modalFolderTreeList');
  if (!listEl) return;

  const query = (input?.value || '').trim().toLowerCase();
  if (clearBtn) clearBtn.style.display = query ? 'flex' : 'none';

  const filtered = query
    ? modalCurrentSubdirs.filter(s => (s.name || '').toLowerCase().includes(query))
    : modalCurrentSubdirs;

  if (badge) {
    if (query) {
      badge.textContent = `${filtered.length} / ${modalCurrentSubdirs.length}`;
    } else {
      badge.textContent = modalCurrentSubdirs.length ? `${modalCurrentSubdirs.length} folders` : '';
    }
  }

  listEl.innerHTML = '';
  modalFocusedFolderIndex = -1;

  if (modalBrowseError) {
    const errDiv = document.createElement('div');
    errDiv.style.padding = '20px 16px';
    errDiv.style.margin = '12px 8px';
    errDiv.style.borderRadius = '8px';
    errDiv.style.backgroundColor = 'rgba(239, 68, 68, 0.1)';
    errDiv.style.border = '1px solid rgba(239, 68, 68, 0.3)';
    errDiv.style.color = '#ef4444';
    errDiv.style.textAlign = 'center';
    errDiv.innerHTML = `
      <div style="font-weight: 600; margin-bottom: 6px; display: flex; align-items: center; justify-content: center; gap: 6px;">
        <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span>Folder Inaccessible</span>
      </div>
      <div style="font-size: 13px; color: var(--text-secondary);">${escapeHtml(modalBrowseError)}</div>
    `;
    listEl.appendChild(errDiv);
    return;
  }

  if (filtered.length === 0) {
    if (modalCurrentSubdirs.length === 0 && !modalIsRoot) {
      const emptyDiv = document.createElement('div');
      emptyDiv.style.padding = '24px 16px';
      emptyDiv.style.color = 'var(--text-tertiary)';
      emptyDiv.style.textAlign = 'center';
      emptyDiv.textContent = 'No subfolders in this directory';
      listEl.appendChild(emptyDiv);
    } else if (query) {
      const noMatchDiv = document.createElement('div');
      noMatchDiv.style.padding = '24px 16px';
      noMatchDiv.style.color = 'var(--text-tertiary)';
      noMatchDiv.style.textAlign = 'center';
      noMatchDiv.innerHTML = `
        <div>No folders match "${escapeHtml(query)}"</div>
        <button class="btn btn-ghost btn-sm" onclick="clearModalFilter()" style="margin-top: 8px;">Clear filter</button>
      `;
      listEl.appendChild(noMatchDiv);
    }
    return;
  }

  filtered.forEach((s, idx) => {
    const itemRow = document.createElement('div');
    itemRow.className = 'folder-tree-item';
    itemRow.setAttribute('data-index', idx);
    itemRow.setAttribute('data-path', s.path);
    itemRow.innerHTML = `
      <div class="folder-tree-item-name">
        <svg class="icon" style="color: #fbbf24;" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
        <span title="${escapeHtml(s.name)}">${escapeHtml(s.name)}</span>
      </div>
      <svg class="icon" style="color: var(--text-tertiary);" viewBox="0 0 24 24"><polyline points="9 18 15 12 9 6"/></svg>
    `;
    itemRow.onclick = () => browseModalPath(s.path);
    listEl.appendChild(itemRow);
  });
}

function clearModalFilter() {
  const input = document.getElementById('modalFolderFilterInput');
  if (input) {
    input.value = '';
    filterModalFolders();
    input.focus();
  }
}

function toggleInlineFolderCreator(show) {
  const creator = document.getElementById('modalInlineFolderCreator');
  const input = document.getElementById('inlineNewFolderName');
  if (!creator) return;

  if (show === undefined) {
    show = creator.style.display === 'none';
  }

  creator.style.display = show ? 'flex' : 'none';
  if (show) {
    if (input) {
      input.value = '';
      input.focus();
    }
  }
}

function promptCreateNewFolder() {
  toggleInlineFolderCreator(true);
}

async function submitInlineNewFolder() {
  const input = document.getElementById('inlineNewFolderName');
  const name = (input?.value || '').trim();
  if (!name) {
    showToast('Please enter a folder name', 'error');
    if (input) input.focus();
    return;
  }

  if (/[\\/:*?"<>|]/.test(name)) {
    showToast('Folder name contains invalid characters', 'error');
    if (input) input.focus();
    return;
  }

  try {
    const res = await fetch('/api/create_folder', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent: activeModalBrowsePath, name: name })
    });
    const data = await res.json();
    if (data.success || data.status === 'ok') {
      showToast('Created folder: ' + name, 'success');
      toggleInlineFolderCreator(false);
      browseModalPath(data.path || (activeModalBrowsePath ? activeModalBrowsePath + '\\' + name : name));
    } else {
      showToast('Could not create folder: ' + (data.error || 'Unknown error'), 'error');
    }
  } catch (e) {
    showToast('Failed to create folder: ' + e.message, 'error');
  }
}

async function confirmModalFolderSelection() {
  const chosen = (activeModalBrowsePath || document.getElementById('modalCurrentPathInput')?.value || '').trim();
  if (!chosen) return;

  try {
    const res = await fetch('/api/set_path', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target: browserModalTarget, type: browserModalTarget, path: chosen })
    });
    const data = await res.json();
    if (data.status === 'ok' || data.success) {
      closeModal('hostBrowserModal');
      showToast('Folder updated: ' + chosen, 'success');
      if (browserModalTarget === 'recv') {
        document.getElementById('recvPathText').textContent = chosen;
      } else {
        document.getElementById('sharePathText').textContent = chosen;
        const shareMeter = document.getElementById('shareStorageMeter');
        if (shareMeter) shareMeter.style.display = 'flex';
      }
      fetchStorageMetrics();
      loadDirectory(activeTab, '', true);
    } else {
      showToast('Error setting path: ' + (data.error || 'Unknown error'), 'error');
    }
  } catch (e) {
    showToast('Failed to update folder: ' + e.message, 'error');
  }
}

// In-Modal Keyboard Navigation & Shortcuts
document.addEventListener('DOMContentLoaded', () => {
  const filterInput = document.getElementById('modalFolderFilterInput');
  if (filterInput) {
    filterInput.addEventListener('input', () => filterModalFolders());
    filterInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const items = document.querySelectorAll('#modalFolderTreeList .folder-tree-item');
        if (items.length === 1) {
          const path = items[0].getAttribute('data-path');
          if (path) browseModalPath(path);
        } else if (!filterInput.value.trim() && activeModalBrowsePath) {
          confirmModalFolderSelection();
        }
      } else if (e.key === 'ArrowDown') {
        e.preventDefault();
        const items = document.querySelectorAll('#modalFolderTreeList .folder-tree-item');
        if (items.length > 0) {
          modalFocusedFolderIndex = 0;
          items.forEach((it, i) => it.classList.toggle('focused', i === 0));
          items[0].scrollIntoView({ block: 'nearest' });
          document.getElementById('modalFolderTreeList')?.focus();
        }
      }
    });
  }

  const inlineInput = document.getElementById('inlineNewFolderName');
  if (inlineInput) {
    inlineInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        submitInlineNewFolder();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        e.stopPropagation();
        toggleInlineFolderCreator(false);
      }
    });
  }

  const treeList = document.getElementById('modalFolderTreeList');
  if (treeList) {
    treeList.addEventListener('keydown', (e) => {
      const items = Array.from(treeList.querySelectorAll('.folder-tree-item'));
      if (items.length === 0) return;

      if (e.key === 'ArrowDown') {
        e.preventDefault();
        modalFocusedFolderIndex = Math.min(items.length - 1, modalFocusedFolderIndex + 1);
        items.forEach((it, i) => it.classList.toggle('focused', i === modalFocusedFolderIndex));
        if (modalFocusedFolderIndex >= 0) items[modalFocusedFolderIndex].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        modalFocusedFolderIndex = Math.max(0, modalFocusedFolderIndex - 1);
        items.forEach((it, i) => it.classList.toggle('focused', i === modalFocusedFolderIndex));
        if (modalFocusedFolderIndex >= 0) items[modalFocusedFolderIndex].scrollIntoView({ block: 'nearest' });
      } else if (e.key === 'Enter') {
        e.preventDefault();
        if (modalFocusedFolderIndex >= 0 && modalFocusedFolderIndex < items.length) {
          const path = items[modalFocusedFolderIndex].getAttribute('data-path');
          if (path) browseModalPath(path);
        }
      }
    });
  }
});

/* ═══════════════════════════════════════════════════════════════════════════════
   HOST OS INTEGRATION (PowerShell STA Picker & OS Explorer Spawner)
   ═══════════════════════════════════════════════════════════════════════════════ */
async function triggerNativePicker(target) {
  const isWin = (window.hostOS || 'windows') === 'windows';
  showToast(`Opening native ${isWin ? 'Windows' : 'system'} folder dialog on Host PC...`, 'info');
  try {
    const res = await fetch('/api/pick_folder?target=' + target);
    const data = await res.json();
    if (data.success || data.status === 'ok') {
      showToast('Selected: ' + data.path, 'success');
      if (target === 'recv') {
        document.getElementById('recvPathText').textContent = data.path;
      } else {
        document.getElementById('sharePathText').textContent = data.path;
        document.getElementById('shareStorageMeter').style.display = 'flex';
      }
      closeModal('hostBrowserModal');
      fetchStorageMetrics();
      loadDirectory(activeTab, '', true);
    } else if (data.error !== 'cancelled') {
      showToast(data.message || ((isWin ? 'Windows dialog notice: ' : 'System dialog notice: ') + data.error), 'info');
    }
  } catch (e) {
    showToast('Failed to trigger dialog: ' + e.message, 'error');
  }
}

async function openInExplorer(target) {
  try {
    const res = await fetch('/api/open_folder?type=' + target);
    const data = await res.json();
    if (data.success || data.status === 'ok') {
      showToast(data.message || 'Opened folder in file manager', 'success');
      if (!data.is_local && !data.is_local_client) {
        // Remote client viewing fallback: switch active tab to view in browser
        switchTab(target);
      }
    } else {
      showToast(data.message || ('Could not open folder: ' + data.error), 'error');
    }
  } catch (e) {
    showToast('Failed to launch file manager: ' + e.message, 'error');
  }
}

/* ═══════════════════════════════════════════════════════════════════════════════
   FAQ ACCORDION & FILTER FUNCTIONS (R4)
   ═══════════════════════════════════════════════════════════════════════════════ */
function toggleFaq(el) {
  const item = el.closest('.faq-item');
  if (item) {
    item.classList.toggle('open');
  }
}

function setFaqCategory(category, pillEl) {
  activeFaqCategory = category;
  document.querySelectorAll('.faq-pill').forEach(p => p.classList.remove('active'));
  if (pillEl) pillEl.classList.add('active');
  const searchVal = document.getElementById('faqSearchInput')?.value || '';
  filterFaq(searchVal);
}

function filterFaq(query) {
  const q = (query || '').toLowerCase().trim();
  const items = document.querySelectorAll('.faq-item');
  
  items.forEach(item => {
    const category = item.getAttribute('data-category') || '';
    const keywords = (item.getAttribute('data-keywords') || '').toLowerCase();
    const text = item.textContent.toLowerCase();

    const matchesCategory = (activeFaqCategory === 'all' || category === activeFaqCategory);
    const matchesSearch = !q || keywords.includes(q) || text.includes(q);

    if (matchesCategory && matchesSearch) {
      item.style.display = 'block';
      if (q) {
        item.classList.add('open');
      }
    } else {
      item.style.display = 'none';
      item.classList.remove('open');
    }
  });
}

/* ═══════════════════════════════════════════════════════════════════════════════
   STORAGE METRICS REFRESH
   ═══════════════════════════════════════════════════════════════════════════════ */
async function fetchStorageMetrics() {
  try {
    const recvPath = document.getElementById('recvPathText')?.textContent;
    if (recvPath) {
      const res = await fetch('/api/disk?path=' + encodeURIComponent(recvPath));
      if (res.ok) {
        const di = await res.json();
        const lbl = document.getElementById('recvDiskLabel');
        if (lbl) lbl.textContent = `${di.free_gb} GB Free (${di.used_pct}% used)`;
        const bar = document.getElementById('recvDiskBar');
        if (bar) {
          bar.style.width = di.used_pct + '%';
          bar.className = 'storage-fill' + (di.used_pct > 70 ? ' warn' : '');
        }
      }
    }
    const sharePath = document.getElementById('sharePathText')?.textContent;
    if (sharePath && sharePath !== 'Not configured' && sharePath !== 'No folder selected' && sharePath !== '') {
      const resShare = await fetch('/api/disk?path=' + encodeURIComponent(sharePath));
      if (resShare.ok) {
        const diShare = await resShare.json();
        const lblShare = document.getElementById('shareDiskLabel');
        if (lblShare) lblShare.textContent = `${diShare.free_gb} GB Free (${diShare.used_pct}% used)`;
        const barShare = document.getElementById('shareDiskBar');
        if (barShare) {
          barShare.style.width = diShare.used_pct + '%';
          barShare.className = 'storage-fill' + (diShare.used_pct > 70 ? ' warn' : '');
        }
        const meterShare = document.getElementById('shareStorageMeter');
        if (meterShare) meterShare.style.display = 'flex';
      }
    }
  } catch (e) {}
}

/* ═══════════════════════════════════════════════════════════════════════════════
   UI MODALS, TOASTS & UTILITIES
   ═══════════════════════════════════════════════════════════════════════════════ */
function copyAddress(text, cardEl) {
  if (!text) return;
  navigator.clipboard.writeText(text).then(() => {
    showToast('Copied address: ' + text, 'success');
    if (cardEl) {
      cardEl.classList.add('copied');
      setTimeout(() => cardEl.classList.remove('copied'), 1800);
    }
  }).catch(() => {
    prompt('Copy address:', text);
  });
}

let currentSelectedQrUrl = '';

function showGeneralQR() {
  const ifaces = window.hostNetInterfaces || [];
  // Prioritize wifi > hotspot > ethernet > first non-virtual > first available
  let best = ifaces.find(i => i.kind === 'wifi') 
          || ifaces.find(i => i.kind === 'hotspot')
          || ifaces.find(i => i.kind === 'ethernet' || i.kind === 'ethernet-direct')
          || ifaces.find(i => i.kind !== 'virtual')
          || ifaces[0];
          
  const targetUrl = best ? best.url : window.location.origin;
  const targetLabel = best ? best.label : 'Network Connection';
  showQRModal(targetUrl, targetLabel);
}

function selectQRInterface(url, label) {
  currentSelectedQrUrl = url;
  const qrImg = document.getElementById('qrModalImg');
  const qrUrlEl = document.getElementById('qrModalUrl');
  const qrActiveBadge = document.getElementById('qrActiveBadge');

  if (qrImg) qrImg.src = '/api/qr?url=' + encodeURIComponent(url);
  if (qrUrlEl) qrUrlEl.textContent = url;
  if (qrActiveBadge) qrActiveBadge.textContent = label || 'Network IP';

  // Highlight active chip
  const chips = document.querySelectorAll('.qr-chip');
  chips.forEach(c => {
    c.classList.toggle('active', c.getAttribute('data-url') === url);
  });
}

function showQRModal(url, label) {
  const ifaces = window.hostNetInterfaces || [];
  
  // If url is localhost or 127.0.0.1, NEVER generate a QR for localhost!
  // Instead, select the best real network interface for the phone to scan!
  const isLoopback = !url || url.includes('127.0.0.1') || url.includes('localhost') || url.includes('::1');
  if (isLoopback) {
    let best = ifaces.find(i => i.kind === 'wifi') 
            || ifaces.find(i => i.kind === 'hotspot')
            || ifaces.find(i => i.kind === 'ethernet' || i.kind === 'ethernet-direct')
            || ifaces.find(i => i.kind !== 'virtual')
            || ifaces[0];
    if (best) {
      url = best.url;
      label = best.label;
    }
  }

  // Populate Interface Switcher Chips
  const chipsContainer = document.getElementById('qrInterfaceChips');
  if (chipsContainer && ifaces.length > 0) {
    chipsContainer.innerHTML = '';
    ifaces.forEach(i => {
      const chip = document.createElement('div');
      chip.className = 'qr-chip' + (i.url === url ? ' active' : '');
      chip.setAttribute('data-url', i.url);
      chip.innerHTML = `<span>${escapeHtml(i.label)}</span> <span class="mono" style="opacity: 0.7; font-size: 10px;">${escapeHtml(i.ip)}</span>`;
      chip.onclick = () => selectQRInterface(i.url, i.label);
      chipsContainer.appendChild(chip);
    });
    chipsContainer.style.display = ifaces.length > 1 ? 'flex' : 'none';
  } else if (chipsContainer) {
    chipsContainer.style.display = 'none';
  }

  selectQRInterface(url, label);

  // Update Host PC section visibility & port
  const hostSection = document.getElementById('qrHostSection');
  if (hostSection) {
    hostSection.style.display = isHostClient ? 'block' : 'none';
  }

  openModal('qrModal');
}

function showToast(msg, type = 'info') {
  const container = document.getElementById('toastContainer');
  const toast = document.createElement('div');
  toast.className = `toast-item toast-${type}`;
  
  let iconSvg = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>';
  if (type === 'success') {
    iconSvg = '<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>';
  } else if (type === 'error') {
    iconSvg = '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>';
  }

  toast.innerHTML = `
    <div class="toast-icon">${iconSvg}</div>
    <div class="toast-content">
      <div class="toast-title">${type.charAt(0).toUpperCase() + type.slice(1)}</div>
      <div class="toast-desc">${escapeHtml(msg)}</div>
    </div>
    <button class="icon-btn-micro" onclick="this.closest('.toast-item').remove()">
      <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
    </button>
  `;

  container.appendChild(toast);
  setTimeout(() => toast.classList.add('show'), 10);
  setTimeout(() => {
    toast.classList.remove('show');
    setTimeout(() => toast.remove(), 250);
  }, 3800);
}

function openModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.add('open');
}

function closeModal(id) {
  const el = document.getElementById(id);
  if (el) el.classList.remove('open');
}

window.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
  }
});

function formatBytes(bytes) {
  if (bytes === 0) return '0 B';
  const k = 1024;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(1)) + ' ' + sizes[i];
}

function escapeHtml(str) {
  return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

/* Auto-sync directory listing every 5 seconds if idle */
setInterval(() => {
  if (!isTransferring && !document.querySelector('.modal-overlay.open')) {
    const curPath = activeTab === 'recv' ? curRecvPath : curSharePath;
    loadDirectory(activeTab, curPath, false);
  }
}, 5000);

/* ═══════════════════════════════════════════════════════════════════════════════
   AUTHENTICATION & ACCESS CONTROL (Persistent Security Engine)
   ═══════════════════════════════════════════════════════════════════════════════ */
let isCallerAuthenticated = false;

async function checkAuthStatus() {
  try {
    const res = await fetch('/api/check_auth');
    const data = await res.json();
    isCallerAuthenticated = !!data.authenticated;
    updateAuthUI();
    if (isLocalhost()) {
      refreshSessionsList();
    }
  } catch (e) {}
}

function updateAuthUI() {
  const badge = document.getElementById('authStatusBadge');
  const hostPill = document.getElementById('hostStatusPill');
  const guestPill = document.getElementById('guestStatusPill');
  const secBtn = document.getElementById('btnSecurityModal');

  const isLocal = isLocalhost();

  if (isLocal) {
    if (hostPill) hostPill.style.display = 'inline-flex';
    if (guestPill) guestPill.style.display = 'none';
    if (secBtn) secBtn.style.display = 'inline-flex';
    if (badge) badge.innerHTML = '';
  } else {
    if (hostPill) hostPill.style.display = 'none';
    if (guestPill) guestPill.style.display = 'inline-flex';
    if (secBtn) secBtn.style.display = 'none';

    if (badge) {
      if (isCallerAuthenticated) {
        badge.innerHTML = `
          <div class="remote-auth-pill">
            <span class="pulse-dot"></span>
            <span style="font-weight:600;">Remote Session</span>
            <button class="btn-logout" onclick="logoutAuth()" title="Disconnect this session">Log Out</button>
          </div>`;
      } else {
        badge.innerHTML = `
          <button class="btn btn-ghost btn-sm" onclick="openAuthModal()" style="color: #60a5fa; border: 1px solid rgba(96,165,250,0.25); background: rgba(96,165,250,0.08);" title="Enter Master Passcode to manage host storage">
            <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
            <span class="btn-label">Passcode</span>
          </button>`;
      }
    }
  }
}

function openAuthModal() {
  openModal('authModal');
  setTimeout(() => {
    const inp = document.getElementById('authPasswordInput');
    if (inp) inp.focus();
  }, 100);
}

async function submitAuthLogin() {
  const inp = document.getElementById('authPasswordInput');
  const err = document.getElementById('authErrorMsg');
  const btn = document.getElementById('authSubmitBtn');
  const val = inp ? inp.value.trim() : '';
  if (!val) return;

  btn.disabled = true;
  btn.innerText = 'Verifying...';
  err.style.display = 'none';

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: val })
    });
    const data = await res.json();
    if (res.ok && data.success) {
      isCallerAuthenticated = true;
      closeModal('authModal');
      if (inp) inp.value = '';
      updateAuthUI();
      showToast('Authenticated successfully! Full access unlocked.', 'success');
      loadDirectory(activeTab, activeTab === 'recv' ? curRecvPath : curSharePath, true);
    } else {
      err.innerText = data.error === 'rate_limited' ? ('Too many attempts. Locked out for ' + data.retry_after + 's.') : 'Invalid passcode or access key.';
      err.style.display = 'block';
    }
  } catch (e) {
    err.innerText = 'Network error during authentication.';
    err.style.display = 'block';
  } finally {
    btn.disabled = false;
    btn.innerText = 'Unlock Access';
  }
}

async function logoutAuth() {
  try {
    await fetch('/api/logout', { method: 'POST' });
  } catch (e) {}
  isCallerAuthenticated = false;
  // Immediately reload so server renders the standalone Passcode Gate screen!
  window.location.reload();
}

/* ═══════════════════════════════════════════════════════════════════════════════
   HOST SECURITY & REMOTE SESSIONS CENTER (Physical Host Only)
   ═══════════════════════════════════════════════════════════════════════════════ */
async function loadHostSecurityInfo() {
  const display = document.getElementById('hostPasscodeDisplay');
  const tip = document.getElementById('hostPasscodeTip');
  const badge = document.getElementById('passcodeTypeBadge');
  if (!display) return;

  try {
    const res = await fetch('/api/host_security_info');
    if (res.ok) {
      const data = await res.json();
      if (data.success) {
        display.innerText = data.passcode || '[Stored hashed in .env]';
        display.dataset.passcode = data.passcode || '';
        if (tip && data.tip) {
          tip.innerText = data.tip;
        }
        if (badge) {
          badge.innerText = data.is_custom ? 'Personal Passcode' : 'Auto-Generated';
          badge.style.background = data.is_custom ? 'rgba(74, 222, 128, 0.15)' : 'rgba(56, 189, 248, 0.15)';
          badge.style.color = data.is_custom ? '#4ade80' : '#38bdf8';
        }
      }
    }
  } catch (e) {
    console.warn('Failed to load host security info:', e);
  }
}

function copyHostPasscode() {
  const display = document.getElementById('hostPasscodeDisplay');
  const btnText = document.getElementById('btnCopyPasscodeText');
  const code = (display && (display.dataset.passcode || display.innerText)) || '';
  if (!code || code.includes('Loading') || code.includes('Stored hashed')) return;

  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(code).then(() => {
      if (btnText) {
        btnText.innerText = 'Copied!';
        setTimeout(() => { btnText.innerText = 'Copy'; }, 2000);
      }
    });
  } else {
    const ta = document.createElement('textarea');
    ta.value = code;
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
    if (btnText) {
      btnText.innerText = 'Copied!';
      setTimeout(() => { btnText.innerText = 'Copy'; }, 2000);
    }
  }
}

function openSecurityModal() {
  openModal('securityModal');
  loadHostSecurityInfo();
  refreshSessionsList();
}

function closeSecurityModal() {
  closeModal('securityModal');
}

async function refreshSessionsList() {
  const tbody = document.getElementById('sessionsTableBody');
  const countBadge = document.getElementById('headerSessionCount');
  if (!tbody) return;

  try {
    const res = await fetch('/api/sessions');
    if (res.status === 403) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#f87171;padding:16px;">Security management is available on the Host PC only.</td></tr>';
      return;
    }
    const data = await res.json();
    if (!data.success || !data.sessions) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-tertiary);padding:16px;">No session data available.</td></tr>';
      return;
    }

    const sessions = data.sessions;
    const activeSessions = sessions.filter(s => !s.revoked);
    if (countBadge) {
      countBadge.innerText = activeSessions.length;
      countBadge.style.display = activeSessions.length > 0 ? 'inline-block' : 'none';
    }

    if (sessions.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-tertiary);padding:24px;">No remote sessions recorded yet. Your server is safe.</td></tr>';
      return;
    }

    tbody.innerHTML = sessions.map(s => {
      const isRevoked = Boolean(s.revoked);
      const statusHtml = isRevoked
        ? '<span class="session-status-badge revoked"><span style="width:6px;height:6px;border-radius:50%;background:#ef4444;"></span> Revoked</span>'
        : '<span class="session-status-badge active"><span style="width:6px;height:6px;border-radius:50%;background:#22c55e;"></span> Active</span>';

      const actionHtml = isRevoked
        ? '<span style="font-size:11px;color:var(--text-tertiary);">-</span>'
        : `<button class="btn-danger-outline btn-sm" onclick="revokeSession('${s.id}')">Revoke</button>`;

      const issuedStr = new Date(s.issued_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      const lastActiveStr = new Date(s.last_active * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

      return `<tr>
        <td style="font-weight:500;color:var(--text-primary);">${escapeHtml(s.device || 'Web Client')}</td>
        <td><div class="mono" style="font-size:11px;">${escapeHtml(s.ip || 'Unknown')}</div><div style="font-size:10px;color:var(--text-tertiary);">${escapeHtml(s.location || 'Unknown')}</div></td>
        <td class="mono" style="font-size:11px;">${issuedStr}</td>
        <td class="mono" style="font-size:11px;">${lastActiveStr}</td>
        <td>${statusHtml}</td>
        <td style="text-align:right;">${actionHtml}</td>
      </tr>`;
    }).join('');

  } catch (err) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:#f87171;padding:16px;">Failed to fetch active sessions.</td></tr>';
  }
}

async function revokeSession(sessionId) {
  try {
    const res = await fetch('/api/revoke_session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: sessionId })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || 'Remote session revoked.', 'success');
      refreshSessionsList();
    } else {
      showToast(data.message || 'Failed to revoke session.', 'error');
    }
  } catch (err) {
    showToast('Network error revoking session.', 'error');
  }
}

async function revokeAllSessions() {
  try {
    const res = await fetch('/api/revoke_session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ all: true })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || 'All remote sessions revoked.', 'success');
      refreshSessionsList();
    } else {
      showToast(data.message || 'Failed to revoke sessions.', 'error');
    }
  } catch (err) {
    showToast('Network error revoking sessions.', 'error');
  }
}

async function handleHostPasswordChange(e) {
  e.preventDefault();
  const input = document.getElementById('newMasterPasswordInput');
  const chk = document.getElementById('chkRevokeOnPassChange');
  const btn = document.getElementById('btnChangePasswordSubmit');
  const newPwd = (input.value || '').trim();

  if (newPwd.length < 6) {
    showToast('Password must be at least 6 characters long.', 'error');
    return;
  }

  btn.disabled = true;
  btn.innerText = 'Saving...';

  try {
    const res = await fetch('/api/change_password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        new_password: newPwd,
        revoke_sessions: Boolean(chk.checked)
      })
    });
    const data = await res.json();
    if (data.success) {
      showToast(data.message || 'Passcode updated successfully.', 'success');
      input.value = '';
      loadHostSecurityInfo();
      refreshSessionsList();
    } else {
      showToast(data.message || 'Failed to update passcode.', 'error');
    }
  } catch (err) {
    showToast('Network error updating passcode.', 'error');
  } finally {
    btn.disabled = false;
    btn.innerText = 'Save Passcode';
  }
}

/* Initial Boot */
applyClientRole();
checkAuthStatus();
loadDirectory('recv', '', true);
</script>

</body>
</html>"""


def render_login_page() -> str:
    """Renders the standalone glassmorphism Passcode Gate screen for unauthenticated remote visitors."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>HostDrop &mdash; Remote Authentication</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-root: #090a0c;
      --bg-card: rgba(18, 20, 26, 0.85);
      --border-subtle: rgba(255, 255, 255, 0.08);
      --border-focus: rgba(56, 189, 248, 0.5);
      --text-primary: #f1f5f9;
      --text-secondary: #94a3b8;
      --accent: #38bdf8;
      --accent-glow: rgba(56, 189, 248, 0.25);
      --danger: #ef4444;
      --danger-bg: rgba(239, 68, 68, 0.12);
      --success: #10b981;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100dvh;
      background: radial-gradient(circle at 50% 20%, rgba(56, 189, 248, 0.08) 0%, transparent 60%),
                  radial-gradient(circle at 80% 80%, rgba(99, 102, 241, 0.05) 0%, transparent 50%),
                  var(--bg-root);
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      color: var(--text-primary);
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 1.5rem;
      overflow-x: hidden;
    }
    .gate-container {
      width: 100%;
      max-width: 440px;
      animation: fadeIn 0.4s cubic-bezier(0.16, 1, 0.3, 1);
    }
    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(12px) scale(0.98); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
    .gate-card {
      background: var(--bg-card);
      backdrop-filter: blur(24px);
      -webkit-backdrop-filter: blur(24px);
      border: 1px solid var(--border-subtle);
      border-radius: 20px;
      padding: 2.5rem 2rem;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7),
                  0 0 0 1px rgba(255, 255, 255, 0.04),
                  inset 0 1px 0 rgba(255, 255, 255, 0.1);
      text-align: center;
      position: relative;
    }
    .gate-card.shake {
      animation: shake 0.4s cubic-bezier(0.36, 0.07, 0.19, 0.97) both;
    }
    @keyframes shake {
      10%, 90% { transform: translate3d(-2px, 0, 0); }
      20%, 80% { transform: translate3d(4px, 0, 0); }
      30%, 50%, 70% { transform: translate3d(-6px, 0, 0); }
      40%, 60% { transform: translate3d(6px, 0, 0); }
    }
    .shield-badge {
      width: 64px;
      height: 64px;
      margin: 0 auto 1.5rem;
      background: linear-gradient(135deg, rgba(56, 189, 248, 0.15), rgba(99, 102, 241, 0.15));
      border: 1px solid rgba(56, 189, 248, 0.3);
      border-radius: 18px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--accent);
      box-shadow: 0 0 24px var(--accent-glow);
    }
    .shield-badge svg {
      width: 32px;
      height: 32px;
    }
    h1 {
      font-size: 1.4rem;
      font-weight: 700;
      letter-spacing: -0.025em;
      margin-bottom: 0.5rem;
      color: #fff;
    }
    .subtitle {
      color: var(--text-secondary);
      font-size: 0.88rem;
      line-height: 1.5;
      margin-bottom: 2rem;
    }
    .form-group {
      margin-bottom: 1.25rem;
      text-align: left;
    }
    .label {
      display: block;
      font-size: 0.78rem;
      font-weight: 600;
      color: var(--text-secondary);
      margin-bottom: 0.5rem;
      letter-spacing: 0.02em;
      text-transform: uppercase;
    }
    .input-wrapper {
      position: relative;
      display: flex;
      align-items: center;
    }
    input[type="password"], input[type="text"] {
      width: 100%;
      background: rgba(9, 10, 12, 0.7);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 0.85rem 3rem 0.85rem 1rem;
      color: #fff;
      font-size: 1rem;
      font-family: inherit;
      outline: none;
      transition: all 0.2s ease;
    }
    input:focus {
      border-color: var(--accent);
      box-shadow: 0 0 0 3px var(--accent-glow);
      background: rgba(9, 10, 12, 0.95);
    }
    .toggle-pwd {
      position: absolute;
      right: 0.75rem;
      background: none;
      border: none;
      color: var(--text-secondary);
      cursor: pointer;
      padding: 0.35rem;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: color 0.2s;
    }
    .toggle-pwd:hover { color: var(--text-primary); }
    .toggle-pwd svg { width: 20px; height: 20px; }
    .btn-submit {
      width: 100%;
      background: linear-gradient(135deg, #0284c7, #2563eb);
      color: #fff;
      border: 1px solid rgba(255, 255, 255, 0.15);
      border-radius: 12px;
      padding: 0.9rem;
      font-size: 0.95rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
      box-shadow: 0 4px 14px rgba(37, 99, 235, 0.35);
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
    }
    .btn-submit:hover:not(:disabled) {
      background: linear-gradient(135deg, #0369a1, #1d4ed8);
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(37, 99, 235, 0.45);
    }
    .btn-submit:active:not(:disabled) {
      transform: translateY(0);
    }
    .btn-submit:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
    .error-box {
      background: var(--danger-bg);
      border: 1px solid rgba(239, 68, 68, 0.3);
      border-radius: 10px;
      padding: 0.75rem 1rem;
      margin-bottom: 1.25rem;
      color: #fca5a5;
      font-size: 0.85rem;
      display: none;
      align-items: center;
      gap: 0.5rem;
      text-align: left;
    }
    .error-box svg { width: 18px; height: 18px; flex-shrink: 0; }
    .footer-note {
      margin-top: 2rem;
      font-size: 0.75rem;
      color: var(--text-secondary);
      opacity: 0.7;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.35rem;
    }
    .footer-note svg { width: 14px; height: 14px; }
  </style>
</head>
<body>

<div class="gate-container">
  <div class="gate-card" id="gateCard">
    <div class="shield-badge">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
        <path d="m9 12 2 2 4-4"/>
      </svg>
    </div>
    <h1>HostDrop Remote Access</h1>
    <p class="subtitle">This server is protected with end-to-end access control. Enter the master passcode to connect.</p>

    <div class="error-box" id="gateErrorBox">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" x2="12" y1="8" y2="12"/><line x1="12" x2="12.01" y2="16" y2="16"/></svg>
      <span id="gateErrorText">Invalid master passcode.</span>
    </div>

    <form id="gateForm" onsubmit="submitGatePasscode(event)">
      <div class="form-group">
        <label class="label" for="gatePasscode">Master Passcode</label>
        <div class="input-wrapper">
          <input type="password" id="gatePasscode" placeholder="Enter server passcode" required autocomplete="current-password" autofocus>
          <button type="button" class="toggle-pwd" onclick="togglePasscodeVisibility()" aria-label="Toggle password view">
            <svg id="eyeIcon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/>
              <circle cx="12" cy="12" r="3"/>
            </svg>
          </button>
        </div>
      </div>

      <button type="submit" id="gateBtn" class="btn-submit">
        <span>Unlock Access</span>
        <svg style="width:18px;height:18px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
      </button>
    </form>

    <div class="footer-note">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      <span>Encrypted Tunnel &middot; Rate Limited &middot; Host Audited</span>
    </div>
  </div>
</div>

<script>
function togglePasscodeVisibility() {
  const input = document.getElementById('gatePasscode');
  const icon = document.getElementById('eyeIcon');
  if (input.type === 'password') {
    input.type = 'text';
    icon.innerHTML = '<path d="m9.88 9.88 4.24 4.24m-6.72-2.12a9.66 9.66 0 0 1 4.6-2c5 0 8 4 8 4a15.82 15.82 0 0 1-2.9 3.5m-3.1 1.5a7.3 7.3 0 0 1-2.6.5c-5 0-8-4-8-4a15.88 15.88 0 0 1 4.12-3.88M2 2l20 20"/>';
  } else {
    input.type = 'password';
    icon.innerHTML = '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>';
  }
}

async function submitGatePasscode(e) {
  e.preventDefault();
  const input = document.getElementById('gatePasscode');
  const btn = document.getElementById('gateBtn');
  const errBox = document.getElementById('gateErrorBox');
  const errText = document.getElementById('gateErrorText');
  const card = document.getElementById('gateCard');

  const pwd = input.value.trim();
  if (!pwd) return;

  btn.disabled = true;
  btn.innerHTML = '<span>Verifying...</span>';
  errBox.style.display = 'none';

  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: pwd })
    });

    const data = await res.json().catch(() => ({}));

    if (res.ok && data.success) {
      btn.style.background = 'linear-gradient(135deg, #059669, #10b981)';
      btn.innerHTML = '<span>&#10003; Verified! Loading...</span>';
      setTimeout(() => {
        window.location.reload();
      }, 400);
      return;
    }

    btn.disabled = false;
    btn.innerHTML = '<span>Unlock Access</span><svg style="width:18px;height:18px;" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>';
    card.classList.remove('shake');
    void card.offsetWidth;
    card.classList.add('shake');

    if (res.status === 429) {
      const wait = data.retry_after || 60;
      errText.innerText = 'Too many failed attempts. Locked out for ' + wait + ' seconds.';
    } else {
      errText.innerText = 'Incorrect master passcode. Access attempt logged.';
    }
    errBox.style.display = 'flex';
    input.select();
    input.focus();
  } catch (err) {
    btn.disabled = false;
    btn.innerHTML = '<span>Unlock Access</span>';
    errText.innerText = 'Connection error reaching server.';
    errBox.style.display = 'flex';
  }
}
</script>

</body>
</html>"""


def render_page(port, is_admin=False):
    global GLOBAL_TUNNEL_URL, GLOBAL_TUNNEL_PROVIDER
    ifaces = get_network_interfaces()
    with STATE_LOCK:
        recv_path = UPLOAD_DIR
        share_path = HOST_SHARE

    recv_di = disk_info(recv_path)
    share_di = disk_info(share_path) if share_path else {}
    
    display_recv = recv_path if is_admin else sanitize_path_for_client(recv_path, is_admin=False)
    display_share = share_path if is_admin else sanitize_path_for_client(share_path, is_admin=False)

    recv_path_esc = html.escape(display_recv)
    share_path_esc = html.escape(display_share) if share_path else "No folder selected"

    # Pre-render network cards
    net_items = []

    # Prepend Global Tunnel Card if tunnel active
    if GLOBAL_TUNNEL_URL:
        magic_url = f"{GLOBAL_TUNNEL_URL}/api/auth?key={auth.get_access_key()}" if auth else GLOBAL_TUNNEL_URL
        tunnel_badge = f"""
        <div class="net-item net-card-tunnel" onclick="copyAddress('{GLOBAL_TUNNEL_URL}', this)" title="Click to copy public address" role="button" tabindex="0">
          <div class="net-item-top">
            <div class="net-kind-badge tunnel">
              <svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><line x1="2" x2="22" y1="12" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>
              <span class="net-kind-text">Global Access ({GLOBAL_TUNNEL_PROVIDER.title()})</span>
            </div>
            <button class="icon-btn-micro" onclick="event.stopPropagation(); showQRModal('{magic_url}', 'Global Remote Access (Magic Link)')" title="Scan Magic QR Code" aria-label="QR Code">
              <svg viewBox="0 0 24 24"><rect width="6" height="6" x="3" y="3" rx="1.5"/><rect width="6" height="6" x="15" y="3" rx="1.5"/><rect width="6" height="6" x="3" y="15" rx="1.5"/><path d="M15 15h2v2h-2z"/><path d="M19 15h2v6h-6v-2h4v-4z"/><path d="M7 7h.01"/><path d="M17 7h.01"/><path d="M7 17h.01"/></svg>
            </button>
          </div>
          <div class="net-item-url tabular-nums">{GLOBAL_TUNNEL_URL}</div>
          <div class="net-item-desc">Secure Worldwide Access via Encrypted Tunnel</div>
          <div class="net-copied-badge"><svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg> Copied!</div>
        </div>
        """
        net_items.append(tunnel_badge)

    for i in ifaces:
        url = f"http://{i['ip']}:{port}"
        badge_kind = i['kind']
        label = html.escape(i['label'])
        desc = html.escape(i['desc'])
        
        # SVG icon per adapter kind
        if badge_kind == 'wifi':
            icon_svg = '<svg class="icon" viewBox="0 0 24 24"><path d="M12 20h.01"/><path d="M2 8.82a15 15 0 0 1 20 0"/><path d="M5 12.859a10 10 0 0 1 14 0"/><path d="M8.5 16.429a5 5 0 0 1 7 0"/></svg>'
        elif badge_kind == 'hotspot':
            icon_svg = '<svg class="icon" viewBox="0 0 24 24"><path d="M4.93 4.93a10 10 0 0 0 0 14.14"/><path d="M7.76 7.76a6 6 0 0 0 0 8.48"/><circle cx="12" cy="12" r="2"/><path d="M16.24 7.76a6 6 0 0 1 0 8.48"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>'
        elif badge_kind == 'ethernet-direct':
            icon_svg = '<svg class="icon" viewBox="0 0 24 24"><path d="m19 5 3-3"/><path d="m2 22 3-3"/><path d="M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4z"/><path d="M7.5 13.5 10 11"/><path d="M10.5 16.5 13 14"/><path d="m12 6 6 6 2.3-2.3a2.4 2.4 0 0 0 0-3.4l-2.6-2.6a2.4 2.4 0 0 0-3.4 0z"/></svg>'
        elif badge_kind == 'ethernet':
            icon_svg = '<svg class="icon" viewBox="0 0 24 24"><rect width="18" height="14" x="3" y="5" rx="2"/><path d="M7 15v-4"/><path d="M10 15v-4"/><path d="M14 15v-4"/><path d="M17 15v-4"/></svg>'
        elif badge_kind == 'virtual':
            icon_svg = '<svg class="icon" viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/><path d="M3 15h18"/><path d="M9 3v18"/><path d="M15 3v18"/></svg>'
        elif badge_kind == 'bluetooth':
            icon_svg = '<svg class="icon" viewBox="0 0 24 24"><path d="m7 7 10 10-5 5V2l5 5L7 17"/></svg>'
        else:
            icon_svg = '<svg class="icon" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/></svg>'

        net_items.append(f"""
        <div class="net-item net-card-{badge_kind}" onclick="copyAddress('{url}', this)" title="Click to copy address" role="button" tabindex="0">
          <div class="net-item-top">
            <div class="net-kind-badge {badge_kind}">
              {icon_svg}
              <span class="net-kind-text">{label}</span>
            </div>
            <button class="icon-btn-micro" onclick="event.stopPropagation(); showQRModal('{url}', '{label}')" title="Scan QR Code" aria-label="QR Code">
              <svg viewBox="0 0 24 24"><rect width="6" height="6" x="3" y="3" rx="1.5"/><rect width="6" height="6" x="15" y="3" rx="1.5"/><rect width="6" height="6" x="3" y="15" rx="1.5"/><path d="M15 15h2v2h-2z"/><path d="M19 15h2v6h-6v-2h4v-4z"/><path d="M7 7h.01"/><path d="M17 7h.01"/><path d="M7 17h.01"/></svg>
            </button>
          </div>
          <div class="net-item-url tabular-nums">{url}</div>
          <div class="net-item-desc">{desc}</div>
          <div class="net-copied-badge"><svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg> Copied!</div>
        </div>
        """)

    net_items_html = "\n".join(net_items)

    ifaces_json = json.dumps([
        {
            "ip": i["ip"],
            "url": f"http://{i['ip']}:{port}",
            "kind": i["kind"],
            "label": i["label"],
            "desc": i["desc"]
        }
        for i in ifaces
    ])

    host_os = "termux" if is_termux() else ("macos" if sys.platform == "darwin" else ("windows" if sys.platform == "win32" else "linux"))

    out = HTML_TEMPLATE.replace("__PORT__", str(port))
    out = out.replace("__HOST_OS__", host_os)
    out = out.replace("__NET_INTERFACES_JSON__", ifaces_json)
    out = out.replace("__NET_ITEMS__", net_items_html)
    out = out.replace("__RECV_PATH__", recv_path_esc)
    out = out.replace("__RECV_FREE_GB__", str(recv_di.get('free_gb', '?')))
    out = out.replace("__RECV_USED_PCT__", str(recv_di.get('used_pct', 0)))
    out = out.replace("__RECV_WARN_CLASS__", "warn" if recv_di.get('used_pct', 0) > 70 else "")
    out = out.replace("__SHARE_PATH__", share_path_esc)
    out = out.replace("__SHARE_METER_DISPLAY__", "flex" if share_path else "none")
    out = out.replace("__SHARE_FREE_GB__", str(share_di.get('free_gb', '?')))
    out = out.replace("__SHARE_USED_PCT__", str(share_di.get('used_pct', 0)))
    out = out.replace("__SHARE_WARN_CLASS__", "warn" if share_di.get('used_pct', 0) > 70 else "")

    return out


# ═══════════════════════════════════════════════════════════════════════════════
#  HTTP REQUEST ROUTER (Zero-Dependency Python Backend)
# ═══════════════════════════════════════════════════════════════════════════════
class HostDropHandler(BaseHTTPRequestHandler):

    def send_security_headers(self):
        """Inject strict defense-in-depth HTTP security headers."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")

    def send_json(self, data, status=200):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_security_headers()
        self.end_headers()
        self.wfile.write(payload)

    def is_authenticated(self) -> bool:
        """Verify caller session against auth module."""
        global REQUIRE_AUTH, REQUIRE_AUTH_ON_LAN
        if not REQUIRE_AUTH:
            return True
        if self.is_physical_localhost() and not REQUIRE_AUTH_ON_LAN:
            return True
        if auth:
            return auth.is_authenticated(self)
        return True

    def check_csrf(self) -> bool:
        """Validate Sec-Fetch-Site to neutralize CSRF attacks."""
        sec_fetch = self.headers.get("Sec-Fetch-Site", "")
        if sec_fetch == "cross-site":
            return False
        return True

    def is_physical_localhost(self) -> bool:
        """Verify whether request originated physically from localhost (not via tunnel)."""
        if auth and hasattr(auth, "is_physical_localhost"):
            return auth.is_physical_localhost(self)

        # Fallback if auth module is unavailable (matching fail-closed logic)
        if not hasattr(self, "client_address") or not self.client_address:
            return False
        try:
            peer = str(self.client_address[0]).strip()
        except (IndexError, TypeError, Exception):
            return False

        is_loopback = (
            peer in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")
            or peer.startswith("127.")
            or peer.startswith("::ffff:127.")
        )
        if not is_loopback:
            return False

        headers = getattr(self, "headers", None)
        if headers:
            tunnel_headers = frozenset({"cf-connecting-ip", "x-forwarded-for", "x-real-ip", "forwarded", "true-client-ip"})
            if hasattr(headers, "items"):
                for k, v in headers.items():
                    if str(k).strip().lower() in tunnel_headers and bool(str(v).strip()):
                        return False
            for h in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP", "Forwarded", "True-Client-IP"):
                val = getattr(headers, "get", lambda x, d=None: None)(h)
                if val and bool(str(val).strip()):
                    return False

        return True

    def do_GET(self):
        global UPLOAD_DIR, HOST_SHARE
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # ── Intercept Authentication & Session Management Routes ──
        if auth and path in ("/api/auth", "/api/login", "/api/logout", "/api/check_auth", "/api/sessions", "/api/revoke_session", "/api/change_password", "/api/host_security_info"):
            if auth.handle_auth_routes(self, path, qs):
                return

        # ── Tunnel Status API ──
        if path == "/api/tunnel":
            self.send_json({
                "enabled": bool(GLOBAL_TUNNEL_URL),
                "url": GLOBAL_TUNNEL_URL,
                "provider": GLOBAL_TUNNEL_PROVIDER
            })
            return

        # ── Main Web Dashboard / Zero-Trust Passcode Gate ──
        if path in ("/", "/index.html"):
            is_host = self.is_physical_localhost()
            is_auth = self.is_authenticated()

            # Strict Zero-Trust Gating:
            # If request is from remote tunnel or external client and NOT authenticated,
            # DO NOT RENDER THE DASHBOARD! Serve the standalone Passcode Gate screen.
            if not is_host and not is_auth:
                content = render_login_page().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_security_headers()
                self.end_headers()
                self.wfile.write(content)
                return

            # Otherwise, serve the full dashboard
            content = render_page(SERVER_PORT, is_admin=is_host).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_security_headers()
            self.end_headers()
            self.wfile.write(content)
            return

        # ── Favicon ──
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return

        # ── Active Network Interfaces ──
        if path == "/api/interfaces":
            self.send_json({
                "interfaces": get_network_interfaces(),
                "port": SERVER_PORT
            })
            return

        # ── Dynamic QR Code Generation ──
        if path == "/api/qr":
            url = qs.get("url", [""])[0] or f"http://127.0.0.1:{SERVER_PORT}"
            if qrcode:
                qr = qrcode.QRCode(box_size=8, border=2)
                qr.add_data(url)
                qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO()
                img.save(buf, "PNG")
                raw = buf.getvalue()
                ct = "image/png"
            else:
                raw = b"<svg xmlns='http://www.w3.org/2000/svg' width='100' height='100'><text y='50'>No QR Lib</text></svg>"
                ct = "image/svg+xml"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return

        # ── In-Browser Host Filesystem Browser ──
        if path == "/api/browse_host":
            if not self.is_authenticated():
                self.send_json({
                    "error": "unauthorized",
                    "login_required": True,
                    "message": "Authentication required to browse host drives."
                }, status=401)
                return
            req_path = qs.get("path", [""])[0]
            data = browse_host_directory(req_path)
            self.send_json(data)
            return

        # ── Directory Listing for Dual Tabs ──
        if path == "/api/list":
            if not self.is_authenticated():
                self.send_json({
                    "error": "unauthorized",
                    "login_required": True,
                    "items": [],
                    "message": "Authentication required to view files."
                }, status=401)
                return
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            with STATE_LOCK:
                base = HOST_SHARE if tab == "share" else UPLOAD_DIR
            is_admin = self.is_physical_localhost()
            if not base or not os.path.exists(base):
                self.send_json({
                    "items": [],
                    "path": rel,
                    "disk": disk_info(base) if base else {},
                    "base": base if is_admin else sanitize_path_for_client(base, is_admin=False)
                })
                return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_json({
                    "items": [],
                    "path": rel,
                    "disk": disk_info(base),
                    "base": base if is_admin else sanitize_path_for_client(base, is_admin=False)
                })
                return

            items = []
            try:
                with os.scandir(target) as it:
                    for entry in it:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                count = 0
                                try:
                                    count = len(os.listdir(entry.path))
                                except Exception:
                                    pass
                                items.append({"name": entry.name, "isDir": True, "count": count})
                            else:
                                items.append({"name": entry.name, "isDir": False, "size": entry.stat().st_size})
                        except (PermissionError, OSError):
                            continue
            except Exception:
                pass

            items.sort(key=lambda x: (not x["isDir"], x["name"].lower()))
            self.send_json({
                "items": items,
                "path": rel,
                "disk": disk_info(target),
                "base": base if is_admin else sanitize_path_for_client(base, is_admin=False)
            })
            return

        # ── Smart Resume Byte Check ──
        if path == "/api/check":
            if not self.is_authenticated():
                self.send_json({"exists": False, "size": 0, "login_required": True}, status=401)
                return
            rel = qs.get("path", [""])[0] or qs.get("filename", [""])[0]
            target_type = qs.get("target", ["recv"])[0]
            with STATE_LOCK:
                base = HOST_SHARE if target_type == "share" else UPLOAD_DIR
            full = safe_path(base, rel)
            if full and os.path.isfile(full):
                self.send_json({"exists": True, "size": os.path.getsize(full)})
            else:
                self.send_json({"exists": False, "size": 0})
            return

        # ── Download Single File ──
        if path == "/download":
            if not self.is_authenticated():
                self.send_response(401); self.send_security_headers(); self.end_headers(); return
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            with STATE_LOCK:
                base = HOST_SHARE if tab == "share" else UPLOAD_DIR
            if not base:
                self.send_response(404); self.end_headers(); return
            fp = safe_path(base, rel)
            if not fp or not os.path.isfile(fp):
                self.send_response(404); self.end_headers(); return

            size = os.path.getsize(fp)
            ct, _ = mimetypes.guess_type(fp)
            self.send_response(200)
            self.send_header("Content-Type", ct or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            raw_fname = os.path.basename(fp)
            safe_ascii_fname = raw_fname.encode("ascii", "ignore").decode("ascii").strip() or "download"
            fname_utf8 = urllib.parse.quote(raw_fname)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_ascii_fname}"; filename*=UTF-8\'\'{fname_utf8}')
            self.send_security_headers()
            self.end_headers()
            try:
                with open(fp, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # ── Stream Folder as ZIP Archive ──
        if path == "/api/zip":
            if not self.is_authenticated():
                self.send_response(401); self.send_security_headers(); self.end_headers(); return
            tab = qs.get("tab", ["recv"])[0] or qs.get("target", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            with STATE_LOCK:
                base = HOST_SHARE if tab == "share" else UPLOAD_DIR
            if not base:
                self.send_response(404); self.send_security_headers(); self.end_headers(); return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_response(404); self.send_security_headers(); self.end_headers(); return

            zip_name = (os.path.basename(target) or "hostdrop_export") + ".zip"

            # Create a temporary file on disk rather than holding gigabytes in RAM (prevents OOM)
            temp_zip = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
            temp_zip_path = temp_zip.name
            temp_zip.close()

            try:
                file_count = 0
                total_bytes = 0
                with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, dirs, files in os.walk(target):
                        # Enforce recursion depth limit
                        rel_root = os.path.relpath(root, target)
                        depth = len(rel_root.replace("\\", "/").split("/")) if rel_root != "." else 0
                        if depth > MAX_ZIP_DEPTH:
                            dirs.clear()
                            continue

                        for d in dirs:
                            dir_full = os.path.join(root, d)
                            arc_d = os.path.relpath(dir_full, target).replace("\\", "/") + "/"
                            zf.writestr(arc_d, "")

                        for f in files:
                            if file_count >= MAX_ZIP_FILES:
                                break
                            full_f = os.path.join(root, f)
                            arc_f = os.path.relpath(full_f, target).replace("\\", "/")
                            try:
                                sz = os.path.getsize(full_f)
                                if total_bytes + sz > MAX_ZIP_SIZE:
                                    break
                                zf.write(full_f, arc_f)
                                file_count += 1
                                total_bytes += sz
                            except (PermissionError, OSError):
                                pass

                zip_size = os.path.getsize(temp_zip_path)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Length", str(zip_size))
                safe_ascii_zip = zip_name.encode("ascii", "ignore").decode("ascii").strip() or "archive.zip"
                fname_esc = urllib.parse.quote(zip_name)
                self.send_header("Content-Disposition", f'attachment; filename="{safe_ascii_zip}"; filename*=UTF-8\'\'{fname_esc}')
                self.send_security_headers()
                self.end_headers()

                # Stream from disk file in 64KB chunks (constant O(1) memory)
                with open(temp_zip_path, "rb") as f:
                    while chunk := f.read(64 * 1024):
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    if os.path.exists(temp_zip_path):
                        os.remove(temp_zip_path)
                except Exception:
                    pass
            return

        # ── Trigger Native OS Folder Picker ──
        if path == "/api/pick_folder":
            if not self.is_authenticated():
                self.send_json({"error": "unauthorized", "login_required": True, "message": "Authentication required to pick folders."}, status=401)
                return
            if not self.is_physical_localhost():
                self.send_json({"success": False, "error": "forbidden", "message": "OS GUI folder picker is disabled over remote tunnels for security."}, status=403)
                return
            target_type = qs.get("target", ["share"])[0] or qs.get("type", ["share"])[0]
            chosen, err = pick_folder_native()
            if chosen:
                with STATE_LOCK:
                    if target_type == "recv":
                        UPLOAD_DIR = os.path.abspath(chosen)
                    else:
                        HOST_SHARE = os.path.abspath(chosen)
                self.send_json({"success": True, "status": "ok", "path": chosen, "target": target_type})
            else:
                self.send_json({"success": False, "status": "cancelled", "error": err or "cancelled"})
            return

        # ── Open Folder in Host OS Explorer ──
        if path == "/api/open_folder":
            if not self.is_authenticated():
                self.send_json({"error": "unauthorized", "login_required": True, "message": "Authentication required to open folders."}, status=401)
                return
            if not self.is_physical_localhost():
                self.send_json({"success": False, "error": "forbidden", "message": "Opening native file manager is only available directly on the Host PC."}, status=403)
                return
            target_type = qs.get("type", ["recv"])[0] or qs.get("target", ["recv"])[0]
            with STATE_LOCK:
                target_dir = HOST_SHARE if target_type == "share" else UPLOAD_DIR
            client_ip = self.client_address[0]
            res = open_in_os_explorer(target_dir, client_ip)
            self.send_json(res)
            return

        # ── Disk Space Metrics ──
        if path == "/api/disk":
            req_path = qs.get("path", [""])[0] or UPLOAD_DIR
            self.send_json(disk_info(req_path))
            return

        # ── Real-Time Path Validation ──
        if path == "/api/validate_path":
            req_path = qs.get("path", [""])[0]
            exists = os.path.exists(req_path) and os.path.isdir(req_path)
            writable = os.access(req_path, os.W_OK) if exists else False
            di = disk_info(req_path) if exists else {}
            self.send_json({
                "valid": exists and writable,
                "exists": exists,
                "writable": writable,
                "disk": di
            })
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        global UPLOAD_DIR, HOST_SHARE
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # ── Intercept Authentication & Session Management Routes ──
        if auth and path in ("/api/auth", "/api/login", "/api/logout", "/api/sessions", "/api/revoke_session", "/api/change_password"):
            content_len = safe_int(self.headers.get("Content-Length", 0))
            body_bytes = self.rfile.read(content_len) if content_len > 0 else b""
            body_data = {}
            if body_bytes:
                try:
                    body_data = json.loads(body_bytes.decode("utf-8"))
                except Exception:
                    pass
            if auth.handle_auth_routes(self, path, qs, body_data=body_data):
                return

        # ── CSRF Protection ──
        if not self.check_csrf():
            self.send_json({"error": "forbidden", "message": "Cross-site request blocked."}, status=403)
            return

        # ── Resumable Chunked Upload Protocol ──
        if path == "/api/upload":
            if not self.is_authenticated():
                self.send_json({"error": "unauthorized", "login_required": True, "message": "Authentication required to upload files."}, status=401)
                return
            rel = qs.get("path", ["upload"])[0]
            offset = safe_int(qs.get("offset", [0]))
            target_type = qs.get("target", ["recv"])[0]
            with STATE_LOCK:
                base = HOST_SHARE if target_type == "share" else UPLOAD_DIR
            full = safe_path(base, rel)
            if not full:
                self.send_response(403); self.send_security_headers(); self.end_headers(); return

            try:
                content_len = safe_int(self.headers.get("Content-Length", 0))
                if content_len > MAX_UPLOAD_SIZE:
                    self.send_json({"success": False, "status": "error", "error": f"File size exceeds maximum allowed limit ({MAX_UPLOAD_SIZE // (1024**3)} GB)."}, status=413)
                    return

                # Pre-flight check free disk space (content_len + MIN_FREE_DISK_BUFFER)
                target_dir = os.path.dirname(full)
                os.makedirs(target_dir, exist_ok=True)
                try:
                    free_disk = shutil.disk_usage(target_dir).free
                    if free_disk < content_len + MIN_FREE_DISK_BUFFER:
                        self.send_json({"success": False, "status": "error", "error": "Insufficient host storage space (500 MB buffer required)."}, status=507)
                        return
                except Exception:
                    pass

                # Offset bounds validation
                if offset > 0 and os.path.exists(full):
                    current_size = os.path.getsize(full)
                    if offset > current_size:
                        self.send_json({"success": False, "status": "error", "error": "Invalid resume offset beyond existing file length."}, status=400)
                        return
                elif offset > 0 and not os.path.exists(full):
                    self.send_json({"success": False, "status": "error", "error": "Cannot resume non-existent file."}, status=400)
                    return

                bytes_written = 0
                chunk_size = 64 * 1024  # 64 KB streaming chunk

                if offset == 0:
                    with open(full, "wb") as f:
                        while bytes_written < content_len:
                            to_read = min(chunk_size, content_len - bytes_written)
                            chunk = self.rfile.read(to_read)
                            if not chunk:
                                break
                            f.write(chunk)
                            bytes_written += len(chunk)
                else:
                    # Resuming partial upload with atomic seek and truncate
                    with open(full, "r+b") as f:
                        f.seek(offset)
                        f.truncate(offset)
                        while bytes_written < content_len:
                            to_read = min(chunk_size, content_len - bytes_written)
                            chunk = self.rfile.read(to_read)
                            if not chunk:
                                break
                            f.write(chunk)
                            bytes_written += len(chunk)

                # Verify full payload received without premature connection drop
                if content_len > 0 and bytes_written < content_len:
                    self.send_json({
                        "success": False,
                        "status": "error",
                        "error": "Upload truncated or connection closed prematurely.",
                        "received": bytes_written,
                        "expected": content_len
                    }, status=400)
                    return

                self.send_json({
                    "success": True,
                    "status": "ok",
                    "saved": rel,
                    "bytes": bytes_written,
                    "received": bytes_written,
                    "completed": True
                })
            except Exception as e:
                self.send_json({"success": False, "status": "error", "error": str(e)}, status=400)
            return

        # ── Set Folder Path ──
        if path in ("/api/set_path", "/api/set_folder"):
            if not self.is_authenticated():
                self.send_json({"error": "unauthorized", "login_required": True, "message": "Authentication required to change host storage paths."}, status=401)
                return
            content_len = safe_int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
                target_type = data.get("target") or data.get("type") or "recv"
                raw_path = data.get("path", "").strip().strip("'\"")
                if not raw_path:
                    self.send_json({"success": False, "status": "error", "error": "Path cannot be empty."}, status=400)
                    return
                target_path = os.path.abspath(raw_path)

                if not os.path.exists(target_path):
                    os.makedirs(target_path, exist_ok=True)

                with STATE_LOCK:
                    if target_type == "share":
                        HOST_SHARE = target_path
                    else:
                        UPLOAD_DIR = target_path

                di = disk_info(target_path)
                self.send_json({
                    "success": True,
                    "status": "ok",
                    "path": target_path,
                    "target": target_type,
                    "free_gb": di.get("free_gb"),
                    "disk": di
                })
            except Exception as e:
                self.send_json({"success": False, "status": "error", "error": str(e)}, status=400)
            return

        # ── Create New Directory on Host ──
        if path == "/api/create_folder":
            if not self.is_authenticated():
                self.send_json({"error": "unauthorized", "login_required": True, "message": "Authentication required to create folders."}, status=401)
                return
            content_len = safe_int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
                parent = data.get("parent", "").strip()
                name = data.get("name", "").strip()
                raw_path = data.get("path", "").strip()
                if not parent or not name:
                    if not raw_path:
                        self.send_json({"success": False, "status": "error", "error": "Folder path cannot be empty."}, status=400)
                        return
                    full_p = os.path.abspath(raw_path)
                else:
                    full_p = os.path.abspath(os.path.join(parent, name))

                os.makedirs(full_p, exist_ok=True)
                self.send_json({"success": True, "status": "ok", "path": full_p})
            except Exception as e:
                self.send_json({"success": False, "status": "error", "error": str(e)}, status=400)
            return

        # ── Trigger Native Picker (POST) ──
        if path == "/api/pick_folder":
            if not self.is_authenticated():
                self.send_json({"error": "unauthorized", "login_required": True, "message": "Authentication required to pick folders."}, status=401)
                return
            if not self.is_physical_localhost():
                self.send_json({"success": False, "error": "forbidden", "message": "OS GUI folder picker is disabled over remote tunnels for security."}, status=403)
                return
            chosen, err = pick_folder_native()
            if chosen:
                self.send_json({"success": True, "status": "ok", "path": chosen})
            else:
                self.send_json({"success": False, "status": "cancelled", "error": err or "cancelled"})
            return

        # ── Open in OS (POST) ──
        if path == "/api/open_folder":
            if not self.is_authenticated():
                self.send_json({"error": "unauthorized", "login_required": True, "message": "Authentication required to open folders."}, status=401)
                return
            if not self.is_physical_localhost():
                self.send_json({"success": False, "error": "forbidden", "message": "Opening native file manager is only available directly on the Host PC."}, status=403)
                return
            content_len = safe_int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len) if content_len > 0 else b"{}"
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                data = {}
            target_type = data.get("target") or data.get("type") or qs.get("type", ["recv"])[0]
            with STATE_LOCK:
                target_dir = HOST_SHARE if target_type == "share" else UPLOAD_DIR
            client_ip = self.client_address[0]
            res = open_in_os_explorer(target_dir, client_ip)
            self.send_json(res)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, *_):
        # Suppress noisy standard HTTP access logs
        pass


def create_server(host: str = "0.0.0.0", port: int = 8080) -> ThreadingHTTPServer:
    """Instantiate and configure the multi-threaded HTTP server."""
    if isinstance(host, int) and isinstance(port, str):
        host, port = port, host
    elif isinstance(host, int):
        port = host
        host = "0.0.0.0"
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((host, port), HostDropHandler)
    server.daemon_threads = True
    return server


# ═══════════════════════════════════════════════════════════════════════════════
#  APPLICATION BOOTSTRAP & SERVER LAUNCHER
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global UPLOAD_DIR, SERVER_PORT, GLOBAL_TUNNEL_URL, GLOBAL_TUNNEL_PROVIDER

    if is_termux():
        if os.path.exists("/sdcard") and os.access("/sdcard", os.W_OK):
            default_dir = "/sdcard/HostDrop"
        else:
            termux_shared = os.path.join(os.path.expanduser("~"), "storage", "shared", "HostDrop")
            if os.path.exists(os.path.dirname(termux_shared)) and os.access(os.path.dirname(termux_shared), os.W_OK):
                default_dir = termux_shared
            else:
                default_dir = os.path.join(os.path.expanduser("~"), "HostDrop")
    elif sys.platform == "win32":
        default_dir = r"D:\HostDrop" if os.path.exists("D:\\") else os.path.join(
            os.path.expanduser("~"), "Downloads", "HostDrop"
        )
    else:
        default_dir = os.path.join(os.path.expanduser("~"), "HostDrop")

    chosen = default_dir
    tunnel_prov = os.environ.get("TUNNEL_PROVIDER", "auto").strip().lower()

    i = 1
    while i < len(sys.argv):
        arg = sys.argv[i].strip()
        if arg in ("--help", "-h"):
            print("HostDrop - High-Speed Cross-Platform File Transfer Hub")
            print("Usage: hostdrop [OPTIONS] [DIRECTORY]")
            print("\nOptions:")
            print("  -p, --port PORT          Port to bind the server to (default: 8080)")
            print("  -t, --tunnel PROVIDER    Tunnel provider: auto, cloudflare, pinggy, none (default: auto)")
            print("  -h, --help               Show this help message and exit")
            print("  DIRECTORY                Optional inbox folder to receive files")
            sys.exit(0)
        elif arg in ("--tunnel", "-t") and i + 1 < len(sys.argv):
            tunnel_prov = sys.argv[i + 1].strip().lower()
            i += 2
        elif arg.startswith("--tunnel="):
            tunnel_prov = arg.split("=", 1)[1].strip().lower()
            i += 1
        elif arg in ("--port", "-p") and i + 1 < len(sys.argv):
            try:
                SERVER_PORT = int(sys.argv[i + 1].strip())
            except ValueError:
                pass
            i += 2
        elif arg.startswith("--port="):
            try:
                SERVER_PORT = int(arg.split("=", 1)[1].strip())
            except ValueError:
                pass
            i += 1
        elif not arg.startswith("-"):
            chosen = arg.strip("'\"")
            i += 1
        else:
            i += 1

    UPLOAD_DIR = os.path.abspath(chosen)
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except OSError as e:
        print(f"[-] Warning: Could not create chosen directory '{UPLOAD_DIR}': {e}")
        fallback = os.path.expanduser("~/HostDrop")
        try:
            os.makedirs(fallback, exist_ok=True)
            UPLOAD_DIR = fallback
            print(f"[+] Falling back to user directory: {UPLOAD_DIR}")
        except OSError:
            UPLOAD_DIR = os.getcwd()
            print(f"[+] Falling back to current directory: {UPLOAD_DIR}")

    # Initialize Auth Configuration
    if auth:
        auth.init_config()

    # Launch Global Tunnel if configured
    if tunnel_prov != "none":
        print(f"[*] Initializing Global Remote Access tunnel (provider={tunnel_prov})...")
        t = threading.Thread(target=lambda: TunnelManager.start(SERVER_PORT, provider=tunnel_prov), daemon=True)
        t.start()

    ifaces = get_network_interfaces()
    primary = next((f"http://{i['ip']}:{SERVER_PORT}" for i in ifaces
                    if i["kind"] in ("wifi", "ethernet", "ethernet-direct", "hotspot")), None)

    print("=" * 68)
    print("  HostDrop -- High-Speed Cross-Device Transfer Hub")
    print("=" * 68)
    print(f"  Inbox Folder (Save Target) : {UPLOAD_DIR}")
    for i in ifaces:
        print(f"  {i['label']:<24} -> http://{i['ip']}:{SERVER_PORT}")
    if auth:
        print("  " + "-" * 64)
        print("  [SECURITY CREDENTIALS]")
        print(f"  Master App Passcode        : {auth.get_master_password()}")
        print(f"  Persistent Bookmark Key    : {auth.get_access_key()}")
        print(f"  Direct Localhost Access    : http://127.0.0.1:{SERVER_PORT}")
        print(f"  Security Recommendation    : Tip: We recommend setting your own personal passcode,")
        print(f"                               though your auto-generated code is active and secure.")
    print("=" * 68)

    if qrcode and primary:
        try:
            qr = qrcode.QRCode()
            qr.add_data(primary)
            qr.print_ascii(invert=True)
        except Exception:
            pass

    try:
        server = create_server("0.0.0.0", SERVER_PORT)
    except OSError as e:
        print(f"\n[ERROR] Failed to start server on port {SERVER_PORT}: {e}")
        print(f"        Port {SERVER_PORT} is likely already occupied by another process.")
        print(f"        To run on a different port, use: hostdrop --port <number> (e.g. hostdrop --port 8081)")
        sys.exit(1)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nHostDrop server stopped cleanly.")


if __name__ == "__main__":
    main()
