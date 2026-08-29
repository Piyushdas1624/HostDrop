<p align="center">
  <img src="https://img.shields.io/badge/⚡_TurboShare-Cross--Device_File_Transfer-38bdf8?style=for-the-badge&labelColor=0f172a" alt="TurboShare">
</p>

<p align="center">
  <strong>No cloud. No accounts. No USB. Just open a browser.</strong><br>
  Transfer files between any two devices on the same local network at full LAN speed.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.8%2B-3776ab?style=flat-square&logo=python&logoColor=white">
  <img src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-0ea5e9?style=flat-square">
  <img src="https://img.shields.io/badge/License-MIT-10b981?style=flat-square">
  <img src="https://img.shields.io/badge/No%20Dependencies-Zero%20Install%20Required-f59e0b?style=flat-square">
</p>

---

## What it does

TurboShare connects two or more devices over your local Wi-Fi router, a mobile hotspot, or a direct Ethernet cable. Transfer speeds run at local network limits instead of routing through internet servers or cloud storage.

| Feature | Details |
|---|---|
| 🔄 **Two-way sharing** | Host shares a folder for download and receives uploads into an inbox simultaneously |
| 📁 **Host drive navigator** | Built-in folder browser with drive capacity bars, breadcrumbs, and quick search |
| ⚡ **Full LAN speed** | Direct Ethernet: 60 to 110 MB/s · Wi-Fi 5 GHz: 25 to 55 MB/s · Hotspot: 20 to 45 MB/s |
| 🔁 **Smart resume** | Interrupted transfers verify `/api/check` and resume from the exact byte on disk |
| 📱 **Cross-platform** | Works on Windows, macOS, Linux, iOS, Android, Smart TVs, and consoles in any browser |
| 🔌 **Direct Ethernet** | Automatic APIPA `169.254.x.x` detection for routerless PC-to-PC gigabit transfers |
| 📡 **QR connect** | Scannable QR codes for Wi-Fi and mobile hotspot network interfaces |
| 🗂️ **Dual folder download** | Download folders as on-the-fly streaming ZIP archives or save directly to disk |
| 🌐 **Global remote access** | Access your home PC from anywhere via encrypted tunnels with persistent authentication |

---

## How to run it

### On Windows

1. Download or clone this repository.
2. Double-click `Run_TurboShare.bat`.
3. The script asks which folder on your PC should store incoming files (defaults to `D:\TurboShare` if you press Enter).
4. The script starts the server and automatically opens `http://127.0.0.1:8080` in your default browser.
5. Point connected phones or guest laptops to the Wi-Fi or Hotspot link displayed in the browser or terminal.

### From the command line

You need Python 3.8 or newer. The server uses Python standard libraries for core transfers. Optional libraries provide QR code generation and network adapter details.

```bash
# Optional dependencies for QR code display and network adapter detection
pip install qrcode[pil] psutil

# Start server with a custom save folder
python turboshare.py "D:\TurboShare"
```

---

## How two-way sharing works

TurboShare organizes transfers into two distinct areas:

1. **Inbox (Sent to PC)**:
   This is the save destination on the host computer. When connected phones or computers upload photos, videos, or documents, those files land directly in this folder. You can change this directory at any time using the folder selector or the Windows dialog button.

2. **Library (Shared by PC)**:
   This is a folder on your computer that you want to share with connected devices (for example, a movies, music, or game folder). Connected devices can browse the contents and download files individually, download directories as ZIP archives, or save whole folder structures directly to their device.

Guest devices do not need to install any software. They open the network URL in Chrome, Safari, Edge, or Firefox and can immediately send files to the host PC or download files from the host library.

---

## Connection types and speeds

Transfer speed depends directly on the network link between your devices:

| Connection type | Address format | Typical transfer speed |
|---|---|---|
| Direct Ethernet cable (PC to PC) | `http://169.254.x.x:8080` | 60 to 110 MB/s |
| Gigabit wired LAN (via router) | `http://192.168.x.x:8080` | 60 to 110 MB/s |
| Wi-Fi 5 GHz (same router) | `http://192.168.x.x:8080` | 25 to 55 MB/s |
| Windows Mobile Hotspot (5 GHz band) | `http://192.168.137.1:8080` | 20 to 45 MB/s |
| Windows Mobile Hotspot (2.4 GHz band) | `http://192.168.137.1:8080` | 2 to 4 MB/s |

### Getting the fastest speed

- For PC to PC transfers, plug an Ethernet cable directly between the two computers. Windows automatically configures APIPA addresses (`169.254.x.x`), and TurboShare detects the direct link. This provides full gigabit speed without needing a router or internet access.
- For laptop to phone transfers, if your Wi-Fi router is slow or crowded, turn on Windows Mobile Hotspot on your laptop, set the hotspot band to 5 GHz in Windows Settings, and connect your phone directly to that hotspot.

---

## Host IP vs network IP

A common point of confusion with local servers is knowing which address to open on each device:

- **Host PC Address (`http://127.0.0.1:8080` or `localhost:8080`)**:
  This is the internal loopback address for the host computer. It only works directly on the computer running `turboshare.py`. Other devices cannot access this address. The interface marks this as Host Only and does not generate a QR code for it.

- **Network Addresses (`http://192.168.x.x:8080`, `http://192.168.137.1:8080`, etc.)**:
  These are the addresses assigned to your network adapters (Wi-Fi, Hotspot, Ethernet). Phones and other computers must use these addresses to reach TurboShare. Clicking "QR Connect" in the header provides a scannable QR code for your active network address.

---

## Smart resume

If a large transfer gets interrupted by a dropped Wi-Fi signal, power cut, or accidental browser closure:

1. Drop the exact same file or folder into the upload zone again.
2. The browser calls `/api/check` to determine how many bytes are already written to disk.
3. The upload resumes from that exact byte offset.
4. The file completes without restarting from zero.

---

## Folder download options

When downloading directories from the Library tab:

1. **Download ZIP**:
   The server packages and streams the directory on the fly into a standard `.zip` archive. Works on all operating systems and mobile devices.

2. **Save as folder**:
   On supported Chromium browsers (Chrome, Edge, Opera), this uses the File System Access API to let you select a local folder on your computer and writes the full folder structure with all subdirectories directly to disk without requiring an unzip step. Unsupported browsers automatically fall back to ZIP download.

---

## Global remote access (access from anywhere)

TurboShare includes built-in encrypted tunneling so you can access your home computer from anywhere in the world on mobile data or outside Wi-Fi.

### How it works

1. When TurboShare launches, it automatically checks for Cloudflare Tunnel (`cloudflared`) or Windows OpenSSH (`ssh` to Pinggy).
2. A secure public HTTPS link is displayed in the terminal and on the dashboard.
3. Access to host files is guarded by a dual-anchor persistent security architecture:
   - **Master Passcode**: Salted PBKDF2-HMAC-SHA256 hash (600,000 iterations) stored in `.env`.
   - **Bookmark Access Key**: A persistent access key (`ts_live_...`) for 1-click browser auto-login that sets a long-lived 30-day session cookie (`turboshare_session`).
   - **Brute-Force Shield**: Sliding-window IP rate limiting (max 5 failed attempts per 15 minutes) with exponential tarpitting ($1\text{s} \to 16\text{s}$) and automatic HTTP 429 lockout.
   - **Security Sandboxing**: Host drives and internal operating system paths are masked from remote guests. Direct Windows Explorer triggers and GUI dialogs are disabled over public tunnels.

See [SECURITY_AUDIT.md](SECURITY_AUDIT.md) for the full threat model, penetration testing verification commands, and security specifications.

---

## Troubleshooting

### Browser says site cannot be reached
- Check that both devices are on the exact same Wi-Fi network or connected to the laptop hotspot.
- Windows Defender Firewall may have blocked the port. Allow Python through Private Networks in Windows Defender Firewall, or run this in an administrative PowerShell prompt:
  ```powershell
  netsh advfirewall firewall add rule name="TurboShare" dir=in action=allow protocol=TCP localport=8080
  ```

### Mobile hotspot download speed is under 3 MB/s
- 2.4 GHz wireless bands are prone to interference and limited bandwidth.
- Open Windows Settings, go to Network and Internet, select Mobile Hotspot, click Edit, and change the Network band setting from "Any available" or "2.4 GHz" to "5 GHz". Reconnect your phone and use `http://192.168.137.1:8080`.

### Guest device cannot connect on public or university Wi-Fi
- Many public, hotel, and campus networks enable "AP isolation" or "client isolation", which prevents devices on the network from talking to each other.
- Turn on Windows Mobile Hotspot on your PC and connect your phone directly to that hotspot instead.

---

## License

This project is licensed under the MIT License.

<p align="center">Made with ⚡ by <a href="https://github.com/Piyushdas1624">Piyush Das</a></p>
