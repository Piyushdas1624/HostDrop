<p align="center">
  <img src="https://img.shields.io/badge/⚡_TurboShare-Cross--Device_File_Transfer-38bdf8?style=for-the-badge&labelColor=0f172a" alt="TurboShare">
</p>

<p align="center">
  <strong>No cloud. No accounts. No USB. Just open a browser.</strong><br>
  Transfer files between any two devices on the same network at full LAN speed.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8%2B-3776ab?style=flat-square&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-0ea5e9?style=flat-square">
  <img src="https://img.shields.io/badge/License-MIT-10b981?style=flat-square">
  <img src="https://img.shields.io/badge/No%20Dependencies-Zero%20Install%20Required-f59e0b?style=flat-square">
</p>

---

## What is TurboShare?

TurboShare is a **self-hosted, zero-dependency file transfer hub** that runs entirely in your browser. You run one Python script, and every device on your network — phones, laptops, smart TVs, gaming consoles — can instantly share and download files with no software to install on the other end.

| Feature | Details |
|---|---|
| 🔄 **2-Way Sharing** | Host shares a folder for download AND receives uploads simultaneously |
| 📁 **Host Folder Picker** | Click "Choose Folder" in the browser — native folder dialog opens on the host PC |
| ⚡ **Full LAN Speed** | Ethernet: ~60–110 MB/s · Wi-Fi 5 GHz: ~25–40 MB/s · Hotspot 2.4 GHz: ~2–4 MB/s |
| 🔁 **Smart Resume** | Interrupted transfers auto-resume from the exact byte — never restart from zero |
| 📱 **Any Device** | Works on iOS, Android, Xbox, PlayStation, Smart TV, fridge, anything with a browser |
| 🔌 **Direct Ethernet** | PC-to-PC cable (no router) auto-detected with APIPA `169.254.x.x` link |
| 📡 **QR Codes** | Per-interface QR codes generated for fast mobile connect |
| 🗂️ **Folder Transfer** | Full directory trees preserved on upload and download (ZIP or browse) |

---

## Quick Start

### Option 1 — Double-click (Windows)

1. Download this repo.
2. Double-click **`Run_TurboShare.bat`**.
3. Enter the folder where you want to receive files (or press Enter for the default).
4. Open the URL shown in the terminal on **any device's browser**.

### Option 2 — Command line

```bash
# Install optional libraries (for QR codes and better network detection)
pip install qrcode[pil] psutil

# Run — receive files into D:\MyShared
python turboshare.py "D:\MyShared"
```

---

## How 2-Way Sharing Works

```
  ┌─────────────────────────────────────────────────────────┐
  │         YOUR PC  (host — runs turboshare.py)            │
  │                                                         │
  │   📤 HOST SHARE PANEL        📥 RECEIVE PANEL          │
  │   Pick any folder →          Friends drop files here →  │
  │   friends can download it    files land on your disk    │
  └───────────────────────┬─────────────────────────────────┘
                          │  Local network (LAN / Hotspot / Cable)
            ┌─────────────┴────────────┐
            │      FRIEND'S DEVICE     │
            │  Just opens the URL in   │
            │  a browser — no install  │
            └──────────────────────────┘
```

**Nobody else needs to run any code.**

- **To share files to a friend:** Click **📂 Choose Folder** in the browser — a native folder picker opens on your PC. Pick any folder. Your friend sees all its contents under the **"Host Shared Folder"** tab and can download individual files or the whole thing as a ZIP.
- **To receive files from a friend:** Your friend drops files or folders into the upload zone. They land on your PC in the receive directory.

---

## Speed Guide

| Connection Type | Link to Use | Typical Speed |
|---|---|---|
| PC-to-PC Ethernet cable | `169.254.x.x:8080` | **60–110 MB/s** |
| Wired LAN (via router) | `192.168.x.x:8080` | **60–110 MB/s** |
| Wi-Fi 5 GHz (same router) | `192.168.x.x:8080` | **25–40 MB/s** |
| Mobile Hotspot 5 GHz | `192.168.137.1:8080` | **20–35 MB/s** |
| Mobile Hotspot 2.4 GHz | `192.168.137.1:8080` | **2–4 MB/s** |

> **Tip:** For fastest laptop-to-laptop transfer, plug an Ethernet cable directly between them — no router needed. Windows auto-assigns `169.254.x.x` addresses and TurboShare shows the correct link automatically.

---

## Smart Resume

If a large file transfer is interrupted (power cut, browser closed, Wi-Fi drop):

1. Drop the same file or folder again.
2. TurboShare checks how many bytes are already on disk.
3. It displays **"Resuming from 483.7 MB…"** and uploads only the missing bytes.
4. Transfer completes without any data loss or restarting.

---

## Requirements

- **Python 3.8+** (standard library only for core functionality)
- **Optional:** `pip install qrcode[pil] psutil` — adds QR code images and better network interface detection

The `.bat` launcher automatically installs optional dependencies on first run.

---

## Supported Devices

Any device with a web browser can upload and download:

- ✅ Windows / macOS / Linux PC
- ✅ iPhone / iPad (Safari)
- ✅ Android (Chrome)
- ✅ Smart TV (LG webOS, Samsung Tizen, Android TV)
- ✅ Xbox / PlayStation (Edge / system browser)
- ✅ Any IoT device with a Chromium browser

---

## File Structure

```
turboshare/
├── turboshare.py      # Main server — single file, no frameworks
├── Run_TurboShare.bat # Windows double-click launcher
└── README.md
```

---

## Troubleshooting

**"This site can't be reached"**
→ Windows Defender Firewall blocked Python. When the prompt appeared, click **Allow access** and check both Private and Public networks. Or run:
```powershell
netsh advfirewall firewall add rule name="TurboShare" dir=in action=allow protocol=TCP localport=8080
```

**Hotspot speed is very slow (~2 MB/s)**
→ Switch to 5 GHz: Settings → Network & Internet → Mobile Hotspot → Edit → Band: 5 GHz. Then use `192.168.137.1:8080`.

**Large file stalled and restarted from 0**
→ This was the old version. The current version uses Smart Resume — drop the file again and it continues from the last byte.

**Can't see the Host Shared Folder tab**
→ Click **📂 Choose Folder** in the left panel of the browser on the host PC. A native folder picker opens. After selecting, the tab activates for all connected devices.

---

## License

MIT — do whatever you want with it.

---

<p align="center">Made with ⚡ by <a href="https://github.com/Piyushdas1624">Piyush Das</a></p>
