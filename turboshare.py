import os
import sys
import io
import json
import socket
import shutil
import zipfile
import mimetypes
import urllib.parse
import html as html_module
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# ── UTF-8 console ─────────────────────────────────────────────────────────────
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

try:
    import psutil
except ImportError:
    psutil = None

try:
    import qrcode
except ImportError:
    qrcode = None

UPLOAD_DIR  = ""
HOST_SHARE  = ""
SERVER_PORT = 8080


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
                            cat, pri = "Virtual / WSL", 9
                        elif ip.startswith("192.168.137."):
                            cat, pri = "Mobile Hotspot", 2
                        elif "wi-fi" in lo or "wireless" in lo or "wlan" in lo:
                            cat, pri = "Wi-Fi", 1
                        elif "ethernet" in lo or "eth" in lo:
                            cat, pri = "Direct Ethernet", 3 if ip.startswith("169.254.") else 3
                        elif "bluetooth" in lo:
                            cat, pri = "Bluetooth", 10
                        else:
                            cat, pri = "LAN", 8
                        interfaces.append(dict(ip=ip, name=name, category=cat, priority=pri))
        except Exception:
            pass
    if not interfaces:
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if not ip.startswith("127.") and ip not in seen:
                    interfaces.append(dict(ip=ip, name="Network", category="LAN", priority=5))
                    seen.add(ip)
        except Exception:
            pass
    interfaces.sort(key=lambda x: x["priority"])
    return interfaces


def disk_info(path):
    try:
        total, used, free = shutil.disk_usage(path)
        return dict(free_gb=f"{free/1024**3:.1f}", total_gb=f"{total/1024**3:.1f}", pct=int(used*100//total))
    except Exception:
        return dict(free_gb="?", total_gb="?", pct=0)


def render_page(port):
    ifaces  = get_network_interfaces()
    recv_di = disk_info(UPLOAD_DIR)
    share_di = disk_info(HOST_SHARE) if HOST_SHARE else {}

    # Build network rows
    net_rows = []
    for i in ifaces:
        url = f"http://{i['ip']}:{port}"
        net_rows.append(
            f'<div class="net-row">'
            f'<span class="net-cat">{html_module.escape(i["category"])}</span>'
            f'<span class="net-url" id="url-{i["ip"]}">{html_module.escape(url)}</span>'
            f'<button class="act-btn" onclick="copyText(\'{url}\')">copy</button>'
            f'<button class="act-btn" onclick="showQR(\'{url}\')">qr</button>'
            f'</div>'
        )

    # QR modal buttons
    qr_btns = "".join(
        f'<button class="qr-opt" onclick="showQR(\'http://{i["ip"]}:{port}\')">'
        f'{html_module.escape(i["category"])}</button>'
        for i in ifaces
    )

    share_path_text = html_module.escape(HOST_SHARE) if HOST_SHARE else ""
    recv_path_text  = html_module.escape(UPLOAD_DIR)
    share_badge_class = "" if HOST_SHARE else " empty"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5">
<title>TurboShare</title>
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=Geist+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ── Tokens ─────────────────────────────────────────────────────────── */
:root {{
  --ink:      #f0ede6;
  --ink-dim:  #8b8680;
  --ink-sub:  #5a5752;
  --surface:  #17140f;
  --surface2: #1f1c16;
  --surface3: #26221b;
  --border:   rgba(240,237,230,.08);
  --border-hi:rgba(240,237,230,.18);
  --lime:     #a3e635;
  --lime-dim: rgba(163,230,53,.18);
  --amber:    #d4a843;
  --red:      #e05252;
  --r:        6px;
}}
*,*::before,*::after {{ box-sizing:border-box; margin:0; padding:0; }}
html {{ background:var(--surface); color:var(--ink); }}
body {{
  font-family:'Space Grotesk',system-ui,sans-serif;
  background:var(--surface);
  min-height:100dvh;
  display:grid;
  grid-template-rows:auto 1fr auto;
  font-size:14px;
  line-height:1.5;
}}
a {{ color:inherit; text-decoration:none; }}
code, .mono {{
  font-family:'Geist Mono',ui-monospace,monospace;
  font-size:.82em;
  color:var(--ink-dim);
}}

/* ── Layout ──────────────────────────────────────────────────────────── */
.wrap {{ max-width:960px; margin:0 auto; padding:0 20px; width:100%; }}

/* ── Header ──────────────────────────────────────────────────────────── */
header {{
  border-bottom:1px solid var(--border);
  padding:14px 0;
  position:sticky; top:0; z-index:200;
  background:rgba(23,20,15,.92);
  backdrop-filter:blur(12px);
}}
.header-inner {{
  display:flex; align-items:center; justify-content:space-between; gap:16px;
}}
.wordmark {{
  font-size:15px; font-weight:600; letter-spacing:-.02em;
  display:flex; align-items:center; gap:8px;
}}
.status-dot {{
  width:6px; height:6px; border-radius:50%;
  background:var(--lime); box-shadow:0 0 8px var(--lime);
}}
.header-meta {{ font-size:12px; color:var(--ink-sub); font-family:'Geist Mono',monospace; }}
.nav-acts {{ display:flex; gap:6px; }}

/* ── Buttons ──────────────────────────────────────────────────────────── */
.btn {{
  display:inline-flex; align-items:center; gap:6px;
  padding:7px 14px; border-radius:var(--r);
  font-size:13px; font-weight:500; font-family:inherit;
  cursor:pointer; border:1px solid var(--border-hi);
  background:var(--surface3); color:var(--ink);
  transition:background .12s, border-color .12s, transform .1s;
  white-space:nowrap; min-height:34px;
}}
.btn:hover {{ background:var(--surface2); border-color:rgba(240,237,230,.28); }}
.btn:active {{ transform:scale(.98); }}
.btn-lime {{
  background:var(--lime); color:#0a0a00;
  border-color:transparent; font-weight:600;
  box-shadow:0 0 16px rgba(163,230,53,.25);
}}
.btn-lime:hover {{ background:#b5f032; border-color:transparent; }}
.act-btn {{
  padding:3px 9px; border-radius:4px;
  font-size:11px; font-weight:500; font-family:'Geist Mono',monospace;
  letter-spacing:.04em; cursor:pointer;
  background:var(--surface3); border:1px solid var(--border);
  color:var(--ink-dim); transition:all .12s; min-height:24px;
}}
.act-btn:hover {{ color:var(--ink); border-color:var(--border-hi); }}

/* ── Main ─────────────────────────────────────────────────────────────── */
main {{ padding:28px 0; display:flex; flex-direction:column; gap:20px; }}

/* ── Section label ───────────────────────────────────────────────────── */
.sec-label {{
  font-size:10px; font-weight:600; letter-spacing:.12em;
  text-transform:uppercase; color:var(--ink-sub);
  margin-bottom:12px;
}}

/* ── Network panel ────────────────────────────────────────────────────── */
.net-panel {{
  background:var(--surface2); border:1px solid var(--border);
  border-radius:var(--r); overflow:hidden;
}}
.net-head {{
  padding:11px 16px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
}}
.net-body {{ padding:4px 0; }}
.net-row {{
  display:flex; align-items:center; gap:10px;
  padding:9px 16px; border-bottom:1px solid var(--border);
  transition:background .1s;
}}
.net-row:last-child {{ border-bottom:none; }}
.net-row:hover {{ background:rgba(240,237,230,.02); }}
.net-cat {{
  font-size:11px; color:var(--ink-sub); min-width:130px;
  font-weight:500; letter-spacing:.03em;
}}
.net-url {{
  font-family:'Geist Mono',monospace; font-size:12px;
  color:var(--ink-dim); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}
.net-row:hover .net-url {{ color:var(--ink); }}

/* ── Two-column action zone ───────────────────────────────────────────── */
.action-grid {{
  display:grid; grid-template-columns:1fr 1fr; gap:16px;
}}
@media(max-width:640px) {{ .action-grid {{ grid-template-columns:1fr; }} }}

.zone {{
  background:var(--surface2); border:1px solid var(--border);
  border-radius:var(--r); padding:20px;
  display:flex; flex-direction:column; gap:14px;
}}
.zone-title {{ font-size:15px; font-weight:600; letter-spacing:-.015em; }}
.zone-sub {{ font-size:12px; color:var(--ink-dim); line-height:1.55; }}
.zone-share {{ border-color:rgba(212,168,67,.25); }}
.zone-recv  {{ border-color:rgba(163,230,53,.15); }}

/* Folder display */
.folder-row {{
  display:flex; align-items:center; gap:8px;
  background:var(--surface3); border:1px solid var(--border);
  border-radius:var(--r); padding:9px 12px;
  min-height:40px;
}}
.folder-path {{
  font-family:'Geist Mono',monospace; font-size:12px;
  color:var(--amber); flex:1; word-break:break-all;
}}
.folder-empty {{ color:var(--ink-sub); font-style:normal; }}

/* Drop zone */
.drop-zone {{
  border:1px dashed var(--border-hi); border-radius:var(--r);
  padding:28px 16px; text-align:center; cursor:pointer;
  transition:all .15s; flex:1; display:flex; flex-direction:column;
  align-items:center; justify-content:center; gap:8px;
  min-height:110px; position:relative; overflow:hidden;
}}
.drop-zone:hover, .drop-zone.over {{
  background:rgba(163,230,53,.04); border-color:var(--lime);
}}
.drop-icon {{
  width:32px; height:32px; stroke:var(--lime); fill:none;
  stroke-width:1.5; stroke-linecap:round; stroke-linejoin:round;
}}
.drop-label {{ font-size:13px; font-weight:500; color:var(--ink); }}
.drop-sub {{ font-size:11px; color:var(--ink-sub); }}
.btn-row {{ display:flex; gap:8px; justify-content:center; flex-wrap:wrap; }}
.recv-meta {{ font-size:11px; color:var(--ink-sub); font-family:'Geist Mono',monospace; }}

/* ── Progress ──────────────────────────────────────────────────────────── */
.progress-zone {{
  display:none;
  background:var(--surface2); border:1px solid var(--border);
  border-radius:var(--r); padding:16px;
  gap:10px; flex-direction:column;
}}
.progress-track {{
  background:var(--surface3); border-radius:3px; height:3px; overflow:hidden;
}}
.progress-fill {{
  background:var(--lime); height:100%; width:0;
  transition:width .08s linear;
  box-shadow:0 0 8px rgba(163,230,53,.5);
}}
.progress-row {{
  display:flex; justify-content:space-between; align-items:center;
  font-size:12px; gap:8px; flex-wrap:wrap;
}}
.progress-status {{ color:var(--ink-dim); }}
.progress-speed {{ font-family:'Geist Mono',monospace; color:var(--lime); font-weight:500; }}
.progress-file {{
  font-size:11px; font-family:'Geist Mono',monospace;
  color:var(--ink-sub); overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
}}

/* ── Explorer ──────────────────────────────────────────────────────────── */
.explorer {{
  background:var(--surface2); border:1px solid var(--border);
  border-radius:var(--r); overflow:hidden;
}}
.explorer-tabs {{
  display:flex; border-bottom:1px solid var(--border);
  background:var(--surface3);
}}
.tab {{
  padding:10px 18px; font-size:12px; font-weight:500; cursor:pointer;
  color:var(--ink-sub); border:none; background:none; border-bottom:2px solid transparent;
  transition:color .12s,border-color .12s; font-family:inherit; letter-spacing:.01em;
}}
.tab.active {{ color:var(--ink); border-bottom-color:var(--lime); }}
.tab:hover:not(.active) {{ color:var(--ink-dim); }}
.tab-badge {{
  display:inline-block; font-size:9px; font-weight:600;
  padding:2px 5px; border-radius:3px; margin-left:5px;
  background:var(--lime-dim); color:var(--lime); vertical-align:middle;
}}
.tab-badge.off {{
  background:rgba(240,237,230,.06); color:var(--ink-sub);
}}
.tab-panel {{ display:none; }}
.tab-panel.active {{ display:block; }}

.explorer-toolbar {{
  padding:10px 16px; border-bottom:1px solid var(--border);
  display:flex; align-items:center; justify-content:space-between;
  flex-wrap:wrap; gap:8px; background:rgba(0,0,0,.15);
}}
.breadcrumb {{
  font-size:12px; font-family:'Geist Mono',monospace;
  color:var(--ink-sub); display:flex; gap:4px; align-items:center; flex-wrap:wrap;
}}
.breadcrumb a {{ color:var(--ink-dim); cursor:pointer; }}
.breadcrumb a:hover {{ color:var(--ink); text-decoration:underline; }}
.breadcrumb span {{ color:var(--ink-sub); }}
.tool-acts {{ display:flex; gap:6px; }}

.table-wrap {{ overflow-x:auto; }}
table {{ width:100%; border-collapse:collapse; }}
th {{
  padding:9px 16px; font-size:10px; font-weight:600;
  letter-spacing:.08em; text-transform:uppercase;
  color:var(--ink-sub); border-bottom:1px solid var(--border);
  text-align:left; background:rgba(0,0,0,.1);
}}
td {{ padding:11px 16px; font-size:13px; border-bottom:1px solid var(--border); vertical-align:middle; }}
tr:last-child td {{ border-bottom:none; }}
tr:hover td {{ background:rgba(240,237,230,.02); }}
.file-name {{
  display:flex; align-items:center; gap:8px; cursor:pointer;
  font-weight:500; color:var(--ink);
}}
.file-name:hover {{ color:var(--lime); }}
.dir-name {{ color:var(--amber); }}
.dir-name:hover {{ color:#e8bc56; }}
.file-sz {{ font-family:'Geist Mono',monospace; font-size:11px; color:var(--ink-sub); white-space:nowrap; }}
.td-act {{ text-align:right; white-space:nowrap; }}
.empty-state {{
  padding:36px 16px; text-align:center; color:var(--ink-sub);
  font-size:12px; line-height:1.8;
}}

/* ── Footer ────────────────────────────────────────────────────────────── */
footer {{
  border-top:1px solid var(--border); padding:12px 0;
  font-size:11px; color:var(--ink-sub); font-family:'Geist Mono',monospace;
}}
.footer-inner {{
  display:flex; gap:20px; flex-wrap:wrap; align-items:center;
}}

/* ── QR overlay ────────────────────────────────────────────────────────── */
.overlay {{
  position:fixed; inset:0;
  background:rgba(10,8,5,.85); backdrop-filter:blur(8px);
  z-index:500; display:none; align-items:center; justify-content:center; padding:20px;
}}
.overlay.open {{ display:flex; }}
.modal {{
  background:var(--surface2); border:1px solid var(--border-hi);
  border-radius:10px; padding:24px; max-width:420px; width:100%;
  box-shadow:0 24px 48px rgba(0,0,0,.6);
  max-height:90vh; overflow-y:auto;
}}
.modal-head {{
  display:flex; align-items:center; justify-content:space-between; margin-bottom:18px;
}}
.modal-title {{ font-size:16px; font-weight:600; }}
.close-btn {{
  background:none; border:none; color:var(--ink-sub);
  font-size:20px; cursor:pointer; padding:2px 6px; border-radius:4px;
}}
.close-btn:hover {{ color:var(--ink); background:var(--surface3); }}
.qr-wrap {{
  background:#fff; padding:14px; border-radius:8px;
  display:flex; align-items:center; justify-content:center; margin-bottom:12px;
}}
.qr-wrap img {{ width:180px; height:180px; display:block; }}
.qr-url {{ font-family:'Geist Mono',monospace; font-size:11px; color:var(--ink-dim); text-align:center; margin-bottom:14px; word-break:break-all; }}
.qr-opts {{ display:flex; gap:6px; flex-wrap:wrap; }}
.qr-opt {{
  padding:5px 10px; font-size:11px; font-weight:500;
  background:var(--surface3); border:1px solid var(--border);
  color:var(--ink-dim); cursor:pointer; border-radius:4px;
  font-family:inherit; transition:all .12s;
}}
.qr-opt:hover {{ color:var(--ink); border-color:var(--border-hi); }}

/* ── Help accordion ────────────────────────────────────────────────────── */
.acc {{ border:1px solid var(--border); border-radius:var(--r); margin-bottom:6px; overflow:hidden; }}
.acc-q {{
  padding:11px 14px; font-size:13px; font-weight:500;
  cursor:pointer; display:flex; justify-content:space-between; align-items:center;
  background:none; border:none; width:100%; text-align:left; color:var(--ink); font-family:inherit;
}}
.acc-q:hover {{ background:var(--surface3); }}
.acc-a {{ display:none; padding:0 14px; font-size:12px; color:var(--ink-dim); line-height:1.7; }}
.acc-a.open {{ display:block; padding-bottom:12px; }}
.acc-a ul {{ margin:6px 0 0 16px; }}
.acc-a li {{ margin-bottom:4px; }}

/* ── Toast ─────────────────────────────────────────────────────────────── */
.toast {{
  position:fixed; bottom:20px; right:20px;
  background:var(--surface3); color:var(--ink);
  padding:8px 14px; border-radius:var(--r);
  border:1px solid var(--border-hi); font-size:12px; font-weight:500;
  box-shadow:0 8px 24px rgba(0,0,0,.4);
  opacity:0; transform:translateY(8px);
  transition:opacity .2s, transform .2s; z-index:999;
  pointer-events:none; font-family:'Geist Mono',monospace;
}}
.toast.show {{ opacity:1; transform:translateY(0); }}

@media(max-width:600px) {{
  .wrap {{ padding:0 14px; }}
  th:nth-child(2), td:nth-child(2) {{ display:none; }}
  .net-cat {{ min-width:90px; }}
}}
</style>
</head>
<body>

<header>
  <div class="wrap header-inner">
    <div class="wordmark">
      <span class="status-dot" title="Server running"></span>
      TurboShare
    </div>
    <div class="header-meta mono">:{port}</div>
    <div class="nav-acts">
      <button class="btn" onclick="openModal('qrModal')">QR</button>
      <button class="btn" onclick="openModal('helpModal')">Help</button>
    </div>
  </div>
</header>

<main>
<div class="wrap" style="display:flex;flex-direction:column;gap:20px">

  <!-- Network -->
  <section>
    <div class="sec-label">Connect — open any URL on any device on your network</div>
    <div class="net-panel">
      <div class="net-head">
        <span class="mono" style="font-size:12px;color:var(--ink-sub)">http://&lt;address&gt;:{port}</span>
        <span style="font-size:11px;color:var(--ink-sub)">Share these with your friend</span>
      </div>
      <div class="net-body">
        {''.join(net_rows)}
      </div>
    </div>
  </section>

  <!-- Action zones -->
  <section>
    <div class="sec-label">Transfer</div>
    <div class="action-grid">

      <!-- Share zone -->
      <div class="zone zone-share">
        <div>
          <div class="zone-title">Share from this PC</div>
          <div class="zone-sub" style="margin-top:4px">Pick a folder. Anyone with the URL can browse and download it — no install needed.</div>
        </div>
        <div class="folder-row" id="shareFolderRow">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--amber)" stroke-width="1.5" stroke-linecap="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
          <span class="folder-path {'' if HOST_SHARE else 'folder-empty'}" id="shareFolderPath">
            {share_path_text if HOST_SHARE else 'no folder selected'}
          </span>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn" onclick="chooseShareFolder()" style="border-color:rgba(212,168,67,.4);color:var(--amber)">
            choose folder
          </button>
          {'<button class="btn" onclick="clearShare()" style="color:var(--red);border-color:rgba(224,82,82,.3)">clear</button>' if HOST_SHARE else ''}
        </div>
        {'<div class="recv-meta">' + share_di.get("free_gb","?") + ' GB free</div>' if HOST_SHARE else ''}
      </div>

      <!-- Receive zone -->
      <div class="zone zone-recv">
        <div>
          <div class="zone-title">Receive files</div>
          <div class="zone-sub" style="margin-top:4px">Drop files or folders here. Large files resume automatically if interrupted.</div>
        </div>
        <div class="drop-zone" id="dropZone"
             onclick="document.getElementById('fileInput').click()"
             ondragover="onDragOver(event)" ondragleave="onDragLeave()" ondrop="onDrop(event)">
          <svg class="drop-icon" viewBox="0 0 24 24">
            <polyline points="16 16 12 12 8 16"/><line x1="12" y1="12" x2="12" y2="21"/>
            <path d="M20.39 18.39A5 5 0 0 0 18 9h-1.26A8 8 0 1 0 3 16.3"/>
          </svg>
          <div class="drop-label">drop files or folders</div>
          <div class="drop-sub">PC · phone · TV · console — anything with a browser</div>
        </div>
        <div class="btn-row">
          <button class="btn" onclick="document.getElementById('fileInput').click()">files</button>
          <button class="btn btn-lime" onclick="document.getElementById('folderInput').click()">folder</button>
        </div>
        <div class="recv-meta">saving to <span style="color:var(--lime)">{recv_path_text}</span> — {recv_di['free_gb']} GB free</div>
        <input type="file" id="fileInput"   multiple style="display:none">
        <input type="file" id="folderInput" webkitdirectory multiple style="display:none">
      </div>
    </div>
  </section>

  <!-- Progress -->
  <div class="progress-zone" id="progressZone">
    <div class="progress-track"><div class="progress-fill" id="pBar"></div></div>
    <div class="progress-row">
      <span class="progress-status" id="pStatus">preparing…</span>
      <span class="progress-speed" id="pSpeed">— MB/s</span>
    </div>
    <div class="progress-file" id="pFile"></div>
  </div>

  <!-- File explorer -->
  <section class="explorer">
    <div class="explorer-tabs">
      <button class="tab active" id="tab-recv" onclick="switchTab('recv')">
        received
        <span class="tab-badge" id="badge-recv">live</span>
      </button>
      <button class="tab" id="tab-share" onclick="switchTab('share')">
        shared folder
        <span class="tab-badge {'off' if not HOST_SHARE else ''}" id="badge-share">{'not set' if not HOST_SHARE else 'live'}</span>
      </button>
    </div>

    <div class="tab-panel active" id="panel-recv">
      <div class="explorer-toolbar">
        <div class="breadcrumb" id="bc-recv">
          <a onclick="loadDir('recv','',true)">root</a>
        </div>
        <div class="tool-acts">
          <button class="act-btn" onclick="loadDir('recv',recvPath,true)">refresh</button>
          <a id="zip-recv" class="act-btn" href="#">zip all</a>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Size</th><th style="text-align:right">Actions</th></tr></thead>
          <tbody id="tbody-recv"><tr><td colspan="3"><div class="empty-state">loading…</div></td></tr></tbody>
        </table>
      </div>
    </div>

    <div class="tab-panel" id="panel-share">
      <div class="explorer-toolbar">
        <div class="breadcrumb" id="bc-share">
          <a onclick="loadDir('share','',true)">root</a>
        </div>
        <div class="tool-acts">
          <button class="act-btn" onclick="loadDir('share',sharePath,true)">refresh</button>
          <a id="zip-share" class="act-btn" href="#">zip all</a>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Name</th><th>Size</th><th style="text-align:right">Actions</th></tr></thead>
          <tbody id="tbody-share">
            <tr><td colspan="3"><div class="empty-state">{'choose a folder to share on the left' if not HOST_SHARE else 'loading…'}</div></td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>

</div>
</main>

<footer>
  <div class="wrap footer-inner">
    <span>port {port}</span>
    <span>recv → <span style="color:var(--lime)">{recv_path_text}</span></span>
    {'<span>share → <span style="color:var(--amber)">' + html_module.escape(HOST_SHARE) + '</span></span>' if HOST_SHARE else '<span style="color:var(--ink-sub)">share → not set</span>'}
  </div>
</footer>

<!-- QR Modal -->
<div class="overlay" id="qrModal" onclick="closeModal('qrModal')">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-head">
      <div class="modal-title">Scan to connect</div>
      <button class="close-btn" onclick="closeModal('qrModal')">×</button>
    </div>
    <div class="qr-wrap"><img id="qrImg" src="" alt="QR code"></div>
    <div class="qr-url" id="qrUrl"></div>
    <div class="qr-opts">{qr_btns}</div>
  </div>
</div>

<!-- Help Modal -->
<div class="overlay" id="helpModal" onclick="closeModal('helpModal')">
  <div class="modal" onclick="event.stopPropagation()">
    <div class="modal-head">
      <div class="modal-title">Troubleshooting</div>
      <button class="close-btn" onclick="closeModal('helpModal')">×</button>
    </div>

    <div class="acc"><button class="acc-q" onclick="tog(this)"><span>Which URL should my friend use?</span><span>▾</span></button>
    <div class="acc-a"><ul>
      <li><b>Same Wi-Fi or router:</b> Wi-Fi address (192.168.x.x)</li>
      <li><b>Your Mobile Hotspot:</b> 192.168.137.1 — always use this one for hotspot</li>
      <li><b>Direct Ethernet cable:</b> 169.254.x.x — fastest, no router needed</li>
    </ul></div></div>

    <div class="acc"><button class="acc-q" onclick="tog(this)"><span>Page says "can't be reached"?</span><span>▾</span></button>
    <div class="acc-a">Windows Firewall blocked Python. When the prompt appeared, click <b>Allow access</b> and check both Private and Public. Or run in PowerShell as admin:<br><code>netsh advfirewall firewall add rule name="TurboShare" dir=in action=allow protocol=TCP localport={port}</code></div></div>

    <div class="acc"><button class="acc-q" onclick="tog(this)"><span>Transfer stopped — do I restart from zero?</span><span>▾</span></button>
    <div class="acc-a">No. Drop the same file again. Smart Resume checks the exact byte count on disk and continues from that point.</div></div>

    <div class="acc"><button class="acc-q" onclick="tog(this)"><span>Hotspot is slow (~2 MB/s)?</span><span>▾</span></button>
    <div class="acc-a">You're on 2.4 GHz. Switch to 5 GHz: Settings → Network & Internet → Mobile Hotspot → Edit → Band: 5 GHz. Reconnect and reload the page. Expect 20–35 MB/s.<br><br>For maximum speed: plug an Ethernet cable directly between both PCs. No router needed. Expect 60–110 MB/s.</div></div>

    <div class="acc"><button class="acc-q" onclick="tog(this)"><span>"Host Shared Folder" tab shows nothing?</span><span>▾</span></button>
    <div class="acc-a">Click <b>choose folder</b> in the left panel on the host PC. A native Windows folder picker opens. After you pick, the tab becomes active for all connected devices.</div></div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast" role="status"></div>

<script>
/* ── State ── */
let recvPath  = '';
let sharePath = '';
let activeTab = 'recv';
let uploading = false;

/* ── Drag / Drop ── */
function onDragOver(e) {{ e.preventDefault(); document.getElementById('dropZone').classList.add('over'); }}
function onDragLeave()  {{ document.getElementById('dropZone').classList.remove('over'); }}
async function onDrop(e) {{
  e.preventDefault(); onDragLeave();
  const items = e.dataTransfer.items;
  const entries = [];
  for (let i = 0; i < items.length; i++) {{
    const ent = items[i].webkitGetAsEntry?.();
    if (ent) await walk(ent, '', entries);
    else if (e.dataTransfer.files[i]) entries.push({{file: e.dataTransfer.files[i], rel: e.dataTransfer.files[i].name}});
  }}
  if (entries.length) upload(entries);
}}
async function walk(e, base, list) {{
  if (e.isFile) {{
    const f = await new Promise(r => e.file(r));
    list.push({{file: f, rel: (base ? base + '/' : '') + f.name}});
  }} else if (e.isDirectory) {{
    const r = e.createReader();
    const ents = await new Promise(r2 => r.readEntries(r2));
    for (const sub of ents) await walk(sub, (base ? base + '/' : '') + e.name, list);
  }}
}}
document.getElementById('fileInput').onchange   = e => upload(Array.from(e.target.files).map(f => ({{file:f,rel:f.name}})));
document.getElementById('folderInput').onchange = e => upload(Array.from(e.target.files).map(f => ({{file:f,rel:f.webkitRelativePath||f.name}})));

/* ── Upload engine ── */
async function upload(items) {{
  if (!items.length) return;
  uploading = true;
  const pz = document.getElementById('progressZone');
  pz.style.display = 'flex';
  const total = items.length;
  const totalBytes = items.reduce((a, i) => a + i.file.size, 0);
  let doneBytes = 0, doneCount = 0, skipped = 0;
  let lastBytes = 0, lastTime = Date.now();

  async function worker(from) {{
    for (let idx = from; idx < items.length; idx += 2) {{
      const {{file, rel}} = items[idx];
      const target = recvPath ? recvPath + '/' + rel : rel;
      let offset = 0, skip = false;
      try {{
        const r = await fetch('/api/check?path=' + encodeURIComponent(target));
        const d = await r.json();
        if (d.size === file.size) {{ skip = true; skipped++; doneBytes += file.size; }}
        else if (d.size > 0 && d.size < file.size) {{
          offset = d.size; doneBytes += offset;
          document.getElementById('pFile').textContent = 'resuming: ' + rel + ' from ' + (offset/1024/1024).toFixed(1) + ' MB';
        }}
      }} catch(_) {{}}

      if (!skip) {{
        document.getElementById('pFile').textContent = rel;
        let startOff = offset;
        for (let attempt = 0; attempt < 5; attempt++) {{
          try {{
            await sendChunk(file, target, startOff, d => {{ doneBytes += d; startOff += d; }});
            break;
          }} catch(_) {{
            await new Promise(r => setTimeout(r, 1500));
            try {{
              const rr = await fetch('/api/check?path=' + encodeURIComponent(target));
              const dd = await rr.json();
              if (dd.size > 0) startOff = dd.size;
            }} catch(__) {{}}
          }}
        }}
      }}

      doneCount++;
      const pct = totalBytes > 0 ? Math.min(100, Math.round(doneBytes * 100 / totalBytes)) : 100;
      document.getElementById('pBar').style.width = pct + '%';
      document.getElementById('pStatus').textContent = doneCount + '/' + total + ' — ' + pct + '%' + (skipped ? ' (' + skipped + ' skipped)' : '');

      const now = Date.now(), dt = (now - lastTime) / 1000;
      if (dt >= 0.5) {{
        const spd = Math.max(0, (doneBytes - lastBytes) / (1024*1024) / dt);
        document.getElementById('pSpeed').textContent = spd.toFixed(1) + ' MB/s';
        lastBytes = doneBytes; lastTime = now;
      }}
    }}
  }}

  await Promise.all([worker(0), worker(1)]);
  document.getElementById('pBar').style.width = '100%';
  document.getElementById('pStatus').textContent = 'done — ' + total + ' items' + (skipped ? ', ' + skipped + ' skipped' : '');
  document.getElementById('pSpeed').textContent = '—';
  document.getElementById('pFile').textContent = '';
  uploading = false;
  setTimeout(() => {{ pz.style.display = 'none'; document.getElementById('pBar').style.width = '0'; }}, 4000);
  loadDir('recv', recvPath, true);
}}

function sendChunk(file, path, offset, onDelta) {{
  return new Promise((res, rej) => {{
    const xhr = new XMLHttpRequest();
    let up = 0, last = Date.now();
    const timer = setInterval(() => {{ if (Date.now() - last > 20000) {{ clearInterval(timer); xhr.abort(); rej(new Error('idle')); }} }}, 3000);
    xhr.upload.onprogress = e => {{ last = Date.now(); const d = e.loaded - up; up = e.loaded; if (d > 0) onDelta(d); }};
    xhr.open('POST', '/api/upload?path=' + encodeURIComponent(path) + '&offset=' + offset, true);
    xhr.onload  = () => {{ clearInterval(timer); xhr.status === 200 ? res() : rej(new Error(xhr.status)); }};
    xhr.onerror = () => {{ clearInterval(timer); rej(new Error('net')); }};
    xhr.onabort = () => {{ clearInterval(timer); rej(new Error('abort')); }};
    xhr.send(offset > 0 ? file.slice(offset) : file);
  }});
}}

/* ── Explorer ── */
async function loadDir(tab, path, force) {{
  if (tab === 'recv') recvPath = path || '';
  else sharePath = path || '';

  const zipEl = document.getElementById('zip-' + tab);
  zipEl.href = '/api/zip?tab=' + tab + '&path=' + encodeURIComponent(path || '');
  renderBC(tab, path);

  try {{
    const r = await fetch('/api/list?tab=' + tab + '&path=' + encodeURIComponent(path || ''));
    const data = await r.json();
    const tbody = document.getElementById('tbody-' + tab);
    tbody.innerHTML = '';

    if (path) {{
      const parent = path.includes('/') ? path.substring(0, path.lastIndexOf('/')) : '';
      const row = tbody.insertRow();
      row.innerHTML = '<td colspan="3"><a class="file-name dir-name" onclick="loadDir(\'' + tab + '\',\'' + parent + '\',true)">.. parent</a></td>';
    }}

    if (!data.items?.length) {{
      const row = tbody.insertRow();
      row.innerHTML = '<td colspan="3"><div class="empty-state">' +
        (tab === 'share' ? 'choose a folder to share on the left' : 'nothing received yet — drop files on the right to send them here') +
        '</div></td>';
      return;
    }}

    for (const item of data.items) {{
      const ip2 = (path ? path + '/' : '') + item.name;
      const row = tbody.insertRow();
      if (item.isDir) {{
        row.innerHTML = '<td><a class="file-name dir-name" onclick="loadDir(\'' + tab + '\',\'' + ip2.replace(/'/g, "\\'") + '\',true)">' + item.name + '</a></td>' +
          '<td class="file-sz">' + item.count + ' items</td>' +
          '<td class="td-act"><a class="act-btn" href="/api/zip?tab=' + tab + '&path=' + encodeURIComponent(ip2) + '">zip</a></td>';
      }} else {{
        const mb = (item.size / 1024 / 1024).toFixed(2);
        const dl = '/download?tab=' + tab + '&path=' + encodeURIComponent(ip2);
        row.innerHTML = '<td><a class="file-name" href="' + dl + '" target="_blank">' + item.name + '</a></td>' +
          '<td class="file-sz">' + mb + ' MB</td>' +
          '<td class="td-act"><a class="act-btn" href="' + dl + '" download>download</a></td>';
      }}
    }}
  }} catch(e) {{ console.error(e); }}
}}

function renderBC(tab, path) {{
  const el = document.getElementById('bc-' + tab);
  el.innerHTML = '<a onclick="loadDir(\'' + tab + '\',\'\',true)">root</a>';
  if (!path) return;
  let acc = '';
  path.split('/').forEach(p => {{
    acc = acc ? acc + '/' + p : p;
    const f = acc;
    el.innerHTML += '<span>/</span><a onclick="loadDir(\'' + tab + '\',\'' + f.replace(/'/g, "\\'") + '\',true)">' + p + '</a>';
  }});
}}

/* ── Tabs ── */
function switchTab(t) {{
  activeTab = t;
  ['recv','share'].forEach(id => {{
    document.getElementById('panel-' + id).classList.toggle('active', id === t);
    document.getElementById('tab-' + id).classList.toggle('active', id === t);
  }});
  loadDir(t, t === 'recv' ? recvPath : sharePath, true);
}}

/* ── Host folder picker ── */
async function chooseShareFolder() {{
  try {{
    const r = await fetch('/api/pick_folder');
    const d = await r.json();
    if (d.success) location.reload();
    else toast('picker cancelled or unavailable');
  }} catch(e) {{ toast('error: ' + e); }}
}}
async function clearShare() {{
  await fetch('/api/clear_share');
  location.reload();
}}

/* ── QR ── */
function openModal(id) {{ document.getElementById(id).classList.add('open'); }}
function closeModal(id) {{ document.getElementById(id).classList.remove('open'); }}
function showQR(url) {{
  document.getElementById('qrImg').src = '/api/qr?url=' + encodeURIComponent(url);
  document.getElementById('qrUrl').textContent = url;
  openModal('qrModal');
}}

/* ── Util ── */
function copyText(t) {{
  navigator.clipboard.writeText(t).then(() => toast('copied')).catch(() => prompt('Copy:', t));
}}
function toast(msg) {{
  const el = document.getElementById('toast');
  el.textContent = msg; el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2200);
}}
function tog(btn) {{ btn.nextElementSibling.classList.toggle('open'); }}
window.addEventListener('keydown', e => {{
  if (e.key === 'Escape') document.querySelectorAll('.overlay.open').forEach(m => m.classList.remove('open'));
}});

/* ── Auto-refresh ── */
setInterval(() => {{ if (!uploading) loadDir(activeTab, activeTab === 'recv' ? recvPath : sharePath, false); }}, 4000);

/* ── Init ── */
loadDir('recv', '', true);
{'loadDir(\'share\',\'\',true);' if HOST_SHARE else ''}
</script>
</body>
</html>"""


def pick_folder_dialog():
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        path = filedialog.askdirectory(title="Choose folder to share with friends")
        root.destroy()
        return path or None
    except Exception:
        return None


def safe_path(base_dir, rel):
    rel = rel.replace("\\", "/").strip("/")
    safe = os.path.normpath(rel).lstrip("/\\")
    full = os.path.abspath(os.path.join(base_dir, safe))
    if not full.startswith(os.path.abspath(base_dir)):
        return None
    return full


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p  = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(p.query)
        path = p.path

        if path in ("/", "/index.html"):
            body = render_page(SERVER_PORT).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/qr":
            url = qs.get("url", [""])[0] or f"http://127.0.0.1:{SERVER_PORT}"
            if qrcode:
                qr = qrcode.QRCode(box_size=8, border=2)
                qr.add_data(url); qr.make(fit=True)
                img = qr.make_image(fill_color="black", back_color="white")
                buf = io.BytesIO(); img.save(buf, "PNG")
                data = buf.getvalue(); ct = "image/png"
            else:
                data = b"<svg/>"; ct = "image/svg+xml"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

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

        if path == "/api/check":
            rel = qs.get("path", [""])[0]
            fp  = safe_path(UPLOAD_DIR, rel)
            if fp and os.path.isfile(fp):
                self.send_json({"exists": True, "size": os.path.getsize(fp)})
            else:
                self.send_json({"exists": False, "size": 0})
            return

        if path == "/api/list":
            tab  = qs.get("tab", ["recv"])[0]
            rel  = qs.get("path", [""])[0]
            base = UPLOAD_DIR if tab == "recv" else HOST_SHARE
            if not base:
                self.send_json({"items": []}); return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_json({"items": []}); return
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

        if path == "/download":
            tab  = qs.get("tab", ["recv"])[0]
            rel  = qs.get("path", [""])[0]
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
            self.send_header("Content-Disposition", f'attachment; filename="{os.path.basename(fp)}"')
            self.end_headers()
            with open(fp, "rb") as f:
                while chunk := f.read(1024*1024):
                    self.wfile.write(chunk)
            return

        if path == "/api/zip":
            tab  = qs.get("tab", ["recv"])[0]
            rel  = qs.get("path", [""])[0]
            base = UPLOAD_DIR if tab == "recv" else HOST_SHARE
            if not base:
                self.send_response(404); self.end_headers(); return
            target = safe_path(base, rel) if rel else os.path.abspath(base)
            if not target or not os.path.isdir(target):
                self.send_response(404); self.end_headers(); return
            name = (os.path.basename(target) + ".zip") if rel else "turboshare.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.end_headers()
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for root, _, files in os.walk(target):
                    for f2 in files:
                        fp2 = os.path.join(root, f2)
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
                f.write(chunk); done += len(chunk)
        self.send_json({"success": True, "saved": rel, "bytes": done})

    def log_message(self, *_):
        pass


def main():
    global UPLOAD_DIR, SERVER_PORT
    default = r"D:\TurboShare" if os.path.exists("D:\\") else os.path.join(os.path.expanduser("~"), "Downloads", "TurboShare")

    print("="*58)
    print("  TurboShare — Cross-Device File Transfer")
    print("="*58)

    if len(sys.argv) > 1:
        chosen = sys.argv[1].strip().strip("'\"")
    else:
        print(f"\nDefault receive folder: {default}")
        try:
            inp = input("Receive folder path [Enter for default]: ").strip().strip("'\"")
            chosen = inp or default
        except (EOFError, KeyboardInterrupt):
            chosen = default

    UPLOAD_DIR = os.path.abspath(chosen)
    try:
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    except Exception:
        UPLOAD_DIR = os.path.abspath("./TurboShare_Received")
        os.makedirs(UPLOAD_DIR, exist_ok=True)

    try:
        _, _, free = shutil.disk_usage(UPLOAD_DIR)
        free_gb = f"{free/1024**3:.1f}"
    except Exception:
        free_gb = "?"

    print(f"\n  recv  → {UPLOAD_DIR}  ({free_gb} GB free)")
    print(f"  share → pick in browser after opening\n")

    ifaces = get_network_interfaces()
    print("-"*58)
    for i in ifaces:
        print(f"  {i['category']:<22} http://{i['ip']}:{SERVER_PORT}")
    print("-"*58)

    primary = next((f"http://{i['ip']}:{SERVER_PORT}" for i in ifaces if i["priority"] <= 3), None)
    if qrcode and primary:
        print(f"\n  QR → {primary}\n")
        try:
            qr = qrcode.QRCode()
            qr.add_data(primary)
            qr.print_ascii(invert=True)
        except Exception:
            pass

    print(f"\n  Listening on :{SERVER_PORT} — Ctrl+C to stop\n")
    try:
        srv = ThreadingHTTPServer(("0.0.0.0", SERVER_PORT), Handler)
        srv.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
    except OSError as e:
        print(f"Error: {e}\nIs another TurboShare already running?")


if __name__ == "__main__":
    main()
