import os
import sys
import io
import json
import socket
import shutil
import zipfile
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

# ── Optional Helper Libraries ───────────────────────────────────────────────────
try:
    import psutil
except ImportError:
    psutil = None

try:
    import qrcode
except ImportError:
    qrcode = None

# ── Global State & Thread Synchronization ───────────────────────────────────────
STATE_LOCK = threading.Lock()
UPLOAD_DIR = ""   # Where incoming files sent to the PC are stored (Inbox tab)
HOST_SHARE = ""   # Folder shared by PC for others to browse/download (Library tab)
SERVER_PORT = 8080


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
                        elif "wi-fi" in lo or "wireless" in lo or "wlan" in lo:
                            kind, label, desc, pri = "wifi", "Wi-Fi Network", "Home/Office Wireless LAN", 1
                        elif ip.startswith("192.168.137.") or "hotspot" in lo or "host" in lo:
                            kind, label, desc, pri = "hotspot", "Mobile Hotspot", "Tethered Hotspot Devices", 2
                        elif "ethernet" in lo or "eth" in lo or "lan" in lo:
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


def safe_path(base_dir, rel):
    """
    Prevent directory traversal attacks by ensuring the resolved path
    is strictly contained inside base_dir.
    """
    if not base_dir:
        return None
    rel = (rel or "").replace("\\", "/").strip("/")
    safe = os.path.normpath(rel).lstrip("/\\")
    full = os.path.abspath(os.path.join(base_dir, safe))
    base_abs = os.path.abspath(base_dir)
    try:
        if os.path.commonpath([base_abs, full]) != base_abs:
            return None
    except ValueError:
        return None
    return full


def get_host_drives():
    """
    Enumerate all logical storage drives on the host PC with capacity and free space.
    On Windows: queries C:\\, D:\\, etc. via kernel32 GetLogicalDrives.
    On Unix/macOS: queries root (/), /Volumes, /media, /mnt, and user home.
    """
    drives = []
    if sys.platform == "win32":
        try:
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for letter in string.ascii_uppercase:
                if bitmask & 1:
                    drive_path = f"{letter}:\\"
                    try:
                        u = shutil.disk_usage(drive_path)
                        used_pct_val = round((u.used / u.total) * 100, 1) if u.total > 0 else 0.0
                        drives.append({
                            "path": drive_path,
                            "name": f"Local Disk ({letter}:)",
                            "letter": letter,
                            "label": f"OS ({letter}:)" if letter.upper() == "C" else f"Data ({letter}:)" if letter.upper() == "D" else f"Local Disk ({letter}:)",
                            "free_gb": f"{u.free / (1024**3):.1f}",
                            "total_gb": f"{u.total / (1024**3):.1f}",
                            "used_gb": f"{u.used / (1024**3):.1f}",
                            "used_pct": int(u.used * 100 // u.total) if u.total > 0 else 0,
                            "used_percent": used_pct_val,
                            "is_system": letter.upper() == "C"
                        })
                    except Exception:
                        drives.append({
                            "path": drive_path,
                            "name": f"Drive ({letter}:)",
                            "letter": letter,
                            "label": f"Drive ({letter}:)",
                            "free_gb": "?",
                            "total_gb": "?",
                            "used_gb": "?",
                            "used_pct": 0,
                            "used_percent": 0.0,
                            "is_system": letter.upper() == "C"
                        })
                bitmask >>= 1
        except Exception:
            for letter in string.ascii_uppercase:
                drive_path = f"{letter}:\\"
                if os.path.exists(drive_path):
                    try:
                        u = shutil.disk_usage(drive_path)
                        used_pct_val = round((u.used / u.total) * 100, 1) if u.total > 0 else 0.0
                        drives.append({
                            "path": drive_path,
                            "name": f"Drive ({letter}:)",
                            "letter": letter,
                            "label": f"Drive ({letter}:)",
                            "free_gb": f"{u.free / (1024**3):.1f}",
                            "total_gb": f"{u.total / (1024**3):.1f}",
                            "used_gb": f"{u.used / (1024**3):.1f}",
                            "used_pct": int(u.used * 100 // u.total) if u.total > 0 else 0,
                            "used_percent": used_pct_val,
                            "is_system": letter.upper() == "C"
                        })
                    except Exception:
                        pass
    else:
        root_candidates = ["/", os.path.expanduser("~")]
        for p in ["/Volumes", "/media", "/mnt"]:
            if os.path.exists(p):
                try:
                    for sub in os.listdir(p):
                        sp = os.path.join(p, sub)
                        if os.path.isdir(sp):
                            root_candidates.append(sp)
                except Exception:
                    pass
        for p in root_candidates:
            if os.path.exists(p):
                try:
                    u = shutil.disk_usage(p)
                    used_pct_val = round((u.used / u.total) * 100, 1) if u.total > 0 else 0.0
                    drives.append({
                        "path": p,
                        "name": os.path.basename(p) or "Root (/)",
                        "letter": "/",
                        "label": os.path.basename(p) or "Root (/)",
                        "free_gb": f"{u.free / (1024**3):.1f}",
                        "total_gb": f"{u.total / (1024**3):.1f}",
                        "used_gb": f"{u.used / (1024**3):.1f}",
                        "used_pct": int(u.used * 100 // u.total) if u.total > 0 else 0,
                        "used_percent": used_pct_val,
                        "is_system": p == "/"
                    })
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
    if parent == target:
        parent = ""  # Reached top-level drive root

    subdirs = []
    try:
        with os.scandir(target) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        name = entry.name
                        # Filter system-protected / reserved Windows folders
                        if name.startswith("$") or name in (
                            "System Volume Information", "Recovery", "$WinREAgent",
                            "Config.Msi", "MSOCache", "hiberfil.sys", "pagefile.sys"
                        ):
                            continue
                        subdirs.append({
                            "name": name,
                            "path": entry.path,
                            "isDir": True
                        })
                except (PermissionError, OSError):
                    continue
    except PermissionError:
        return {
            "is_root": False,
            "error": "Permission denied accessing folder",
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


def pick_folder_powershell(timeout_sec=120):
    """
    Launch native Windows FolderBrowserDialog via PowerShell STA mode with topmost form focus.
    Guarantees non-blocking execution in server threads and proper foreground window activation.
    """
    if sys.platform != "win32":
        return None, "unsupported_platform"

    ps_script = (
        "[System.Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms') | Out-Null;"
        "$dialog = New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$dialog.Description = 'Select folder for TurboShare File Transfer Hub';"
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
    except Exception as e:
        # Fallback to Tkinter if PowerShell fails
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.wm_attributes("-topmost", 1)
            path = filedialog.askdirectory(title="Select folder for TurboShare")
            root.destroy()
            if path and os.path.isdir(path):
                return path, None
            return None, "cancelled"
        except Exception:
            return None, str(e)


def open_in_os_explorer(target_dir, client_ip):
    """
    Spawn the host OS file explorer (explorer.exe / open / xdg-open) in the foreground.
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
        if sys.platform == "win32":
            subprocess.Popen(["explorer.exe", norm])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", norm])
        else:
            subprocess.Popen(["xdg-open", norm])

        msg = "Opened folder in Windows Explorer" if is_local else "Folder opened on Host PC display (viewing in browser)"
        return {
            "success": True,
            "status": "ok",
            "is_local": is_local,
            "is_local_client": is_local,
            "path": norm,
            "message": msg
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
<title>TurboShare &mdash; High-Speed LAN Transfer Hub</title>
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
        TurboShare
        <span class="status-pill host-badge" id="hostStatusPill">
          <span class="status-dot"></span>
          Host Computer &bull; :__PORT__
        </span>
        <span class="status-pill guest-badge" id="guestStatusPill" style="display: none;">
          <span class="status-dot" style="background: #38bdf8; box-shadow: 0 0 8px #38bdf8;"></span>
          Connected to PC &bull; :__PORT__
        </span>
      </div>
    </div>

    <div class="header-actions">
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
        <button class="btn btn-sm" onclick="triggerNativePicker('recv')" title="Launch Windows Folder Dialog on Host">
          <svg class="icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
          Windows Dialog
        </button>
        <button class="btn btn-sm btn-ghost" onclick="openInExplorer('recv')" title="Open folder in Windows Explorer">
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
        <button class="btn btn-sm" onclick="triggerNativePicker('share')" title="Launch Windows Folder Dialog on Host">
          <svg class="icon" viewBox="0 0 24 24"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
          Windows Dialog
        </button>
        <button class="btn btn-sm btn-ghost" onclick="openInExplorer('share')" title="Open folder in Windows Explorer">
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
      <button class="btn btn-ghost btn-sm" onclick="triggerNativePicker(browserModalTarget)" title="Open standard Windows Explorer folder picker on Host">
        <svg class="icon" viewBox="0 0 24 24"><rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/><rect x="13" y="13" width="8" height="8" rx="1"/></svg>
        <span>Windows Dialog</span>
      </button>
      <button class="btn btn-ghost btn-sm" onclick="closeModal('hostBrowserModal')">Cancel</button>
      <button class="btn btn-primary btn-sm" id="modalConfirmBtn" onclick="confirmModalFolderSelection()">Select This Folder</button>
    </div>
  </div>
</div>

<!-- QR Connect Modal -->
<div class="modal-overlay" id="qrModal">
  <div class="modal-content" style="max-width: 400px; text-align: center;">
    <div class="modal-header">
      <div class="modal-title" id="qrModalTitle">Connect Device</div>
      <button class="icon-btn-micro" onclick="closeModal('qrModal')">
        <svg viewBox="0 0 24 24"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
    </div>
    <div class="modal-body" style="align-items: center; gap: 16px;">
      <div style="background: #ffffff; padding: 12px; border-radius: var(--radius-md); display: inline-block;">
        <img id="qrModalImg" src="" alt="QR Code" style="width: 220px; height: 220px; display: block;">
      </div>
      <div class="mono" id="qrModalUrl" style="font-size: 12px; color: var(--text-primary); word-break: break-all;"></div>
      <div style="font-size: 11px; color: var(--text-tertiary);">Scan with your phone or tablet camera to connect instantly over local Wi-Fi / Hotspot.</div>
    </div>
    <div class="modal-footer" style="justify-content: center;">
      <button class="btn btn-primary btn-sm" onclick="copyAddress(document.getElementById('qrModalUrl').textContent)">Copy Link</button>
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
        TurboShare Connection Guide &amp; Beginner FAQ
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
            <p><strong>The IP address shown in TurboShare belongs to your PC (the host computer).</strong> When you want to connect your phone, tablet, or another laptop, you type your <strong>PC's IP address</strong> into the phone's web browser (or simply scan the QR code). You do <em>not</em> type your phone's IP address&mdash;you are telling your phone to visit your PC so they can exchange files!</p>
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
            <p>TurboShare automatically detects all active connections. Here is when to use each one:</p>
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
            <p>All files and folders transferred from phones or other computers are saved directly into your <strong>Inbox Folder on PC</strong> (default: <code>D:\TurboShare</code> or <code>C:\TurboShare</code>).</p>
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
            <p><strong>Yes, absolutely!</strong> TurboShare preserves full folder structures recursively:</p>
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
            <p><strong>Zero corruption, zero wasted time.</strong> TurboShare features smart byte-level chunk resumption.</p>
            <p>If your Wi-Fi flickers, your phone goes to sleep, or the browser closes: simply re-select or drop the same file again. TurboShare will check the PC's storage, recognize the exact bytes already received, and <strong>resume from the exact byte where it stopped</strong> without re-uploading from the start.</p>
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
            <p>Your files move strictly across your local Wi-Fi, Hotspot, or Ethernet cable directly from one device to the other. Your data never touches the internet, third-party cloud servers, or external tracking. You can even unplug your internet cable or turn off mobile data, and TurboShare will continue transferring at maximum local speed!</p>
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
              <li><strong>Windows Firewall:</strong> When TurboShare started, Windows may have asked for network permission. Make sure <em>"Private Networks"</em> is allowed. In Windows Defender Firewall &rarr; <em>Allow an app through firewall</em> &rarr; ensure Python / TurboShare is checked for Private networks.</li>
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

/* Host vs Guest Role Adaptive State */
const isHostClient = ['localhost', '127.0.0.1', '::1', ''].includes(window.location.hostname);

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
    const data = await res.json();
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
  const folderName = curPath ? curPath.split('/').pop() : (activeTab === 'recv' ? 'TurboShare_Inbox' : 'TurboShare_Library');
  
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

  for (let i = 0; i < fileArray.length; i++) {
    const file = fileArray[i];
    const relPath = file.webkitRelativePath || file.name;
    document.getElementById('transFileName').textContent = `[${i + 1}/${fileArray.length}] ${file.name}`;
    
    try {
      await uploadFileWithSmartResume(file, relPath);
    } catch (e) {
      showToast(`Upload failed for ${file.name}: ${e.message}`, 'error');
    }
  }

  isTransferring = false;
  setTimeout(() => card.classList.remove('active'), 2500);
  document.getElementById('transStatus').textContent = 'All transfers complete!';
  showToast('Upload complete!', 'success');
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
        reject(new Error(xhr.statusText || 'Upload failed'));
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
    const data = await res.json();
    
    activeModalBrowsePath = data.current_path || '';
    modalParentPath = (data.parent_path !== undefined) ? data.parent_path : '';
    modalIsRoot = !!data.is_root;
    modalCurrentSubdirs = data.subdirs || [];
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
    const isActive = curPathNorm && (curPathNorm.startsWith(drivePathNorm) || (d.letter && curPathNorm.startsWith(d.letter.toUpperCase() + ':')));

    const card = document.createElement('div');
    card.className = 'drive-card' + (isActive ? ' active' : '');
    
    const iconSvg = d.is_system
      ? '<svg class="icon" viewBox="0 0 24 24"><rect width="20" height="8" x="2" y="14" rx="2"/><path d="M6 18h.01"/><path d="M10 18h.01"/><path d="M4 14v-4a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v4"/></svg>'
      : '<svg class="icon" viewBox="0 0 24 24"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>';

    const badgeLabel = isActive ? 'Active' : (d.letter ? d.letter + ':' : 'Disk');

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
  showToast('Opening native Windows folder dialog on Host PC...', 'info');
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
      showToast('Windows dialog notice: ' + data.error, 'info');
    }
  } catch (e) {
    showToast('Failed to trigger Windows dialog: ' + e, 'error');
  }
}

async function openInExplorer(target) {
  try {
    const res = await fetch('/api/open_folder?type=' + target);
    const data = await res.json();
    if (data.success || data.status === 'ok') {
      showToast(data.message || 'Opened folder in Windows Explorer', 'success');
      if (!data.is_local && !data.is_local_client) {
        // Remote client viewing fallback: switch active tab to view in browser
        switchTab(target);
      }
    } else {
      showToast('Could not open folder: ' + data.error, 'error');
    }
  } catch (e) {
    showToast('Failed to launch Windows Explorer: ' + e, 'error');
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

function showGeneralQR() {
  showQRModal(window.location.origin, 'TurboShare Web Hub');
}

function showQRModal(url, label) {
  document.getElementById('qrModalTitle').textContent = label;
  document.getElementById('qrModalImg').src = '/api/qr?url=' + encodeURIComponent(url);
  document.getElementById('qrModalUrl').textContent = url;
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

/* Initial Boot */
applyClientRole();
loadDirectory('recv', '', true);
</script>

</body>
</html>"""


def render_page(port):
    ifaces = get_network_interfaces()
    with STATE_LOCK:
        recv_path = UPLOAD_DIR
        share_path = HOST_SHARE

    recv_di = disk_info(recv_path)
    share_di = disk_info(share_path) if share_path else {}
    
    recv_path_esc = html.escape(recv_path)
    share_path_esc = html.escape(share_path) if share_path else "No folder selected"

    # Pre-render network cards
    net_items = []
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

    out = HTML_TEMPLATE.replace("__PORT__", str(port))
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
class TurboShareHandler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        global UPLOAD_DIR, HOST_SHARE
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # ── Main Web Dashboard ──
        if path in ("/", "/index.html"):
            content = render_page(SERVER_PORT).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
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
            req_path = qs.get("path", [""])[0]
            data = browse_host_directory(req_path)
            self.send_json(data)
            return

        # ── Directory Listing for Dual Tabs ──
        if path == "/api/list":
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            with STATE_LOCK:
                base = HOST_SHARE if tab == "share" else UPLOAD_DIR
            if not base or not os.path.exists(base):
                self.send_json({"items": [], "path": rel, "disk": disk_info(base)})
                return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_json({"items": [], "path": rel, "disk": disk_info(base)})
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
                "base": base
            })
            return

        # ── Smart Resume Byte Check ──
        if path == "/api/check":
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
            tab = qs.get("tab", ["recv"])[0] or qs.get("target", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            with STATE_LOCK:
                base = HOST_SHARE if tab == "share" else UPLOAD_DIR
            if not base:
                self.send_response(404); self.end_headers(); return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_response(404); self.end_headers(); return

            zip_name = (os.path.basename(target) or "turboshare_export") + ".zip"
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, dirs, files in os.walk(target):
                    for d in dirs:
                        dir_full = os.path.join(root, d)
                        arc_d = os.path.relpath(dir_full, target).replace("\\", "/") + "/"
                        zf.writestr(arc_d, "")
                    for f in files:
                        full_f = os.path.join(root, f)
                        arc_f = os.path.relpath(full_f, target).replace("\\", "/")
                        try:
                            zf.write(full_f, arc_f)
                        except (PermissionError, OSError):
                            pass

            raw_zip = buf.getvalue()
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(raw_zip)))
            safe_ascii_zip = zip_name.encode("ascii", "ignore").decode("ascii").strip() or "archive.zip"
            fname_esc = urllib.parse.quote(zip_name)
            self.send_header("Content-Disposition", f'attachment; filename="{safe_ascii_zip}"; filename*=UTF-8\'\'{fname_esc}')
            self.end_headers()
            try:
                self.wfile.write(raw_zip)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        # ── Trigger Native OS Folder Picker ──
        if path == "/api/pick_folder":
            target_type = qs.get("target", ["share"])[0] or qs.get("type", ["share"])[0]
            chosen, err = pick_folder_powershell()
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

        # ── Resumable Chunked Upload Protocol ──
        if path == "/api/upload":
            rel = qs.get("path", ["upload"])[0]
            offset = int(qs.get("offset", [0])[0])
            target_type = qs.get("target", ["recv"])[0]
            with STATE_LOCK:
                base = HOST_SHARE if target_type == "share" else UPLOAD_DIR
            full = safe_path(base, rel)
            if not full:
                self.send_response(403); self.end_headers(); return

            try:
                os.makedirs(os.path.dirname(full), exist_ok=True)
                content_len = int(self.headers.get("Content-Length", 0))
                bytes_written = 0
                chunk_size = 1024 * 1024  # 1 MB optimal streaming chunk

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
                    if os.path.exists(full):
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
                    else:
                        with open(full, "wb") as f:
                            while bytes_written < content_len:
                                to_read = min(chunk_size, content_len - bytes_written)
                                chunk = self.rfile.read(to_read)
                                if not chunk:
                                    break
                                f.write(chunk)
                                bytes_written += len(chunk)

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
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                data = json.loads(body.decode("utf-8"))
                target_type = data.get("target") or data.get("type") or "recv"
                target_path = os.path.abspath(data.get("path", "").strip().strip("'\""))

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
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                data = json.loads(body.decode("utf-8"))
                parent = data.get("parent", "").strip()
                name = data.get("name", "").strip()
                if not parent or not name:
                    full_p = os.path.abspath(data.get("path", "").strip())
                else:
                    full_p = os.path.abspath(os.path.join(parent, name))

                os.makedirs(full_p, exist_ok=True)
                self.send_json({"success": True, "status": "ok", "path": full_p})
            except Exception as e:
                self.send_json({"success": False, "status": "error", "error": str(e)}, status=400)
            return

        # ── Trigger Native Picker (POST) ──
        if path == "/api/pick_folder":
            chosen, err = pick_folder_powershell()
            if chosen:
                self.send_json({"success": True, "status": "ok", "path": chosen})
            else:
                self.send_json({"success": False, "status": "cancelled", "error": err or "cancelled"})
            return

        # ── Open in OS (POST) ──
        if path == "/api/open_folder":
            content_len = int(self.headers.get("Content-Length", 0))
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


# ═══════════════════════════════════════════════════════════════════════════════
#  APPLICATION BOOTSTRAP & SERVER LAUNCHER
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global UPLOAD_DIR, SERVER_PORT

    default_dir = r"D:\TurboShare" if os.path.exists("D:\\") else os.path.join(
        os.path.expanduser("~"), "Downloads", "TurboShare"
    )

    if len(sys.argv) > 1:
        chosen = sys.argv[1].strip().strip("'\"")
    else:
        chosen = default_dir

    UPLOAD_DIR = os.path.abspath(chosen)
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    ifaces = get_network_interfaces()
    primary = next((f"http://{i['ip']}:{SERVER_PORT}" for i in ifaces
                    if i["kind"] in ("wifi", "ethernet", "ethernet-direct", "hotspot")), None)

    print("=" * 68)
    print("  TurboShare -- High-Speed Cross-Device Transfer Hub")
    print("=" * 68)
    print(f"  Inbox Folder (Save Target) : {UPLOAD_DIR}")
    for i in ifaces:
        print(f"  {i['label']:<24} -> http://{i['ip']}:{SERVER_PORT}")
    print("=" * 68)

    if qrcode and primary:
        try:
            qr = qrcode.QRCode()
            qr.add_data(primary)
            qr.print_ascii(invert=True)
        except Exception:
            pass

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", SERVER_PORT), TurboShareHandler)
    server.daemon_threads = True

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nTurboShare server stopped cleanly.")


if __name__ == "__main__":
    main()
