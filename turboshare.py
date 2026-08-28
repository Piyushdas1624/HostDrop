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

# ── UTF-8 console on Windows ──────────────────────────────────────────────────
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

# ── Globals set at runtime ────────────────────────────────────────────────────
UPLOAD_DIR   = ""   # where friends upload TO  (receive dir)
HOST_SHARE   = ""   # folder HOST shares FROM  (browse/download dir)
SERVER_PORT  = 8080


# ═══════════════════════════════════════════════════════════════════════════════
#  NETWORK HELPERS
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
                            cat, icon, pri = "Virtual / WSL", "💻", 9
                        elif "wi-fi" in lo or "wireless" in lo or "wlan" in lo:
                            cat, icon, pri = "Wi-Fi", "📶", 1
                        elif ip.startswith("192.168.137."):
                            cat, icon, pri = "Mobile Hotspot", "📡", 2
                        elif "hotspot" in lo or "host" in lo:
                            cat, icon, pri = "Mobile Hotspot", "📡", 2
                        elif "ethernet" in lo or "eth" in lo:
                            cat, icon, pri = ("Direct Ethernet (Cable)", "🔌", 3) if ip.startswith("169.254.") else ("Wired Ethernet", "🔌", 3)
                        elif "bluetooth" in lo:
                            cat, icon, pri = "Bluetooth PAN", "🔵", 10
                        else:
                            cat, icon, pri = "LAN", "🌐", 8
                        interfaces.append(dict(ip=ip, name=name, category=cat, icon=icon, priority=pri))
        except Exception:
            pass

    if not interfaces:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127.") and ip not in seen:
                    interfaces.append(dict(ip=ip, name="Network", category="Wi-Fi / LAN", icon="🌐", priority=5))
                    seen.add(ip)
        except Exception:
            pass

    interfaces.sort(key=lambda x: x["priority"])
    return interfaces


def disk_info(path):
    try:
        total, used, free = shutil.disk_usage(path)
        return dict(free_gb=f"{free/1024**3:.1f}", total_gb=f"{total/1024**3:.1f}",
                    pct=int(used*100//total))
    except Exception:
        return dict(free_gb="?", total_gb="?", pct=0)


# ═══════════════════════════════════════════════════════════════════════════════
#  HTML PAGE
# ═══════════════════════════════════════════════════════════════════════════════
def render_page(port):
    ifaces   = get_network_interfaces()
    recv_di  = disk_info(UPLOAD_DIR)
    share_di = disk_info(HOST_SHARE) if HOST_SHARE else {}

    # build network pills
    pills = []
    for i in ifaces:
        url = f"http://{i['ip']}:{port}"
        pills.append(f"""
<div class="pill">
  <div class="pill-left">
    <span class="pill-icon">{i['icon']}</span>
    <div>
      <div class="pill-cat">{i['category']}</div>
      <div class="pill-url">{url}</div>
    </div>
  </div>
  <div class="pill-actions">
    <button class="btn-xs" onclick="copyText('{url}')">Copy</button>
    <button class="btn-xs" onclick="showQR('{url}')">QR</button>
  </div>
</div>""")

    share_dir_text = html.escape(HOST_SHARE) if HOST_SHARE else "No folder selected"
    recv_dir_text  = html.escape(UPLOAD_DIR)

    # QR modal per-interface buttons (precomputed to avoid f-string quoting issues)
    qr_btns = "".join(
        f'<button class="btn btn-ghost btn-sm" onclick="showQR(\'http://{i["ip"]}:{port}\')">'
        f'{i["icon"]} {i["category"]}</button>'
        for i in ifaces
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5">
<title>⚡ TurboShare</title>
<style>
/* ── Reset & tokens ─────────────────────────────── */
*, *::before, *::after {{ box-sizing: border-box; margin:0; padding:0; }}
:root {{
  --bg:        #080c14;
  --surface:   #0e1520;
  --surface2:  #141d2b;
  --border:    rgba(255,255,255,.07);
  --border-hi: rgba(56,189,248,.35);
  --text:      #e2e8f0;
  --muted:     #64748b;
  --dim:       #334155;
  --cyan:      #38bdf8;
  --cyan-dark: #0284c7;
  --green:     #10b981;
  --amber:     #f59e0b;
  --red:       #f43f5e;
  --r:         12px;
  --r-lg:      18px;
  --shadow:    0 8px 24px rgba(0,0,0,.5);
}}
body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
        background:var(--bg); color:var(--text);
        min-height:100dvh; display:flex; flex-direction:column; }}

/* ── Utility ────────────────────────────────────── */
.sr-only {{ position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0,0,0,0); }}
a {{ color:inherit; text-decoration:none; }}
code {{ font-family:ui-monospace,monospace; font-size:.85em; color:var(--cyan);
        background:rgba(56,189,248,.1); padding:2px 6px; border-radius:4px; }}

/* ── Buttons ────────────────────────────────────── */
.btn {{
  display:inline-flex; align-items:center; gap:6px; cursor:pointer;
  border:1px solid transparent; border-radius:var(--r); font-weight:600;
  transition:all .15s ease; text-decoration:none; white-space:nowrap;
  padding:9px 16px; font-size:13px; min-height:38px;
  background:var(--cyan-dark); color:#fff;
}}
.btn:hover {{ background:#0369a1; transform:translateY(-1px); }}
.btn:active {{ transform:translateY(0); }}
.btn-ghost {{ background:rgba(255,255,255,.05); border-color:var(--border); color:var(--text); }}
.btn-ghost:hover {{ background:rgba(255,255,255,.09); border-color:rgba(255,255,255,.15); }}
.btn-sm {{ padding:6px 12px; font-size:12px; min-height:32px; }}
.btn-danger {{ background:rgba(244,63,94,.15); color:var(--red); border-color:rgba(244,63,94,.3); }}
.btn-danger:hover {{ background:rgba(244,63,94,.25); }}
.btn-xs {{
  padding:4px 10px; font-size:11px; font-weight:600; border-radius:6px;
  background:rgba(255,255,255,.06); border:1px solid var(--border);
  color:var(--text); cursor:pointer; min-height:26px;
  transition:background .15s;
}}
.btn-xs:hover {{ background:rgba(255,255,255,.12); }}

/* ── Navbar ─────────────────────────────────────── */
.navbar {{
  position:sticky; top:0; z-index:200;
  background:rgba(8,12,20,.88);
  backdrop-filter:blur(20px); -webkit-backdrop-filter:blur(20px);
  border-bottom:1px solid var(--border);
  padding:10px 20px;
  display:flex; align-items:center; justify-content:space-between; gap:12px;
}}
.brand {{ display:flex; align-items:center; gap:10px; }}
.brand-mark {{
  width:34px; height:34px; border-radius:10px;
  background:linear-gradient(135deg,#0284c7,#38bdf8);
  display:flex; align-items:center; justify-content:center; font-size:17px;
  box-shadow:0 4px 16px rgba(14,165,233,.3);
  flex-shrink:0;
}}
.brand-name {{ font-size:16px; font-weight:700; letter-spacing:-.02em; }}
.brand-status {{
  font-size:10px; font-weight:600; color:var(--green);
  display:flex; align-items:center; gap:4px;
}}
.brand-status::before {{
  content:""; width:5px; height:5px; background:var(--green);
  border-radius:50%; box-shadow:0 0 6px var(--green);
}}
.nav-right {{ display:flex; gap:8px; align-items:center; flex-wrap:wrap; }}

/* ── Layout ─────────────────────────────────────── */
.main {{ max-width:1080px; width:100%; margin:0 auto; padding:20px; flex:1;
         display:flex; flex-direction:column; gap:16px; }}

/* ── Cards ──────────────────────────────────────── */
.card {{
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--r-lg); overflow:hidden;
}}
.card-head {{
  padding:14px 18px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  flex-wrap:wrap; gap:10px;
  background:rgba(255,255,255,.02);
}}
.card-title {{ font-size:12px; font-weight:700; text-transform:uppercase;
               letter-spacing:.06em; color:var(--muted);
               display:flex; align-items:center; gap:7px; }}
.card-body {{ padding:16px 18px; }}

/* ── The dual-panel hero ────────────────────────── */
.dual-panel {{
  display:grid; grid-template-columns:1fr 1fr; gap:16px;
}}
@media (max-width:700px) {{ .dual-panel {{ grid-template-columns:1fr; }} }}

.panel {{
  background:var(--surface2); border:1px solid var(--border);
  border-radius:var(--r-lg); padding:20px;
  display:flex; flex-direction:column; gap:14px;
  transition:border-color .2s;
}}
.panel:focus-within, .panel:hover {{ border-color:var(--border-hi); }}
.panel-label {{
  font-size:11px; font-weight:700; text-transform:uppercase;
  letter-spacing:.07em; color:var(--muted);
  display:flex; align-items:center; gap:6px;
}}
.panel-heading {{ font-size:17px; font-weight:700; line-height:1.25; }}
.panel-sub {{ font-size:12px; color:var(--muted); line-height:1.5; }}

/* HOST SHARE panel */
.panel-share .panel-label {{ color:#a78bfa; }}
.panel-share {{ border-color:rgba(167,139,250,.2); }}
.panel-share:hover {{ border-color:rgba(167,139,250,.5); }}

/* Share folder display */
.folder-badge {{
  background:rgba(167,139,250,.1); border:1px solid rgba(167,139,250,.25);
  border-radius:8px; padding:10px 14px;
  display:flex; align-items:center; gap:10px; flex-wrap:wrap;
}}
.folder-path {{
  font-size:12px; font-family:monospace; color:#c4b5fd;
  word-break:break-all; flex:1;
}}
.folder-empty {{ color:var(--muted); font-style:italic; }}

/* Upload/receive panel */
.panel-recv .panel-label {{ color:var(--cyan); }}
.panel-recv {{ border-color:rgba(56,189,248,.15); }}

/* Drop zone inside recv panel */
.drop-inner {{
  border:2px dashed rgba(56,189,248,.3); border-radius:10px;
  padding:24px 16px; text-align:center; cursor:pointer;
  transition:all .2s; background:rgba(56,189,248,.03);
  flex:1; display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:10px;
  min-height:120px;
}}
.drop-inner:hover, .drop-inner.over {{
  border-color:var(--cyan);
  background:rgba(56,189,248,.08);
}}
.drop-inner svg {{ fill:var(--cyan); width:40px; height:40px;
                   filter:drop-shadow(0 3px 8px rgba(56,189,248,.3)); }}
.drop-text {{ font-size:14px; font-weight:600; }}
.drop-sub  {{ font-size:12px; color:var(--muted); }}
.btn-row {{ display:flex; gap:8px; justify-content:center; flex-wrap:wrap; }}
.recv-dest {{ font-size:12px; color:var(--muted); font-family:monospace; word-break:break-all; }}

/* ── Progress ────────────────────────────────────── */
.progress-card {{
  display:none;
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--r-lg); padding:18px;
}}
.progress-track {{
  background:rgba(255,255,255,.06); border-radius:8px;
  height:12px; overflow:hidden; margin-bottom:10px;
}}
.progress-fill {{
  background:linear-gradient(90deg,#0284c7,#38bdf8);
  height:100%; width:0; transition:width .1s linear;
  box-shadow:0 0 10px rgba(56,189,248,.5);
}}
.progress-meta {{ display:flex; justify-content:space-between; font-size:12px; color:var(--muted); flex-wrap:wrap; gap:8px; }}
.progress-file {{ margin-top:6px; font-size:11px; color:var(--cyan); font-family:monospace; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}

/* ── Network pills ───────────────────────────────── */
.pills-grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:10px; }}
.pill {{
  background:rgba(255,255,255,.025); border:1px solid var(--border);
  border-radius:10px; padding:12px 14px;
  display:flex; align-items:center; justify-content:space-between; gap:10px;
  transition:all .15s;
}}
.pill:hover {{ background:rgba(255,255,255,.05); border-color:rgba(56,189,248,.35); }}
.pill-left {{ display:flex; align-items:center; gap:10px; overflow:hidden; }}
.pill-icon {{ font-size:20px; flex-shrink:0; }}
.pill-cat  {{ font-size:12px; font-weight:600; color:var(--cyan); }}
.pill-url  {{ font-size:11px; font-family:monospace; color:var(--text); opacity:.7; }}
.pill-actions {{ display:flex; gap:6px; flex-shrink:0; }}

/* ── File Explorer ───────────────────────────────── */
.explorer-tabs {{
  display:flex; gap:2px; padding:12px 18px 0;
  border-bottom:1px solid var(--border);
}}
.tab {{
  padding:8px 16px; font-size:13px; font-weight:600; cursor:pointer;
  border-radius:8px 8px 0 0; color:var(--muted);
  border:1px solid transparent; border-bottom:none;
  transition:all .15s; background:none;
}}
.tab.active {{
  background:var(--surface2); color:var(--text);
  border-color:var(--border); border-bottom-color:var(--surface2);
}}
.tab:hover:not(.active) {{ color:var(--text); }}
.tab-panel {{ display:none; }}
.tab-panel.active {{ display:block; }}

.explorer-toolbar {{
  padding:12px 18px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  flex-wrap:wrap; gap:10px; background:rgba(255,255,255,.015);
}}
.breadcrumbs {{ font-size:13px; color:var(--muted); display:flex; gap:5px; align-items:center; flex-wrap:wrap; }}
.breadcrumbs a {{ color:var(--cyan); cursor:pointer; font-weight:500; }}
.breadcrumbs a:hover {{ text-decoration:underline; }}
.table-wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; }}
th {{ padding:11px 18px; font-size:11px; color:var(--dim); text-transform:uppercase;
      letter-spacing:.05em; border-bottom:1px solid var(--border); text-align:left; }}
td {{ padding:12px 18px; font-size:13px; border-bottom:1px solid rgba(255,255,255,.03); vertical-align:middle; }}
tr:hover {{ background:rgba(255,255,255,.03); }}
.file-link {{ display:flex; align-items:center; gap:9px; font-weight:500; color:var(--text); cursor:pointer; }}
.file-link:hover {{ color:var(--cyan); }}
.folder-link {{ color:#facc15; }}
.sz {{ color:var(--muted); font-size:12px; white-space:nowrap; }}
.acts {{ text-align:right; white-space:nowrap; }}

/* ── Status bar ─────────────────────────────────── */
.statusbar {{
  font-size:11px; color:var(--muted); padding:10px 20px;
  border-top:1px solid var(--border); display:flex; gap:16px;
  flex-wrap:wrap; align-items:center;
}}
.status-dot {{ width:6px; height:6px; background:var(--green); border-radius:50%;
               box-shadow:0 0 6px var(--green); display:inline-block; }}

/* ── QR & Modals ─────────────────────────────────── */
.overlay {{
  position:fixed; inset:0; background:rgba(0,0,0,.75);
  backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px);
  z-index:500; display:none; align-items:center; justify-content:center; padding:20px;
}}
.overlay.open {{ display:flex; }}
.modal {{
  background:var(--surface); border:1px solid var(--border);
  border-radius:var(--r-lg); padding:24px; max-width:520px; width:100%;
  box-shadow:var(--shadow); max-height:88vh; overflow-y:auto;
}}
.modal-head {{
  display:flex; align-items:center; justify-content:space-between; margin-bottom:18px;
}}
.modal-title {{ font-size:17px; font-weight:700; }}
.modal-close {{ background:none; border:none; color:var(--muted); font-size:22px;
                cursor:pointer; padding:4px 8px; border-radius:6px; }}
.modal-close:hover {{ color:var(--text); background:rgba(255,255,255,.07); }}

/* Trouble accordion */
.acc-item {{ border:1px solid var(--border); border-radius:10px; margin-bottom:8px; overflow:hidden; }}
.acc-q {{
  padding:13px 16px; font-size:13px; font-weight:600;
  cursor:pointer; display:flex; justify-content:space-between; align-items:center;
}}
.acc-q:hover {{ background:rgba(255,255,255,.04); }}
.acc-a {{ padding:0 16px; font-size:12px; color:var(--muted); line-height:1.7; display:none; }}
.acc-a.open {{ display:block; padding-bottom:13px; }}
.acc-a ul {{ margin:6px 0 0 16px; }}

/* ── Toast ───────────────────────────────────────── */
.toast {{
  position:fixed; bottom:24px; left:50%;
  transform:translateX(-50%) translateY(80px);
  background:var(--surface); color:var(--text);
  padding:10px 20px; border-radius:10px;
  border:1px solid var(--cyan); font-size:13px; font-weight:600;
  box-shadow:var(--shadow); z-index:999;
  transition:transform .3s cubic-bezier(.16,1,.3,1); pointer-events:none;
}}
.toast.show {{ transform:translateX(-50%) translateY(0); }}

/* ── Responsive ──────────────────────────────────── */
@media (max-width:600px) {{
  .main {{ padding:12px; gap:12px; }}
  .card-body {{ padding:12px; }}
  th:nth-child(2), td:nth-child(2) {{ display:none; }}
}}
</style>
</head>
<body>

<!-- ── Navbar ── -->
<header class="navbar">
  <div class="brand">
    <div class="brand-mark">⚡</div>
    <div>
      <div class="brand-name">TurboShare</div>
      <div class="brand-status">Live · Ready</div>
    </div>
  </div>
  <nav class="nav-right" aria-label="Navigation">
    <button class="btn btn-ghost btn-sm" onclick="openQR()">📱 QR</button>
    <button class="btn btn-ghost btn-sm" onclick="openModal('helpModal')">❓ Help</button>
  </nav>
</header>

<main class="main">

  <!-- ── Network links ── -->
  <section class="card" aria-label="Connection links">
    <div class="card-head">
      <div class="card-title">📡 Connect From Any Device</div>
      <span style="font-size:11px;color:var(--muted)">Open URL on phone, TV, console, or friend's PC</span>
    </div>
    <div class="card-body">
      <div class="pills-grid">
        {''.join(pills)}
      </div>
    </div>
  </section>

  <!-- ── Two-panel sharing hub ── -->
  <div class="dual-panel">

    <!-- HOST: choose folder to share -->
    <div class="panel panel-share" role="region" aria-label="Host share folder">
      <div class="panel-label">🟣 You are Sharing (Host)</div>
      <div>
        <div class="panel-heading">Pick a folder to share</div>
        <div class="panel-sub">Friends can browse &amp; download everything inside this folder — no USB, no cloud needed.</div>
      </div>

      <div class="folder-badge" id="shareFolderBadge">
        <span style="font-size:20px">📁</span>
        <span class="folder-path {'folder-empty' if not HOST_SHARE else ''}" id="shareFolderPath">
          {share_dir_text if HOST_SHARE else 'No folder selected yet'}
        </span>
      </div>

      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-sm" style="background:rgba(167,139,250,.2);color:#c4b5fd;border-color:rgba(167,139,250,.35);"
                onclick="chooseShareFolder()">
          📂 Choose Folder
        </button>
        {'<button class="btn btn-sm btn-danger" onclick="clearShareFolder()">✕ Clear</button>' if HOST_SHARE else ''}
      </div>

      {'<div style="font-size:11px;color:var(--muted)">💾 Drive space: <strong style=\'color:var(--text)\'>' + share_di.get('free_gb','?') + ' GB free</strong></div>' if HOST_SHARE else ''}
    </div>

    <!-- RECEIVE: friends upload here -->
    <div class="panel panel-recv" role="region" aria-label="Receive files">
      <div class="panel-label">🔵 Receive Files</div>
      <div>
        <div class="panel-heading">Drop or pick files to send here</div>
        <div class="panel-sub">Files land on this PC automatically. Auto-resumes if interrupted.</div>
      </div>

      <div class="drop-inner" id="dropZone"
           onclick="document.getElementById('fileInput').click()"
           ondragover="ev(event)" ondragleave="ev2()" ondrop="onDrop(event)">
        <svg viewBox="0 0 24 24" aria-hidden="true">
          <path d="M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35
                   8.04 2.34 8.36 0 10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0
                   5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96zM14 13v4h-4v-4H7l5-5 5 5h-3z"/>
        </svg>
        <div class="drop-text">Drag &amp; Drop Files or Folders</div>
        <div class="drop-sub">Works from PC, phone, TV, Xbox, PS5</div>
      </div>

      <div class="btn-row" onclick="event.stopPropagation()">
        <button class="btn btn-sm btn-ghost" onclick="document.getElementById('fileInput').click()">📄 Files</button>
        <button class="btn btn-sm"           onclick="document.getElementById('folderInput').click()">📁 Folder</button>
      </div>
      <div class="recv-dest">Saves to: <code>{recv_dir_text}</code></div>
      <div style="font-size:11px;color:var(--muted)">💾 <strong style="color:var(--text)">{recv_di['free_gb']} GB</strong> free</div>

      <input type="file" id="fileInput"   multiple style="display:none">
      <input type="file" id="folderInput" webkitdirectory multiple style="display:none">
    </div>

  </div>

  <!-- ── Progress ── -->
  <div class="progress-card" id="progressCard">
    <div class="progress-track"><div class="progress-fill" id="pBar"></div></div>
    <div class="progress-meta">
      <span id="pStatus" style="color:var(--text)">Preparing…</span>
      <span id="pSpeed" style="color:var(--cyan);font-weight:700">0 MB/s</span>
    </div>
    <div class="progress-file" id="pFile"></div>
  </div>

  <!-- ── File Explorer with tabs ── -->
  <section class="card" aria-label="File browser">
    <div class="explorer-tabs">
      <button class="tab active" onclick="switchTab('recv')" id="tab-recv">
        📥 Received Files
      </button>
      <button class="tab" onclick="switchTab('share')" id="tab-share">
        📤 Host Shared Folder {'<span style="background:var(--green);color:#000;font-size:9px;padding:1px 5px;border-radius:20px;margin-left:4px;font-weight:700">LIVE</span>' if HOST_SHARE else '<span style="background:var(--dim);color:var(--muted);font-size:9px;padding:1px 5px;border-radius:20px;margin-left:4px">NOT SET</span>'}
      </button>
    </div>

    <!-- RECV tab -->
    <div class="tab-panel active" id="panel-recv">
      <div class="explorer-toolbar">
        <div class="breadcrumbs" id="bc-recv"><span>📁 Received Root</span></div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-ghost btn-sm" onclick="loadDir('recv', recvPath, true)">↻ Refresh</button>
          <a id="zip-recv" class="btn btn-sm" href="#">📦 ZIP</a>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Size</th><th class="acts">Actions</th></tr></thead>
          <tbody id="tbody-recv"><tr><td colspan="3" style="text-align:center;padding:32px;color:var(--muted)">Loading…</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- SHARE tab -->
    <div class="tab-panel" id="panel-share">
      <div class="explorer-toolbar">
        <div class="breadcrumbs" id="bc-share"><span>📁 Shared Root</span></div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-ghost btn-sm" onclick="loadDir('share', sharePath, true)">↻ Refresh</button>
          <a id="zip-share" class="btn btn-sm" href="#">📦 ZIP</a>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Size</th><th class="acts">Actions</th></tr></thead>
          <tbody id="tbody-share">
            <tr><td colspan="3" style="text-align:center;padding:32px;color:var(--muted)">
              {'Select a folder to share to see files here.' if not HOST_SHARE else 'Loading…'}
            </td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</main>

<!-- Status bar -->
<footer class="statusbar">
  <span><span class="status-dot"></span> Server live on port {port}</span>
  <span>📥 Saving to: <code>{recv_dir_text}</code></span>
  {'<span>📤 Sharing: <code>' + share_dir_text + '</code></span>' if HOST_SHARE else ''}
</footer>

<!-- ── QR Modal ── -->
<div class="overlay" id="qrModal" onclick="closeOverlay('qrModal')">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-head">
      <div class="modal-title">📱 Scan to Connect</div>
      <button class="modal-close" onclick="closeOverlay('qrModal')" aria-label="Close">×</button>
    </div>
    <p style="font-size:12px;color:var(--muted);margin-bottom:16px">Point your phone or TV camera at this code.</p>
    <div style="text-align:center">
      <div style="background:#fff;padding:16px;border-radius:14px;display:inline-block">
        <img id="qrImg" src="" alt="QR code" style="width:200px;height:200px;display:block">
      </div>
      <p id="qrUrl" style="font-size:12px;color:var(--cyan);margin-top:12px;font-family:monospace"></p>
    </div>
    <div style="margin-top:16px;display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:8px">
      {qr_btns}
    </div>
  </div>
</div>

<!-- ── Help Modal ── -->
<div class="overlay" id="helpModal" onclick="closeOverlay('helpModal')">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-head">
      <div class="modal-title">🛠️ Usage & Troubleshooting</div>
      <button class="modal-close" onclick="closeOverlay('helpModal')" aria-label="Close">×</button>
    </div>

    <div class="acc-item">
      <div class="acc-q" onclick="tog(this)"><span>How does 2-way sharing work?</span><span>▾</span></div>
      <div class="acc-a">
        <strong>Host (you):</strong> Pick a folder on the left panel. Friends open the URL in their browser, go to "Host Shared Folder" tab and can download anything you put in that folder.
        <br><br>
        <strong>Friends:</strong> Drop files into the right panel. Files land on this PC under the receive directory.
        <br><br>
        Neither side needs to install anything. Just a browser.
      </div>
    </div>

    <div class="acc-item">
      <div class="acc-q" onclick="tog(this)"><span>Which URL should my friend open?</span><span>▾</span></div>
      <div class="acc-a">
        <ul>
          <li><strong>Same Wi-Fi or Ethernet:</strong> Use the Wi-Fi or Wired Ethernet link.</li>
          <li><strong>Friend on your Mobile Hotspot:</strong> Use the Mobile Hotspot link (<code>192.168.137.1</code>).</li>
          <li><strong>PC-to-PC cable (no router):</strong> Use the Direct Ethernet link (<code>169.254.x.x</code>).</li>
        </ul>
      </div>
    </div>

    <div class="acc-item">
      <div class="acc-q" onclick="tog(this)"><span>Page says "This site can't be reached"?</span><span>▾</span></div>
      <div class="acc-a">
        Windows Firewall is blocking the connection. A prompt should have appeared asking to allow Python — click <strong>Allow access</strong> and check both Private and Public networks.
      </div>
    </div>

    <div class="acc-item">
      <div class="acc-q" onclick="tog(this)"><span>Transfer paused or stuck? (Large files)</span><span>▾</span></div>
      <div class="acc-a">
        Drop the same file or folder again. Smart Resume detects exactly how many bytes arrived and continues from that point — it never restarts from zero.
      </div>
    </div>

    <div class="acc-item">
      <div class="acc-q" onclick="tog(this)"><span>Hotspot speed is slow (~2 MB/s)?</span><span>▾</span></div>
      <div class="acc-a">
        2.4 GHz hotspot is physically limited. Switch to <strong>5 GHz</strong> in Windows Settings → Network & Internet → Mobile Hotspot → Edit → Band: 5 GHz. Reconnect your friend and use <code>192.168.137.1:{port}</code> for 25–40 MB/s.
        <br><br>
        For maximum speed, plug an Ethernet cable between both PCs and use <code>169.254.x.x:{port}</code> for 60–110 MB/s.
      </div>
    </div>
  </div>
</div>

<!-- ── Toast ── -->
<div class="toast" id="toast" role="status" aria-live="polite"></div>

<!-- ── JS ── -->
<script>
/* ── State ── */
let recvPath  = '';
let sharePath = '';
let activeTab = 'recv';
let uploading = false;

/* ── Tab switching ── */
function switchTab(t) {{
  activeTab = t;
  ['recv','share'].forEach(id => {{
    document.getElementById('panel-'+id).classList.toggle('active', id===t);
    document.getElementById('tab-'+id).classList.toggle('active', id===t);
  }});
  loadDir(t, t==='recv'?recvPath:sharePath, true);
}}

/* ── Drop Zone ── */
function ev(e)  {{ e.preventDefault(); document.getElementById('dropZone').classList.add('over'); }}
function ev2()  {{ document.getElementById('dropZone').classList.remove('over'); }}

async function onDrop(e) {{
  e.preventDefault(); ev2();
  const items = e.dataTransfer.items, entries = [];
  for (let i=0; i<items.length; i++) {{
    const ent = items[i].webkitGetAsEntry?.();
    if (ent) await walk(ent,'',entries);
    else if (e.dataTransfer.files[i]) entries.push({{file:e.dataTransfer.files[i],rel:e.dataTransfer.files[i].name}});
  }}
  if (entries.length) upload(entries);
}}

async function walk(e, base, list) {{
  if (e.isFile) {{
    const f = await new Promise(r=>e.file(r));
    list.push({{file:f, rel:(base?base+'/':'')+f.name}});
  }} else if (e.isDirectory) {{
    const r = e.createReader(), ents = await new Promise(r2=>r.readEntries(r2));
    for (const sub of ents) await walk(sub,(base?base+'/':'')+e.name,list);
  }}
}}

document.getElementById('fileInput').onchange = e =>
  upload(Array.from(e.target.files).map(f=>{{return {{file:f,rel:f.name}}}}) );
document.getElementById('folderInput').onchange = e =>
  upload(Array.from(e.target.files).map(f=>{{return {{file:f,rel:f.webkitRelativePath||f.name}}}}) );

/* ── Upload engine (resumable, 2 parallel) ── */
async function upload(items) {{
  if (!items.length) return;
  uploading = true;
  const card = document.getElementById('progressCard');
  card.style.display = 'block';

  const total = items.length;
  const totalBytes = items.reduce((a,i)=>a+i.file.size,0);
  let doneBytes=0, doneCount=0, skipped=0, lastBytes=0, lastTime=Date.now();

  const CONCURRENCY=2;
  let cur=0;

  async function worker() {{
    while (cur < items.length) {{
      const idx=cur++, {{file,rel}}=items[idx];
      const target = recvPath ? recvPath+'/'+rel : rel;

      let offset=0, skip=false;
      try {{
        const r = await fetch('/api/check?path='+encodeURIComponent(target));
        const d = await r.json();
        if (d.size===file.size) {{ skip=true; skipped++; doneBytes+=file.size; }}
        else if (d.size>0 && d.size<file.size) {{
          offset=d.size; doneBytes+=offset;
          document.getElementById('pFile').textContent='Resuming: '+rel+' from '+(offset/1024/1024).toFixed(1)+' MB';
        }}
      }} catch(_) {{}}

      if (!skip) {{
        document.getElementById('pFile').textContent='Sending: '+rel;
        let startOff=offset;
        for (let attempt=0; attempt<5; attempt++) {{
          try {{ await sendChunk(file, target, startOff, (d)=>{{ doneBytes+=d; startOff+=d; }}); break; }}
          catch(_) {{
            await new Promise(r=>setTimeout(r,1500));
            try {{
              const rr=await fetch('/api/check?path='+encodeURIComponent(target));
              const dd=await rr.json();
              if (dd.size>0) startOff=dd.size;
            }} catch(__) {{}}
          }}
        }}
      }}

      doneCount++;
      const pct = totalBytes>0 ? Math.min(100,Math.round(doneBytes*100/totalBytes)) : 100;
      document.getElementById('pBar').style.width = pct+'%';
      document.getElementById('pStatus').textContent =
        `[${{doneCount}}/${{total}}] ${{pct}}% — ${{skipped}} skipped`;

      const now=Date.now(), dt=(now-lastTime)/1000;
      if (dt>=0.5) {{
        const spd=(doneBytes-lastBytes)/(1024*1024)/dt;
        document.getElementById('pSpeed').textContent=Math.max(0,spd).toFixed(1)+' MB/s';
        lastBytes=doneBytes; lastTime=now;
      }}
    }}
  }}

  await Promise.all(Array.from({{length:Math.min(CONCURRENCY,items.length)}}, worker));

  document.getElementById('pBar').style.width='100%';
  document.getElementById('pStatus').textContent=`✓ Done — ${{total}} items (${{skipped}} skipped)`;
  document.getElementById('pSpeed').textContent='Finished';
  document.getElementById('pFile').textContent='';
  uploading=false;
  setTimeout(()=>card.style.display='none', 4000);
  loadDir('recv', recvPath, true);
}}

function sendChunk(file, path, offset, onDelta) {{
  return new Promise((res,rej)=>{{
    const xhr=new XMLHttpRequest();
    let up=0, last=Date.now();
    const chk=setInterval(()=>{{ if(Date.now()-last>20000){{clearInterval(chk);xhr.abort();rej(new Error('idle'));}} }},4000);
    xhr.upload.onprogress=e=>{{ last=Date.now(); const d=e.loaded-up; up=e.loaded; if(d>0) onDelta(d); }};
    xhr.open('POST','/api/upload?path='+encodeURIComponent(path)+'&offset='+offset,true);
    xhr.onload =()=>{{ clearInterval(chk); xhr.status===200?res():rej(new Error(xhr.status)); }};
    xhr.onerror=()=>{{ clearInterval(chk); rej(new Error('net')); }};
    xhr.onabort=()=>{{ clearInterval(chk); rej(new Error('abort')); }};
    xhr.send(offset>0?file.slice(offset):file);
  }});
}}

/* ── File Explorer ── */
async function loadDir(tab, path, force) {{
  if (tab==='recv') recvPath=path||'';
  else sharePath=path||'';

  const zipEl = document.getElementById('zip-'+tab);
  zipEl.href = '/api/zip?tab='+tab+'&path='+encodeURIComponent(path||'');
  renderBC(tab, path);

  try {{
    const r = await fetch('/api/list?tab='+tab+'&path='+encodeURIComponent(path||''));
    const data = await r.json();
    const tbody = document.getElementById('tbody-'+tab);
    tbody.innerHTML='';

    if (path) {{
      const parent = path.includes('/')?path.substring(0,path.lastIndexOf('/')):'';
      const row = tbody.insertRow();
      row.innerHTML=`<td colspan="3"><a class="file-link folder-link" onclick="loadDir('${{tab}}','${{parent}}',true)">📁 .. (Up)</a></td>`;
    }}

    if (!data.items?.length) {{
      const row=tbody.insertRow();
      row.innerHTML=`<td colspan="3" style="text-align:center;padding:32px;color:var(--muted)">
        ${{tab==='share'?'No files here. Choose a folder to share on the left.':'Nothing received yet. Drop files on the right panel to send.'}}
      </td>`;
      return;
    }}

    for (const item of data.items) {{
      const ip = (path?path+'/':'')+item.name;
      const row = tbody.insertRow();
      if (item.isDir) {{
        row.innerHTML=`
          <td><a class="file-link folder-link" onclick="loadDir('${{tab}}','${{ip}}',true)">📁 ${{item.name}}</a></td>
          <td class="sz">${{item.count}} items</td>
          <td class="acts"><a class="btn btn-sm btn-ghost" href="/api/zip?tab=${{tab}}&path=${{encodeURIComponent(ip)}}">📦 ZIP</a></td>`;
      }} else {{
        const mb=(item.size/1024/1024).toFixed(2);
        row.innerHTML=`
          <td><a class="file-link" href="/download?tab=${{tab}}&path=${{encodeURIComponent(ip)}}" target="_blank">📄 ${{item.name}}</a></td>
          <td class="sz">${{mb}} MB</td>
          <td class="acts"><a class="btn btn-sm btn-ghost" href="/download?tab=${{tab}}&path=${{encodeURIComponent(ip)}}" download>⬇ Download</a></td>`;
      }}
    }}
  }} catch(e) {{ console.error(e); }}
}}

function renderBC(tab, path) {{
  const el = document.getElementById('bc-'+tab);
  el.innerHTML = `<a onclick="loadDir('${{tab}}','',true)">📁 ${{tab==='recv'?'Received':'Shared'}} Root</a>`;
  if (!path) return;
  let acc='';
  path.split('/').forEach(p=>{{
    acc = acc?acc+'/'+p:p;
    const f=acc;
    el.innerHTML += ` / <a onclick="loadDir('${{tab}}','${{f}}',true)">${{p}}</a>`;
  }});
}}

/* ── QR ── */
function openQR() {{
  const url = window.location.origin;
  showQR(url);
  document.getElementById('qrModal').classList.add('open');
}}
function showQR(url) {{
  document.getElementById('qrImg').src='/api/qr?url='+encodeURIComponent(url);
  document.getElementById('qrUrl').textContent=url;
  document.getElementById('qrModal').classList.add('open');
}}

/* ── Host folder picker (native dialog via server) ── */
async function chooseShareFolder() {{
  try {{
    const r = await fetch('/api/pick_folder');
    const d = await r.json();
    if (d.success) {{ location.reload(); }}
    else toast('Could not open folder picker: '+d.error);
  }} catch(e) {{ toast('Error: '+e); }}
}}
async function clearShareFolder() {{
  await fetch('/api/clear_share');
  location.reload();
}}

/* ── Modals ── */
function openModal(id) {{ document.getElementById(id).classList.add('open'); }}
function closeOverlay(id) {{ document.getElementById(id).classList.remove('open'); }}
window.addEventListener('keydown', e=>{{ if(e.key==='Escape') document.querySelectorAll('.overlay.open').forEach(m=>m.classList.remove('open')); }});

/* ── Misc ── */
function copyText(t) {{
  navigator.clipboard.writeText(t).then(()=>toast('Copied: '+t)).catch(()=>prompt('Copy:',t));
}}
function toast(msg) {{
  const el=document.getElementById('toast');
  el.textContent=msg; el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'),2500);
}}
function tog(el) {{ el.nextElementSibling.classList.toggle('open'); }}

/* ── Auto-refresh explorer every 4 s ── */
setInterval(()=>{{ if(!uploading) loadDir(activeTab, activeTab==='recv'?recvPath:sharePath, false); }},4000);

/* ── Initial load ── */
loadDir('recv','',true);
{'loadDir(\'share\',\'\',true);' if HOST_SHARE else ''}
</script>
</body>
</html>"""


# ═══════════════════════════════════════════════════════════════════════════════
#  FOLDER PICKER (Windows-native tkinter dialog, runs server-side)
# ═══════════════════════════════════════════════════════════════════════════════
def pick_folder_dialog():
    """Open a native folder picker on the server PC and return the path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        path = filedialog.askdirectory(title="Choose folder to share with friends")
        root.destroy()
        return path or None
    except Exception as e:
        return None


def safe_path(base_dir, rel):
    """Resolve rel under base_dir, reject traversal."""
    rel = rel.replace("\\", "/").strip("/")
    safe = os.path.normpath(rel).lstrip("/\\")
    full = os.path.abspath(os.path.join(base_dir, safe))
    if not full.startswith(os.path.abspath(base_dir)):
        return None
    return full


# ═══════════════════════════════════════════════════════════════════════════════
#  HTTP HANDLER
# ═══════════════════════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        qs   = urllib.parse.parse_qs(p.query)

        # ── Main page ──
        if path in ("/", "/index.html"):
            body = render_page(SERVER_PORT).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # ── QR ──
        if path == "/api/qr":
            url = qs.get("url", [""])[0] or f"http://127.0.0.1:{SERVER_PORT}"
            if qrcode:
                qr = qrcode.QRCode(box_size=8, border=2)
                qr.add_data(url); qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO(); img.save(buf, "PNG")
                data = buf.getvalue()
                ct = "image/png"
            else:
                data = b"<svg/>"
                ct   = "image/svg+xml"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        # ── Folder picker ──
        if path == "/api/pick_folder":
            global HOST_SHARE
            chosen = pick_folder_dialog()
            if chosen:
                HOST_SHARE = os.path.abspath(chosen)
                self.send_json({"success": True, "path": HOST_SHARE})
            else:
                self.send_json({"success": False, "error": "cancelled"})
            return

        if path == "/api/clear_share":
            HOST_SHARE = ""
            self.send_json({"success": True})
            return

        # ── Check (resume support) ──
        if path == "/api/check":
            rel = qs.get("path", [""])[0]
            fp  = safe_path(UPLOAD_DIR, rel)
            if fp and os.path.isfile(fp):
                self.send_json({"exists": True, "size": os.path.getsize(fp)})
            else:
                self.send_json({"exists": False, "size": 0})
            return

        # ── List directory ──
        if path == "/api/list":
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            base = UPLOAD_DIR if tab == "recv" else HOST_SHARE
            if not base:
                self.send_json({"items": [], "error": "no_share"})
                return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_json({"items": []})
                return
            items = []
            try:
                for name in sorted(os.listdir(target),
                                   key=lambda x: (not os.path.isdir(os.path.join(target, x)), x.lower())):
                    fp = os.path.join(target, name)
                    if os.path.isdir(fp):
                        items.append({"name": name, "isDir": True, "count": len(os.listdir(fp))})
                    else:
                        items.append({"name": name, "isDir": False, "size": os.path.getsize(fp)})
            except Exception:
                pass
            self.send_json({"items": items})
            return

        # ── Download ──
        if path == "/download":
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            base = UPLOAD_DIR if tab == "recv" else HOST_SHARE
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
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(fp)}"')
            self.end_headers()
            with open(fp, "rb") as f:
                while chunk := f.read(1024*1024):
                    self.wfile.write(chunk)
            return

        # ── ZIP download ──
        if path == "/api/zip":
            tab = qs.get("tab", ["recv"])[0]
            rel = qs.get("path", [""])[0]
            base = UPLOAD_DIR if tab == "recv" else HOST_SHARE
            if not base:
                self.send_response(404); self.end_headers(); return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_response(404); self.end_headers(); return
            name = os.path.basename(target) + ".zip" if rel else "turboshare.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(target):
                    for f in files:
                        fp2 = os.path.join(root, f)
                        zf.write(fp2, os.path.relpath(fp2, target))
            self.wfile.write(buf.getvalue())
            return

        self.send_response(404); self.end_headers()

    def do_POST(self):
        p  = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        if p.path != "/api/upload":
            self.send_response(404); self.end_headers(); return

        rel    = qs.get("path",   ["upload"])[0]
        offset = int(qs.get("offset", [0])[0])
        fp     = safe_path(UPLOAD_DIR, rel)
        if not fp:
            self.send_response(403); self.end_headers(); return

        os.makedirs(os.path.dirname(fp), exist_ok=True)
        cl   = int(self.headers.get("Content-Length", 0))
        done = 0
        mode = "ab" if offset > 0 else "wb"
        with open(fp, mode) as f:
            while done < cl:
                chunk = self.rfile.read(min(1024*1024, cl-done))
                if not chunk: break
                f.write(chunk)
                done += len(chunk)

        self.send_json({"success": True, "saved": rel, "bytes": done})

    def log_message(self, *_):
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  STARTUP
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    global UPLOAD_DIR, SERVER_PORT

    default = r"D:\TurboShare" if os.path.exists("D:\\") else os.path.join(
        os.path.expanduser("~"), "Downloads", "TurboShare")

    print("=" * 62)
    print("   ⚡ TurboShare — 2-Way Cross-Device File Transfer Hub")
    print("=" * 62)

    if len(sys.argv) > 1:
        chosen = sys.argv[1].strip().strip("'\"")
    else:
        print(f"\nDefault save folder: {default}")
        try:
            inp = input(f"Where should received files go? [Enter for default]: ").strip().strip("'\"")
            chosen = inp or default
        except (EOFError, KeyboardInterrupt):
            chosen = default

    UPLOAD_DIR = os.path.abspath(chosen)
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except Exception:
        UPLOAD_DIR = os.path.abspath("./TurboShare_Received")
        os.makedirs(UPLOAD_DIR, exist_ok=True)

    di = {"free_gb": "?"}
    try:
        _, _, free = shutil.disk_usage(UPLOAD_DIR)
        di["free_gb"] = f"{free/1024**3:.1f}"
    except Exception:
        pass

    print(f"\n✓ Receiving files to : {UPLOAD_DIR}")
    print(f"✓ Free disk space    : {di['free_gb']} GB")
    print(f"✓ Host share folder  : (choose in browser → 'Choose Folder')")

    ifaces = get_network_interfaces()
    primary = next((f"http://{i['ip']}:{SERVER_PORT}" for i in ifaces
                    if "Wi-Fi" in i["category"] or "Ethernet" in i["category"]), None)

    print("\n" + "-"*62)
    print("  CONNECT FROM YOUR DEVICES")
    print("-"*62)
    for i in ifaces:
        print(f"  {i['icon']} {i['category']:<26} → http://{i['ip']}:{SERVER_PORT}")
    print("-"*62)

    if qrcode and primary:
        print(f"\nQR Code → {primary}\n")
        try:
            qr = qrcode.QRCode()
            qr.add_data(primary)
            qr.print_ascii(invert=True)
        except Exception:
            pass

    print(f"\n🚀 TurboShare live on port {SERVER_PORT}. Press Ctrl+C to stop.\n")

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", SERVER_PORT), Handler)
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    except OSError as e:
        print(f"Error: {e}")


if __name__ == "__main__":
    main()
