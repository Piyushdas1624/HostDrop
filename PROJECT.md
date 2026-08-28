# Project: TurboShare Complete Overhaul

## Architecture
TurboShare is a high-speed, zero-dependency, 2-way cross-device local file transfer hub for PC-to-PC direct Ethernet, Wi-Fi, and Mobile Hotspot. The system is architected as a clean, high-performance Python application with an embedded modern single-page web interface.

```
┌────────────────────────────────────────────────────────────────────────┐
│                        TurboShare Client (Web UI)                       │
│  - Obsidian Dark Theme (#090a0c) & Surface Ladder (#111215 - #22232a)  │
│  - 35+ Stroke-based Crisp Vector SVG Icons (Zero cartoon emojis)       │
│  - Inter & JetBrains Mono (font-variant-numeric: tabular-nums)         │
│  - Interactive In-Browser Drive & Folder Navigator Modal                │
│  - Network Links Ribbon (Mouse-wheel deltaX, Chevrons, Grid Toggle)    │
│  - File Explorer (Dual Tabs, Real-time Search/Filter, Storage Gauges)  │
│  - Full-Window Drag-and-Drop Overlay & Mobile Media Upload Picker      │
│  - Mobile Viewport min-h-[100dvh], >=44px Touch Targets, Bottom Sheets │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ HTTP / REST APIs
┌───────────────────────────────────▼────────────────────────────────────┐
│                    TurboShare Backend (Python 3 Server)                 │
│  - ThreadingHTTPServer with SO_REUSEADDR & 1MB Socket Buffers          │
│  - Drive & Directory Traversal Engine (/api/browse_host, disk_usage)   │
│  - Host OS Integration (PowerShell STA Folder Dialog, explorer.exe)    │
│  - Smart Resumable Chunked Upload Protocol (/api/check, atomic r+b)    │
│  - Dual-Tab Storage Management (UPLOAD_DIR & HOST_SHARE isolation)     │
│  - Real-time Zip Streaming Engine (/api/zip)                           │
│  - Multi-Adapter Network Discovery (Wi-Fi, Ethernet, Hotspot, P2P)     │
└────────────────────────────────────────────────────────────────────────┘
```

## Feature Inventory
| # | Feature | Description | Milestone | Status | Source |
|---|---------|-------------|-----------|--------|--------|
| 1 | In-Browser Host Folder Navigator | Navigate host drives (C:\, D:\, etc.) & subdirectories via modal from any device | M1, M2 | DONE | ORIGINAL_REQUEST §R1 |
| 2 | Robust PowerShell STA Folder Dialog | Non-blocking background STA Windows FolderBrowserDialog with foreground focus | M1 | DONE | ORIGINAL_REQUEST §R1 |
| 3 | Manual Path Input & Instant Validation | Live drive free-space detection (shutil.disk_usage) and validation | M1, M2 | DONE | ORIGINAL_REQUEST §R1 |
| 4 | "Open in OS" Foreground Explorer | Subprocess explorer.exe foreground launch with remote client viewing fallback | M1, M2 | DONE | ORIGINAL_REQUEST §R1 |
| 5 | Network Links Desktop Wheel Scroll | Mouse-wheel deltaY-to-scrollLeft translation & scroll chevron buttons | M2 | DONE | ORIGINAL_REQUEST §R2 |
| 6 | Network Links Mobile Momentum & Grid View | Touch momentum swipe & toggleable ribbon/grid view for multi-adapter layout | M2 | DONE | ORIGINAL_REQUEST §R2 |
| 7 | Min 44px Touch Hit Targets | Accessible touch targets across all mobile interactive elements | M2 | DONE | ORIGINAL_REQUEST §R2 |
| 8 | Mobile-First Layout (<860px to 360px) | Clean card stacking, zero horizontal window blowout, min-h-[100dvh] | M2 | DONE | ORIGINAL_REQUEST §R3 |
| 9 | File Explorer Search & Storage Gauges | Real-time search filter, metadata columns, visual storage capacity meters | M2 | DONE | ORIGINAL_REQUEST §R3 |
| 10| Full-Window Drag & Drop Overlay | Window-wide dragover feedback with backdrop blur and mobile file picker | M2 | DONE | ORIGINAL_REQUEST §R3 |
| 11| 100% Vector SVG Icon Suite | Obsidian dark theme (#090a0c), Inter + JetBrains Mono, strictly 0 emojis | M2 | DONE | ORIGINAL_REQUEST §R3 |
| 12| Smart Transfer Resumption | /api/check offset detection and atomic r+b seek/truncate append mode | M1 | DONE | ORIGINAL_REQUEST Acceptance |
| 13| Dual Tabs & Instant ZIP Actions | "Received Files" & "Host Shared Files" isolation with folder ZIP downloads | M1, M2 | DONE | ORIGINAL_REQUEST Acceptance |
| 14| End-to-End Live Verification | Port 8080 checks, DevTools 0 console errors, mobile viewport screenshots | M3 | DONE | ORIGINAL_REQUEST Acceptance |

## Milestones
| # | Name | Scope | Dependencies | Status | Output |
|---|------|-------|-------------|--------|--------|
| M1 | Core Backend & Host Integration Engine | API endpoints (/api/browse_host, /api/pick_folder, /api/open_folder, /api/check, /api/upload, /api/zip, /api/set_path), PowerShell STA dialog, explorer.exe foreground launcher, atomic seek/truncate resumption | None | DONE | turboshare.py, file_drop_server.py |
| M2 | Developer-Tool Obsidian Frontend Overhaul | Complete HTML/CSS/JS single-page overhaul: Obsidian palette (#090a0c), 35+ vector SVGs, in-browser folder browser modal, network links ribbon + grid toggle + wheel scroll, explorer search/filter & storage meter, full-window drop overlay, mobile layout (<860px down to 360px) | M1 | DONE | turboshare.py, file_drop_server.py |
| M3 | Integration, Adversarial Hardening & Live Verification | Live server execution on port 8080, Chrome DevTools console audit (0 errors), mobile viewport verification (360px–1280px), transfer resume stress test (35 tests pass), forensic audit (CLEAN) | M1, M2 | DONE | test_turboshare.py, tests/test_adversarial_backend.py, verify_playwright.py |

## Interface Contracts

### GET /api/browse_host?path=<optional_path>
- **Query params**: `path` (e.g. `C:\Users`, empty/omitted returns root drives)
- **Response**:
```json
{
  "current_path": "C:\\Users",
  "parent_path": "C:\\",
  "drives": [
    {"name": "C:\\", "path": "C:\\", "label": "Local Disk", "free_gb": 142.5, "total_gb": 476.2, "is_system": true}
  ],
  "subdirs": [
    {"name": "piklu", "path": "C:\\Users\\piklu", "modified": 1712345678}
  ],
  "is_root": false,
  "free_gb": 142.5,
  "total_gb": 476.2
}
```

### POST /api/set_path
- **Request body**: `{"path": "D:\\SharedFolder", "type": "recv" | "share"}`
- **Response**: `{"success": true, "status": "ok", "path": "D:\\SharedFolder", "type": "recv", "free_gb": 210.4}`

### POST /api/open_folder
- **Request body / query**: `type=recv` or `type=share`
- **Response**: `{"status": "ok", "is_local": true, "message": "Opened folder in Windows Explorer"}` (if remote: `{"status": "ok", "is_local": false, "message": "Folder opened on host PC; viewing in browser"}`)

### POST /api/pick_folder
- **Request body / query**: `type=recv` or `type=share`
- **Response**: `{"status": "ok", "path": "D:\\SelectedPath"}` or `{"status": "cancelled"}`

### GET /api/check?path=<name>&target=<recv|share>
- **Response**: `{"exists": true, "size": 10485760}` (returns exact byte size on disk for client chunk resumption)

### POST /api/upload
- **Query**: `path=<name>&offset=<bytes>&target=<recv|share>`
- **Body**: Binary chunk stream
- **Response**: `{"success": true, "status": "ok", "path": "<name>", "size": <bytes>}`

### GET /api/zip?path=<rel_path>&target=<recv|share>
- **Response**: Binary `application/zip` stream with `Content-Disposition: attachment; filename="<folder_name>.zip"`

## Code Layout
- `c:\Users\piklu\Documents\turboshare\turboshare.py` (Primary monolithic, self-contained server + embedded SPA frontend)
- `c:\Users\piklu\Documents\turboshare\file_drop_server.py` (100% bit-for-bit identical synchronized copy)
- `c:\Users\piklu\Documents\turboshare\test_turboshare.py` (Functional test suite — 29 tests)
- `c:\Users\piklu\Documents\turboshare\tests\test_adversarial_backend.py` (Adversarial stress test suite — 35 tests)
- `c:\Users\piklu\Documents\turboshare\verify_playwright.py` (Multi-viewport live browser audit — 10 viewports)
- `c:\Users\piklu\Documents\turboshare\Run_TurboShare.bat` (Host launcher script)
