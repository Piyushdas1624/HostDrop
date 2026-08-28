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
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ── Force UTF-8 on Windows Console ─────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ── Optional libraries ─────────────────────────────────────────────────────────
try:
    import psutil
except ImportError:
    psutil = None

try:
    import qrcode
except ImportError:
    qrcode = None

# ── Global State ───────────────────────────────────────────────────────────────
UPLOAD_DIR  = ""   # Where files sent to the host are stored (Received)
HOST_SHARE  = ""   # Folder shared by host for others to download (Shared)
SERVER_PORT = 8080


# ═══════════════════════════════════════════════════════════════════════════════
#  NETWORK DISCOVERY
# ═══════════════════════════════════════════════════════════════════════════════
def get_network_interfaces():
    interfaces = []
    seen = set()

    if psutil:
        try:
            for name, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == socket.AF_INET and not a.address.startswith("127."):
                        ip = a.address
                        if ip in seen:
                            continue
                        seen.add(ip)
                        lo = name.lower()
                        if "vethernet" in lo or "switch" in lo or "wsl" in lo or "hyper" in lo:
                            kind, label, pri = "virtual", "Virtual / WSL", 9
                        elif "wi-fi" in lo or "wireless" in lo or "wlan" in lo:
                            kind, label, pri = "wifi", "Wi-Fi", 1
                        elif ip.startswith("192.168.137.") or "hotspot" in lo or "host" in lo:
                            kind, label, pri = "hotspot", "Mobile Hotspot", 2
                        elif "ethernet" in lo or "eth" in lo:
                            if ip.startswith("169.254."):
                                kind, label, pri = "ethernet-direct", "Direct Cable (P2P)", 3
                            else:
                                kind, label, pri = "ethernet", "Ethernet LAN", 3
                        elif "bluetooth" in lo:
                            kind, label, pri = "bluetooth", "Bluetooth PAN", 10
                        else:
                            kind, label, pri = "lan", "Local Network", 8
                        interfaces.append(dict(ip=ip, name=name, kind=kind, label=label, priority=pri))
        except Exception:
            pass

    if not interfaces:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127.") and ip not in seen:
                    interfaces.append(dict(ip=ip, name="Network", kind="lan", label="Local Network", priority=5))
                    seen.add(ip)
        except Exception:
            pass

    interfaces.sort(key=lambda x: x["priority"])
    return interfaces


def disk_info(path):
    try:
        total, used, free = shutil.disk_usage(path)
        return {
            "free_gb": f"{free/1024**3:.1f}",
            "total_gb": f"{total/1024**3:.1f}",
            "used_pct": int(used * 100 // total)
        }
    except Exception:
        return {"free_gb": "?", "total_gb": "?", "used_pct": 0}


def safe_path(base_dir, rel):
    if not base_dir:
        return None
    rel = rel.replace("\\", "/").strip("/")
    safe = os.path.normpath(rel).lstrip("/\\")
    full = os.path.abspath(os.path.join(base_dir, safe))
    if not full.startswith(os.path.abspath(base_dir)):
        return None
    return full


def pick_folder_dialog():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        path = filedialog.askdirectory(title="Select folder to share on TurboShare")
        root.destroy()
        return path or None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  HIGH-CRAFT UI TEMPLATE (Linear / Raycast Dark Precision)
# ═══════════════════════════════════════════════════════════════════════════════
def render_page(port):
    ifaces = get_network_interfaces()
    recv_di = disk_info(UPLOAD_DIR)
    share_di = disk_info(HOST_SHARE) if HOST_SHARE else {}
    
    recv_path_esc = html.escape(UPLOAD_DIR)
    share_path_esc = html.escape(HOST_SHARE) if HOST_SHARE else "No folder selected"

    # Pre-render network interface items
    net_items = []
    qr_options = []
    for i in ifaces:
        url = f"http://{i['ip']}:{port}"
        net_items.append(f"""
        <div class="net-item" onclick="copyAddress('{url}')" title="Click to copy address">
          <div class="net-item-header">
            <span class="net-badge {i['kind']}">{i['label']}</span>
            <button class="icon-btn-micro" onclick="event.stopPropagation(); showQRModal('{url}', '{i['label']}')" title="Show QR Code">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="5" height="5" x="3" y="3" rx="1"/><rect width="5" height="5" x="16" y="3" rx="1"/><rect width="5" height="5" x="3" y="16" rx="1"/><path d="M21 16h-3a2 2 0 0 0-2 2v3"/><path d="M21 21v.01"/><path d="M12 7v3a2 2 0 0 1-2 2H7"/><path d="M3 12h.01"/><path d="M12 3h.01"/><path d="M12 16v.01"/><path d="M16 12h1"/><path d="M21 12v.01"/><path d="M12 21v-1"/></svg>
            </button>
          </div>
          <div class="net-item-url">{url}</div>
        </div>
        """)
        qr_options.append(f"""
        <button class="btn btn-ghost btn-sm" onclick="showQRModal('{url}', '{i['label']}')">
          {i['label']}
        </button>
        """)

    net_items_html = "\n".join(net_items)
    qr_options_html = "\n".join(qr_options)

    return f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0">
<title>TurboShare &mdash; LAN Transfer</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ── Reset & Raycast/Linear Design Tokens ──────────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

:root {{
  --canvas: #090a0c;
  --surface-1: #111215;
  --surface-2: #16171b;
  --surface-3: #1c1d22;
  --surface-hover: #22232a;
  
  --border-subtle: rgba(255, 255, 255, 0.05);
  --border-standard: rgba(255, 255, 255, 0.08);
  --border-focus: rgba(255, 255, 255, 0.20);
  
  --text-primary: #ededed;
  --text-secondary: #9ca0a8;
  --text-tertiary: #5c6068;
  
  --accent: #ededed;
  --accent-fg: #090a0c;
  --brand-blue: #4f7fff;
  --status-green: #10b981;
  --status-green-glow: rgba(16, 185, 129, 0.15);
  
  --font-sans: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, SFMono-Regular, Consolas, monospace;
  
  --radius-sm: 6px;
  --radius-md: 8px;
  --radius-lg: 12px;
}}

body {{
  background-color: var(--canvas);
  color: var(--text-primary);
  font-family: var(--font-sans);
  font-size: 13px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  min-height: 100dvh;
  display: flex;
  flex-direction: column;
}}

/* ── Typography & Components ──────────────────────────────────────────────── */
h1, h2, h3, h4 {{ font-weight: 500; letter-spacing: -0.015em; color: var(--text-primary); }}
code, .mono {{ font-family: var(--font-mono); font-size: 12px; }}

.app-header {{
  background: var(--canvas);
  border-bottom: 1px solid var(--border-standard);
  position: sticky;
  top: 0;
  z-index: 100;
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
}}

.header-inner {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 12px 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}}

.brand-wrap {{
  display: flex;
  align-items: center;
  gap: 12px;
}}

.logo-badge {{
  width: 28px;
  height: 28px;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-primary);
}}

.brand-name {{
  font-size: 14px;
  font-weight: 600;
  letter-spacing: -0.02em;
}}

.status-badge {{
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: var(--status-green);
  background: var(--status-green-glow);
  padding: 2px 8px;
  border-radius: 9999px;
  border: 1px solid rgba(16, 185, 129, 0.2);
}}

.status-dot {{
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: var(--status-green);
  box-shadow: 0 0 6px var(--status-green);
}}

.header-actions {{
  display: flex;
  align-items: center;
  gap: 8px;
}}

/* ── Buttons ──────────────────────────────────────────────────────────────── */
.btn {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s ease;
  border: 1px solid transparent;
  white-space: nowrap;
  text-decoration: none;
}}

.btn-primary {{
  background: var(--accent);
  color: var(--accent-fg);
}}
.btn-primary:hover {{
  background: #ffffff;
}}

.btn-ghost {{
  background: transparent;
  color: var(--text-secondary);
  border-color: var(--border-standard);
}}
.btn-ghost:hover {{
  background: var(--surface-2);
  color: var(--text-primary);
  border-color: var(--border-focus);
}}

.btn-sm {{
  height: 28px;
  padding: 0 10px;
  font-size: 11px;
}}

.icon-btn {{
  width: 32px;
  height: 32px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  background: transparent;
  border: 1px solid var(--border-standard);
  color: var(--text-secondary);
  cursor: pointer;
  transition: all 0.15s ease;
}}
.icon-btn:hover {{
  background: var(--surface-2);
  color: var(--text-primary);
  border-color: var(--border-focus);
}}

.icon-btn-micro {{
  width: 20px;
  height: 20px;
  border-radius: 4px;
  background: transparent;
  border: none;
  color: var(--text-tertiary);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
}}
.icon-btn-micro:hover {{
  color: var(--text-primary);
  background: var(--surface-3);
}}

/* ── Network Sub-Bar ──────────────────────────────────────────────────────── */
.network-bar {{
  background: var(--surface-1);
  border-bottom: 1px solid var(--border-subtle);
  padding: 10px 0;
}}

.network-bar-inner {{
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 24px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  overflow-x: auto;
}}

.net-list {{
  display: flex;
  align-items: center;
  gap: 10px;
}}

.net-item {{
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-sm);
  padding: 6px 10px;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 160px;
}}
.net-item:hover {{
  border-color: var(--border-focus);
  background: var(--surface-3);
}}

.net-item-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
}}

.net-badge {{
  font-size: 10px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-tertiary);
}}
.net-badge.wifi {{ color: #60a5fa; }}
.net-badge.hotspot {{ color: #fbbf24; }}
.net-badge.ethernet-direct, .net-badge.ethernet {{ color: #34d399; }}

.net-item-url {{
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-secondary);
}}

/* ── Main Layout ──────────────────────────────────────────────────────────── */
.app-container {{
  max-width: 1200px;
  width: 100%;
  margin: 0 auto;
  padding: 24px;
  flex: 1;
  display: grid;
  grid-template-columns: 360px 1fr;
  gap: 24px;
  align-items: start;
}}

@media (max-width: 860px) {{
  .app-container {{
    grid-template-columns: 1fr;
  }}
}}

/* ── Sidebar Cards ────────────────────────────────────────────────────────── */
.sidebar {{
  display: flex;
  flex-direction: column;
  gap: 20px;
}}

.panel-card {{
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-lg);
  overflow: hidden;
}}

.panel-card-head {{
  padding: 14px 16px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: space-between;
}}

.panel-card-title {{
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-tertiary);
  display: flex;
  align-items: center;
  gap: 8px;
}}

.panel-card-body {{
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}}

/* Path Display Box */
.path-display {{
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-sm);
  padding: 10px 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}}

.path-text {{
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--text-primary);
  word-break: break-all;
  overflow: hidden;
  text-overflow: ellipsis;
}}

.path-text.empty {{
  color: var(--text-tertiary);
  font-style: italic;
}}

/* Dropzone */
.dropzone {{
  background: var(--surface-2);
  border: 1px dashed var(--border-standard);
  border-radius: var(--radius-md);
  padding: 24px 16px;
  text-align: center;
  cursor: pointer;
  transition: all 0.15s ease;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 10px;
}}
.dropzone:hover, .dropzone.active {{
  border-color: var(--border-focus);
  background: var(--surface-3);
}}

.dropzone-icon {{
  color: var(--text-secondary);
}}

.dropzone-title {{
  font-size: 13px;
  font-weight: 500;
  color: var(--text-primary);
}}

.dropzone-sub {{
  font-size: 11px;
  color: var(--text-tertiary);
  max-width: 240px;
  line-height: 1.4;
}}

.dropzone-actions {{
  display: flex;
  gap: 8px;
  margin-top: 4px;
}}

/* Disk gauge */
.disk-meter {{
  font-size: 11px;
  color: var(--text-tertiary);
  display: flex;
  align-items: center;
  justify-content: space-between;
}}

/* ── Explorer View ────────────────────────────────────────────────────────── */
.explorer-card {{
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-lg);
  overflow: hidden;
  display: flex;
  flex-direction: column;
  min-height: 540px;
}}

/* Explorer Tabs */
.explorer-tabs {{
  display: flex;
  border-bottom: 1px solid var(--border-standard);
  padding: 0 16px;
  background: var(--surface-1);
}}

.explorer-tab {{
  padding: 12px 16px;
  font-size: 13px;
  font-weight: 500;
  color: var(--text-secondary);
  cursor: pointer;
  border-bottom: 2px solid transparent;
  transition: all 0.15s ease;
  display: flex;
  align-items: center;
  gap: 8px;
}}
.explorer-tab:hover {{
  color: var(--text-primary);
}}
.explorer-tab.active {{
  color: var(--text-primary);
  border-bottom-color: var(--accent);
}}

.tab-badge {{
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 9999px;
  background: var(--surface-3);
  color: var(--text-secondary);
}}
.explorer-tab.active .tab-badge {{
  background: var(--surface-hover);
  color: var(--text-primary);
}}

/* Explorer Toolbar */
.explorer-toolbar {{
  padding: 12px 16px;
  border-bottom: 1px solid var(--border-subtle);
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}}

.breadcrumbs {{
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}}

.bc-link {{
  color: var(--text-secondary);
  cursor: pointer;
}}
.bc-link:hover {{
  color: var(--text-primary);
  text-decoration: underline;
}}

.toolbar-actions {{
  display: flex;
  align-items: center;
  gap: 8px;
}}

/* Data Table */
.table-container {{
  flex: 1;
  overflow-x: auto;
}}

table {{
  width: 100%;
  border-collapse: collapse;
  text-align: left;
}}

th {{
  padding: 10px 16px;
  font-size: 11px;
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-tertiary);
  border-bottom: 1px solid var(--border-subtle);
}}

td {{
  padding: 10px 16px;
  font-size: 13px;
  border-bottom: 1px solid var(--border-subtle);
  color: var(--text-primary);
}}

tr:hover td {{
  background: var(--surface-2);
}}

.row-item {{
  display: flex;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  color: var(--text-primary);
  text-decoration: none;
}}
.row-item:hover {{
  color: #fff;
}}

.row-icon {{
  color: var(--text-tertiary);
  display: flex;
  align-items: center;
}}
.row-item:hover .row-icon {{
  color: var(--text-primary);
}}

.size-col {{
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-secondary);
}}

.acts-col {{
  text-align: right;
}}

.empty-state {{
  text-align: center;
  padding: 64px 24px;
  color: var(--text-tertiary);
}}

/* ── Transfer Sheet / Progress ────────────────────────────────────────────── */
.transfer-sheet {{
  position: fixed;
  bottom: 20px;
  right: 24px;
  width: 380px;
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-lg);
  padding: 16px;
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.6);
  z-index: 150;
  display: none;
}}
.transfer-sheet.active {{ display: block; }}

.ts-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}}
.ts-title {{
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-secondary);
}}
.ts-speed {{
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--status-green);
  font-weight: 500;
}}

.ts-filename {{
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
  margin-bottom: 8px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}}

.progress-track {{
  height: 4px;
  background: var(--surface-3);
  border-radius: 9999px;
  overflow: hidden;
  margin-bottom: 8px;
}}
.progress-fill {{
  height: 100%;
  width: 0%;
  background: var(--accent);
  transition: width 0.1s linear;
}}

.ts-meta {{
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
}}

/* ── Modals ───────────────────────────────────────────────────────────────── */
.modal-overlay {{
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.75);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  z-index: 200;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 24px;
}}
.modal-overlay.open {{ display: flex; }}

.modal-content {{
  background: var(--surface-1);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-lg);
  width: 100%;
  max-width: 480px;
  padding: 24px;
  box-shadow: 0 16px 40px rgba(0, 0, 0, 0.7);
}}

.modal-header {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
}}
.modal-title {{
  font-size: 15px;
  font-weight: 600;
}}

.input-text {{
  width: 100%;
  height: 36px;
  background: var(--surface-2);
  border: 1px solid var(--border-standard);
  border-radius: var(--radius-sm);
  padding: 0 12px;
  font-family: var(--font-mono);
  font-size: 12px;
  color: var(--text-primary);
  outline: none;
  margin-top: 8px;
}}
.input-text:focus {{
  border-color: var(--border-focus);
}}

/* ── Toast ────────────────────────────────────────────────────────────────── */
.toast {{
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%) translateY(40px);
  opacity: 0;
  background: var(--surface-2);
  color: var(--text-primary);
  border: 1px solid var(--border-standard);
  padding: 8px 16px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  font-weight: 500;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
  pointer-events: none;
  transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
  z-index: 300;
}}
.toast.show {{
  opacity: 1;
  transform: translateX(-50%) translateY(0);
}}
</style>
</head>
<body>

<!-- ── Application Header ─────────────────────────────────────────────────── -->
<header class="app-header">
  <div class="header-inner">
    <div class="brand-wrap">
      <div class="logo-badge">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
      </div>
      <span class="brand-name">TurboShare</span>
      <div class="status-badge">
        <span class="status-dot"></span>
        <span>Ready &middot; Port {port}</span>
      </div>
    </div>
    
    <div class="header-actions">
      <button class="btn btn-ghost btn-sm" onclick="showGeneralQR()">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="5" height="5" x="3" y="3" rx="1"/><rect width="5" height="5" x="16" y="3" rx="1"/><rect width="5" height="5" x="3" y="16" rx="1"/><path d="M21 16h-3a2 2 0 0 0-2 2v3"/><path d="M21 21v.01"/><path d="M12 7v3a2 2 0 0 1-2 2H7"/><path d="M3 12h.01"/><path d="M12 3h.01"/><path d="M12 16v.01"/><path d="M16 12h1"/><path d="M21 12v.01"/><path d="M12 21v-1"/></svg>
        QR Connect
      </button>
      <button class="btn btn-ghost btn-sm" onclick="openModal('helpModal')">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
        Guide
      </button>
    </div>
  </div>
</header>

<!-- ── Network Interfaces Ribbon ──────────────────────────────────────────── -->
<section class="network-bar">
  <div class="network-bar-inner">
    <div style="font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; font-weight: 600; letter-spacing: 0.05em; white-space: nowrap;">
      Network Links
    </div>
    <div class="net-list">
      {net_items_html}
    </div>
  </div>
</section>

<!-- ── Main Workbench ─────────────────────────────────────────────────────── -->
<main class="app-container">

  <!-- Left Column: Controls & Upload -->
  <aside class="sidebar">

    <!-- Host Shared Folder Card -->
    <div class="panel-card">
      <div class="panel-card-head">
        <span class="panel-card-title">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
          Host Shared Folder
        </span>
        <span class="mono" style="font-size:11px; color:var(--text-tertiary);">
          {'Active' if HOST_SHARE else 'Unset'}
        </span>
      </div>
      <div class="panel-card-body">
        <p style="font-size:12px; color:var(--text-secondary);">
          Files inside this directory are published for any connected client to browse and download.
        </p>
        
        <div class="path-display">
          <span class="path-text {'empty' if not HOST_SHARE else ''}" id="hostSharePathText">
            {share_path_esc}
          </span>
          <button class="icon-btn-micro" onclick="copyAddress(document.getElementById('hostSharePathText').textContent.trim())" title="Copy Path">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/></svg>
          </button>
        </div>

        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
          <button class="btn btn-primary btn-sm" onclick="pickShareFolder()">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
            Choose Folder...
          </button>
          <button class="btn btn-ghost btn-sm" onclick="openSetPathModal('share')">
            Edit Path
          </button>
          {'<button class="btn btn-ghost btn-sm" onclick="openInExplorer(\'share\')">Open in OS</button>' if HOST_SHARE else ''}
        </div>

        {'<div class="disk-meter"><span>Disk Available</span><span class="mono">' + share_di.get('free_gb', '?') + ' GB free</span></div>' if HOST_SHARE else ''}
      </div>
    </div>

    <!-- Receive Files Card -->
    <div class="panel-card">
      <div class="panel-card-head">
        <span class="panel-card-title">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Send Files to Host
        </span>
        <button class="btn btn-ghost btn-sm" onclick="openInExplorer('recv')">
          Open Folder
        </button>
      </div>
      <div class="panel-card-body">
        <div class="dropzone" id="dropZone"
             onclick="document.getElementById('filePicker').click()"
             ondragover="handleDragOver(event)"
             ondragleave="handleDragLeave(event)"
             ondrop="handleDrop(event)">
          <div class="dropzone-icon">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          </div>
          <div class="dropzone-title">Drop files or folders here</div>
          <div class="dropzone-sub">Uploads straight to Host PC. Smart resume auto-continues interrupted transfers.</div>
          
          <div class="dropzone-actions" onclick="event.stopPropagation()">
            <button class="btn btn-ghost btn-sm" onclick="document.getElementById('filePicker').click()">Select Files</button>
            <button class="btn btn-ghost btn-sm" onclick="document.getElementById('folderPicker').click()">Select Folder</button>
          </div>
        </div>

        <input type="file" id="filePicker" multiple style="display:none">
        <input type="file" id="folderPicker" webkitdirectory multiple style="display:none">

        <div class="disk-meter">
          <span>Destination Storage</span>
          <span class="mono">{recv_di['free_gb']} GB free</span>
        </div>
        <div style="font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono); word-break: break-all;">
          {recv_path_esc}
        </div>
      </div>
    </div>

  </aside>

  <!-- Right Column: File Explorer -->
  <section class="explorer-card">
    
    <!-- Tab navigation -->
    <div class="explorer-tabs">
      <div class="explorer-tab active" id="tab-recv" onclick="setTab('recv')">
        <span>Received Files</span>
        <span class="tab-badge" id="badge-recv">&middot;</span>
      </div>
      <div class="explorer-tab" id="tab-share" onclick="setTab('share')">
        <span>Host Shared Files</span>
        <span class="tab-badge" id="badge-share">{'Live' if HOST_SHARE else 'Not Set'}</span>
      </div>
    </div>

    <!-- Explorer Toolbar -->
    <div class="explorer-toolbar">
      <div class="breadcrumbs" id="bcContainer">
        <span>Root</span>
      </div>
      <div class="toolbar-actions">
        <button class="icon-btn" onclick="refreshCurrentDir()" title="Refresh">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12a9 9 0 0 0-9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/><path d="M3 12a9 9 0 0 0 9 9 9.75 9.75 0 0 0 6.74-2.74L21 16"/><path d="M16 21h5v-5"/></svg>
        </button>
        <a id="zipDownloadBtn" class="btn btn-ghost btn-sm" href="#">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Download ZIP
        </a>
      </div>
    </div>

    <!-- Data Table -->
    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th style="width: 140px;">Size</th>
            <th class="acts-col" style="width: 120px;">Action</th>
          </tr>
        </thead>
        <tbody id="fileTableBody">
          <tr>
            <td colspan="3" class="empty-state">Loading contents...</td>
          </tr>
        </tbody>
      </table>
    </div>

  </section>

</main>

<!-- ── Transfer Progress Sheet ────────────────────────────────────────────── -->
<div class="transfer-sheet" id="transferSheet">
  <div class="ts-header">
    <span class="ts-title" id="tsTitle">Transferring</span>
    <span class="ts-speed" id="tsSpeed">0.0 MB/s</span>
  </div>
  <div class="ts-filename" id="tsFilename">Preparing transfer...</div>
  <div class="progress-track">
    <div class="progress-fill" id="tsProgressFill"></div>
  </div>
  <div class="ts-meta">
    <span id="tsCount">0 of 0 files</span>
    <span id="tsPercent">0%</span>
  </div>
</div>

<!-- ── QR Code Modal ──────────────────────────────────────────────────────── -->
<div class="modal-overlay" id="qrModal" onclick="closeModal('qrModal')">
  <div class="modal-content" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3 class="modal-title" id="qrModalTitle">Connect Device</h3>
      <button class="icon-btn-micro" onclick="closeModal('qrModal')">&times;</button>
    </div>
    <div style="text-align: center; margin: 16px 0;">
      <div style="background: #fff; padding: 14px; border-radius: var(--radius-md); display: inline-block;">
        <img id="qrModalImg" src="" alt="QR Code" style="width: 200px; height: 200px; display: block;">
      </div>
      <p id="qrModalUrl" class="mono" style="margin-top: 12px; font-size: 12px; color: var(--text-secondary);"></p>
    </div>
    <div style="display: flex; gap: 8px; flex-wrap: wrap; justify-content: center; margin-top: 16px;">
      {qr_options_html}
    </div>
  </div>
</div>

<!-- ── Set Folder Path Modal ──────────────────────────────────────────────── -->
<div class="modal-overlay" id="setPathModal" onclick="closeModal('setPathModal')">
  <div class="modal-content" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3 class="modal-title">Set Folder Path</h3>
      <button class="icon-btn-micro" onclick="closeModal('setPathModal')">&times;</button>
    </div>
    <p style="font-size: 12px; color: var(--text-secondary);">
      Specify absolute directory path on the host computer:
    </p>
    <input type="text" id="manualPathInput" class="input-text" placeholder="e.g. D:/MyFolder">
    <div style="display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px;">
      <button class="btn btn-ghost btn-sm" onclick="closeModal('setPathModal')">Cancel</button>
      <button class="btn btn-primary btn-sm" onclick="submitManualPath()">Save Path</button>
    </div>
  </div>
</div>

<!-- ── Help / Guide Modal ─────────────────────────────────────────────────── -->
<div class="modal-overlay" id="helpModal" onclick="closeModal('helpModal')">
  <div class="modal-content" style="max-width: 540px;" onclick="event.stopPropagation()">
    <div class="modal-header">
      <h3 class="modal-title">Network &amp; Speed Guide</h3>
      <button class="icon-btn-micro" onclick="closeModal('helpModal')">&times;</button>
    </div>
    <div style="display: flex; flex-direction: column; gap: 14px; font-size: 12px; color: var(--text-secondary); line-height: 1.6;">
      <div>
        <strong style="color: var(--text-primary); display: block; margin-bottom: 2px;">Direct PC-to-PC Ethernet (Max Speed: 60&ndash;110 MB/s)</strong>
        Plug an Ethernet cable directly between both computers. No router needed. Windows auto-assigns <code>169.254.x.x</code> addresses. Use the "Direct Cable" link.
      </div>
      <div>
        <strong style="color: var(--text-primary); display: block; margin-bottom: 2px;">Mobile Hotspot (20&ndash;40 MB/s)</strong>
        Ensure your laptop hotspot band is set to <strong>5 GHz</strong> in Windows Settings &rarr; Network &rarr; Mobile Hotspot &rarr; Edit &rarr; Band: 5 GHz. Friend connects and opens <code>192.168.137.1:{port}</code>.
      </div>
      <div>
        <strong style="color: var(--text-primary); display: block; margin-bottom: 2px;">Cross-Device Support</strong>
        Any browser works: iPhone, iPad, Android, Mac, Linux, Xbox, PlayStation, and Smart TVs.
      </div>
      <div>
        <strong style="color: var(--text-primary); display: block; margin-bottom: 2px;">Interrupted Transfers (Smart Resume)</strong>
        Simply drop the same file or folder again. TurboShare computes existing bytes and continues without restarting from 0.
      </div>
    </div>
    <div style="margin-top: 20px; text-align: right;">
      <button class="btn btn-ghost btn-sm" onclick="closeModal('helpModal')">Close</button>
    </div>
  </div>
</div>

<!-- Toast element -->
<div class="toast" id="toast"></div>

<!-- ── Client Logic ───────────────────────────────────────────────────────── -->
<script>
let activeTab = 'recv';
let curRecvPath = '';
let curSharePath = '';
let isTransferring = false;
let editingTarget = 'share';

/* Tab Switcher */
function setTab(tab) {{
  activeTab = tab;
  document.getElementById('tab-recv').classList.toggle('active', tab === 'recv');
  document.getElementById('tab-share').classList.toggle('active', tab === 'share');
  loadDirectory(tab, tab === 'recv' ? curRecvPath : curSharePath, true);
}}

/* File Table Loader */
async function loadDirectory(tab, relPath, force) {{
  if (tab === 'recv') curRecvPath = relPath || '';
  else curSharePath = relPath || '';

  const zipBtn = document.getElementById('zipDownloadBtn');
  zipBtn.href = '/api/zip?tab=' + tab + '&path=' + encodeURIComponent(relPath || '');
  renderBreadcrumbs(tab, relPath);

  try {{
    const res = await fetch('/api/list?tab=' + tab + '&path=' + encodeURIComponent(relPath || ''));
    const data = await res.json();
    const tbody = document.getElementById('fileTableBody');
    tbody.innerHTML = '';

    if (relPath) {{
      const parent = relPath.includes('/') ? relPath.substring(0, relPath.lastIndexOf('/')) : '';
      const row = tbody.insertRow();
      row.innerHTML = `
        <td colspan="3">
          <div class="row-item" onclick="loadDirectory('${{tab}}', '${{parent}}', true)">
            <span class="row-icon">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>
            </span>
            <span class="mono">.. (Parent Directory)</span>
          </div>
        </td>`;
    }}

    if (!data.items || !data.items.length) {{
      const row = tbody.insertRow();
      row.innerHTML = `
        <td colspan="3" class="empty-state">
          ${{tab === 'share' ? 'No folder shared by host yet, or folder is empty.' : 'No files received yet. Drop files on the left to transfer.'}}
        </td>`;
      return;
    }}

    for (const item of data.items) {{
      const itemRel = (relPath ? relPath + '/' : '') + item.name;
      const row = tbody.insertRow();
      if (item.isDir) {{
        row.innerHTML = `
          <td>
            <div class="row-item" onclick="loadDirectory('${{tab}}', '${{itemRel}}', true)">
              <span class="row-icon">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.93a2 2 0 0 1-1.66-.9l-.82-1.2A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2Z"/></svg>
              </span>
              <span>${{escapeHtml(item.name)}}</span>
            </div>
          </td>
          <td class="size-col">${{item.count}} items</td>
          <td class="acts-col">
            <a class="btn btn-ghost btn-sm" href="/api/zip?tab=${{tab}}&path=${{encodeURIComponent(itemRel)}}">ZIP</a>
          </td>`;
      }} else {{
        const mb = (item.size / (1024 * 1024)).toFixed(2);
        row.innerHTML = `
          <td>
            <a class="row-item" href="/download?tab=${{tab}}&path=${{encodeURIComponent(itemRel)}}" target="_blank">
              <span class="row-icon">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75"><path d="M14.5 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7.5L14.5 2z"/><polyline points="14 2 14 8 20 8"/></svg>
              </span>
              <span>${{escapeHtml(item.name)}}</span>
            </a>
          </td>
          <td class="size-col">${{mb}} MB</td>
          <td class="acts-col">
            <a class="btn btn-ghost btn-sm" href="/download?tab=${{tab}}&path=${{encodeURIComponent(itemRel)}}" download>Download</a>
          </td>`;
      }}
    }}
  }} catch (e) {{
    console.error(e);
  }}
}}

function renderBreadcrumbs(tab, relPath) {{
  const container = document.getElementById('bcContainer');
  container.innerHTML = `<span class="bc-link" onclick="loadDirectory('${{tab}}', '', true)">${{tab === 'recv' ? 'Received' : 'Shared'}} Root</span>`;
  if (!relPath) return;

  let acc = '';
  relPath.split('/').forEach(part => {{
    acc = acc ? acc + '/' + part : part;
    const target = acc;
    container.innerHTML += ` / <span class="bc-link" onclick="loadDirectory('${{tab}}', '${{target}}', true)">${{escapeHtml(part)}}</span>`;
  }});
}}

function refreshCurrentDir() {{
  loadDirectory(activeTab, activeTab === 'recv' ? curRecvPath : curSharePath, true);
}}

/* Drag & Drop Upload Handlers */
function handleDragOver(e) {{
  e.preventDefault();
  document.getElementById('dropZone').classList.add('active');
}}

function handleDragLeave(e) {{
  document.getElementById('dropZone').classList.remove('active');
}}

async function handleDrop(e) {{
  e.preventDefault();
  document.getElementById('dropZone').classList.remove('active');
  const items = e.dataTransfer.items;
  const entries = [];

  for (let i = 0; i < items.length; i++) {{
    const entry = items[i].webkitGetAsEntry ? items[i].webkitGetAsEntry() : null;
    if (entry) await traverseEntry(entry, '', entries);
    else if (e.dataTransfer.files[i]) entries.push({{ file: e.dataTransfer.files[i], rel: e.dataTransfer.files[i].name }});
  }}

  if (entries.length) startUpload(entries);
}}

async function traverseEntry(entry, base, list) {{
  if (entry.isFile) {{
    const file = await new Promise(res => entry.file(res));
    list.push({{ file, rel: (base ? base + '/' : '') + file.name }});
  }} else if (entry.isDirectory) {{
    const reader = entry.createReader();
    const children = await new Promise(res => reader.readEntries(res));
    for (const child of children) {{
      await traverseEntry(child, (base ? base + '/' : '') + entry.name, list);
    }}
  }}
}}

document.getElementById('filePicker').onchange = e => {{
  const list = Array.from(e.target.files).map(f => ({{ file: f, rel: f.name }}));
  if (list.length) startUpload(list);
}};

document.getElementById('folderPicker').onchange = e => {{
  const list = Array.from(e.target.files).map(f => ({{ file: f, rel: f.webkitRelativePath || f.name }}));
  if (list.length) startUpload(list);
}};

/* Resumable Upload Pipeline */
async function startUpload(items) {{
  if (!items.length || isTransferring) return;
  isTransferring = true;

  const sheet = document.getElementById('transferSheet');
  sheet.classList.add('active');

  const totalBytes = items.reduce((acc, i) => acc + i.file.size, 0);
  let sentBytes = 0;
  let doneCount = 0;
  let skippedCount = 0;
  let lastBytes = 0;
  let lastTime = Date.now();

  const CONCURRENCY = 2;
  let cursor = 0;

  async function worker() {{
    while (cursor < items.length) {{
      const idx = cursor++;
      const {{ file, rel }} = items[idx];
      const targetRel = curRecvPath ? curRecvPath + '/' + rel : rel;

      let startOffset = 0;
      let skip = false;

      try {{
        const checkRes = await fetch('/api/check?path=' + encodeURIComponent(targetRel));
        const checkData = await checkRes.json();
        if (checkData.size === file.size) {{
          skip = true;
          skippedCount++;
          sentBytes += file.size;
        }} else if (checkData.size > 0 && checkData.size < file.size) {{
          startOffset = checkData.size;
          sentBytes += startOffset;
          document.getElementById('tsFilename').textContent = 'Resuming: ' + rel;
        }}
      }} catch (_) {{}}

      if (!skip) {{
        document.getElementById('tsFilename').textContent = rel;
        for (let retry = 0; retry < 5; retry++) {{
          try {{
            await uploadSlice(file, targetRel, startOffset, delta => {{
              sentBytes += delta;
              startOffset += delta;
            }});
            break;
          }} catch (err) {{
            await new Promise(r => setTimeout(r, 1200));
            try {{
              const r2 = await fetch('/api/check?path=' + encodeURIComponent(targetRel));
              const d2 = await r2.json();
              if (d2.size > 0) startOffset = d2.size;
            }} catch (__) {{}}
          }}
        }}
      }}

      doneCount++;
      const pct = totalBytes > 0 ? Math.min(100, Math.round((sentBytes / totalBytes) * 100)) : 100;
      document.getElementById('tsProgressFill').style.width = pct + '%';
      document.getElementById('tsPercent').textContent = pct + '%';
      document.getElementById('tsCount').textContent = `${{doneCount}} / ${{items.length}} files`;

      const now = Date.now();
      const dt = (now - lastTime) / 1000;
      if (dt >= 0.5) {{
        const speedMb = (sentBytes - lastBytes) / (1024 * 1024) / dt;
        document.getElementById('tsSpeed').textContent = Math.max(0, speedMb).toFixed(1) + ' MB/s';
        lastBytes = sentBytes;
        lastTime = now;
      }}
    }}
  }}

  await Promise.all(Array.from({{ length: Math.min(CONCURRENCY, items.length) }}, worker));

  document.getElementById('tsProgressFill').style.width = '100%';
  document.getElementById('tsSpeed').textContent = 'Complete';
  document.getElementById('tsFilename').textContent = `Uploaded ${{items.length}} items (${{skippedCount}} unchanged)`;
  isTransferring = false;

  setTimeout(() => sheet.classList.remove('active'), 4000);
  loadDirectory('recv', curRecvPath, true);
}}

function uploadSlice(file, relPath, offset, onProgressDelta) {{
  return new Promise((resolve, reject) => {{
    const xhr = new XMLHttpRequest();
    let loadedSoFar = 0;
    let lastActive = Date.now();

    const watchdog = setInterval(() => {{
      if (Date.now() - lastActive > 20000) {{
        clearInterval(watchdog);
        xhr.abort();
        reject(new Error('timeout'));
      }}
    }}, 3000);

    xhr.upload.onprogress = e => {{
      lastActive = Date.now();
      const delta = e.loaded - loadedSoFar;
      loadedSoFar = e.loaded;
      if (delta > 0) onProgressDelta(delta);
    }};

    xhr.open('POST', '/api/upload?path=' + encodeURIComponent(relPath) + '&offset=' + offset, true);
    xhr.onload = () => {{
      clearInterval(watchdog);
      xhr.status === 200 ? resolve() : reject(new Error(xhr.statusText));
    }};
    xhr.onerror = () => {{
      clearInterval(watchdog);
      reject(new Error('network_error'));
    }};
    xhr.onabort = () => {{
      clearInterval(watchdog);
      reject(new Error('aborted'));
    }};

    xhr.send(offset > 0 ? file.slice(offset) : file);
  }});
}}

/* Host Folder Actions */
async function pickShareFolder() {{
  try {{
    const res = await fetch('/api/pick_folder');
    const data = await res.json();
    if (data.success) {{
      location.reload();
    }} else if (data.error !== 'cancelled') {{
      showToast('Could not open OS folder dialog: ' + data.error);
    }}
  }} catch (e) {{
    showToast('Failed: ' + e);
  }}
}}

function openSetPathModal(target) {{
  editingTarget = target;
  document.getElementById('manualPathInput').value = target === 'share' 
    ? document.getElementById('hostSharePathText').textContent.trim() 
    : '';
  openModal('setPathModal');
}}

async function submitManualPath() {{
  const path = document.getElementById('manualPathInput').value.trim();
  if (!path) return;
  try {{
    const res = await fetch('/api/set_folder', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ type: editingTarget, path: path }})
    }});
    const data = await res.json();
    if (data.success) {{
      location.reload();
    }} else {{
      showToast('Error: ' + data.error);
    }}
  }} catch (e) {{
    showToast('Failed to set path');
  }}
}}

async function openInExplorer(target) {{
  try {{
    await fetch('/api/open_folder?type=' + target);
    showToast('Opened in Explorer');
  }} catch (e) {{
    showToast('Could not open folder');
  }}
}}

/* UI Helpers */
function copyAddress(text) {{
  navigator.clipboard.writeText(text).then(() => {{
    showToast('Copied: ' + text);
  }}).catch(() => {{
    prompt('Copy address:', text);
  }});
}}

function showToast(msg) {{
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2200);
}}

function showGeneralQR() {{
  showQRModal(window.location.origin, 'TurboShare Web Hub');
}}

function showQRModal(url, label) {{
  document.getElementById('qrModalTitle').textContent = label;
  document.getElementById('qrModalImg').src = '/api/qr?url=' + encodeURIComponent(url);
  document.getElementById('qrModalUrl').textContent = url;
  openModal('qrModal');
}}

function openModal(id) {{
  document.getElementById(id).classList.add('open');
}}

function closeModal(id) {{
  document.getElementById(id).classList.remove('open');
}}

window.addEventListener('keydown', e => {{
  if (e.key === 'Escape') {{
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
  }}
}});

function escapeHtml(str) {{
  return (str || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}}

/* Auto-sync directory listing every 4 seconds */
setInterval(() => {{
  if (!isTransferring) {{
    loadDirectory(activeTab, activeTab === 'recv' ? curRecvPath : curSharePath, false);
  }}
}}, 4000);

/* Initial Boot */
loadDirectory('recv', '', true);
</script>

</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
#  HTTP REQUEST ROUTER
# ═══════════════════════════════════════════════════════════════════════════════
class TurboShareHandler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        payload = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
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

        # ── Trigger Native Folder Picker on Host ──
        if path == "/api/pick_folder":
            global HOST_SHARE
            chosen = pick_folder_dialog()
            if chosen:
                HOST_SHARE = os.path.abspath(chosen)
                self.send_json({"success": True, "path": HOST_SHARE})
            else:
                self.send_json({"success": False, "error": "cancelled"})
            return

        # ── Open in Host OS Explorer ──
        if path == "/api/open_folder":
            target_type = qs.get("type", ["recv"])[0]
            target_dir = HOST_SHARE if target_type == "share" else UPLOAD_DIR
            if target_dir and os.path.exists(target_dir):
                if sys.platform == "win32":
                    os.startfile(target_dir)
                self.send_json({"success": True})
            else:
                self.send_json({"success": False, "error": "directory_not_found"})
            return

        # ── Smart Resume Check ──
        if path == "/api/check":
            rel = qs.get("path", [""])[0]
            full = safe_path(UPLOAD_DIR, rel)
            if full and os.path.isfile(full):
                self.send_json({"exists": True, "size": os.path.getsize(full)})
            else:
                self.send_json({"exists": False, "size": 0})
            return

        # ── Directory Listing ──
        if path == "/api/list":
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            base = HOST_SHARE if tab == "share" else UPLOAD_DIR
            if not base or not os.path.exists(base):
                self.send_json({"items": []})
                return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_json({"items": []})
                return
            items = []
            try:
                for entry in sorted(os.listdir(target), key=lambda x: (not os.path.isdir(os.path.join(target, x)), x.lower())):
                    fp = os.path.join(target, entry)
                    if os.path.isdir(fp):
                        items.append({"name": entry, "isDir": True, "count": len(os.listdir(fp))})
                    else:
                        items.append({"name": entry, "isDir": False, "size": os.path.getsize(fp)})
            except Exception:
                pass
            self.send_json({"items": items})
            return

        # ── Download Single File ──
        if path == "/download":
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
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
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(fp)}"')
            self.end_headers()
            with open(fp, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    self.wfile.write(chunk)
            return

        # ── Stream Folder as ZIP ──
        if path == "/api/zip":
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            base = HOST_SHARE if tab == "share" else UPLOAD_DIR
            if not base:
                self.send_response(404); self.end_headers(); return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_response(404); self.end_headers(); return

            zip_name = (os.path.basename(target) or "turboshare") + ".zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{zip_name}"')
            self.end_headers()
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(target):
                    for file in files:
                        full_f = os.path.join(root, file)
                        zf.write(full_f, os.path.relpath(full_f, target))
            self.wfile.write(buf.getvalue())
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        global HOST_SHARE, UPLOAD_DIR
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        # ── Resumable Chunk Upload ──
        if path == "/api/upload":
            rel = qs.get("path", ["upload"])[0]
            offset = int(qs.get("offset", [0])[0])
            full = safe_path(UPLOAD_DIR, rel)
            if not full:
                self.send_response(403); self.end_headers(); return

            os.makedirs(os.path.dirname(full), exist_ok=True)
            content_len = int(self.headers.get("Content-Length", 0))
            bytes_written = 0
            mode = "ab" if offset > 0 else "wb"

            with open(full, mode) as f:
                while bytes_written < content_len:
                    chunk = self.rfile.read(min(1024 * 1024, content_len - bytes_written))
                    if not chunk:
                        break
                    f.write(chunk)
                    bytes_written += len(chunk)

            self.send_json({"success": True, "saved": rel, "bytes": bytes_written})
            return

        # ── Manual Path Configuration ──
        if path == "/api/set_folder":
            content_len = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_len)
            try:
                data = json.loads(body.decode("utf-8"))
                target_type = data.get("type", "share")
                target_path = os.path.abspath(data.get("path", "").strip())

                if not os.path.exists(target_path):
                    os.makedirs(target_path, exist_ok=True)

                if target_type == "share":
                    HOST_SHARE = target_path
                else:
                    UPLOAD_DIR = target_path

                self.send_json({"success": True, "path": target_path})
            except Exception as e:
                self.send_json({"success": False, "error": str(e)}, status=400)
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, *_):
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  APPLICATION BOOTSTRAP
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global UPLOAD_DIR, SERVER_PORT

    default_dir = r"D:\EthernetTransfers" if os.path.exists("D:\\") else os.path.join(
        os.path.expanduser("~"), "Downloads", "EthernetTransfers")

    if len(sys.argv) > 1:
        chosen = sys.argv[1].strip().strip("'\"")
    else:
        chosen = default_dir

    UPLOAD_DIR = os.path.abspath(chosen)
    os.makedirs(UPLOAD_DIR, exist_ok=True)

    ifaces = get_network_interfaces()
    primary = next((f"http://{i['ip']}:{SERVER_PORT}" for i in ifaces
                    if "wifi" in i["kind"] or "ethernet" in i["kind"]), None)

    print("-" * 64)
    print("  TurboShare &mdash; High-Speed Cross-Device Transfer Hub")
    print("-" * 64)
    print(f"  Storage Target : {UPLOAD_DIR}")
    for i in ifaces:
        print(f"  {i['label']:<24} -> http://{i['ip']}:{SERVER_PORT}")
    print("-" * 64)

    if qrcode and primary:
        try:
            qr = qrcode.QRCode()
            qr.add_data(primary)
            qr.print_ascii(invert=True)
        except Exception:
            pass

    server = ThreadingHTTPServer(("0.0.0.0", SERVER_PORT), TurboShareHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutdown.")


if __name__ == "__main__":
    main()
