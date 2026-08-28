# Original User Request

## Initial Request — 2026-08-28T12:32:50Z

<USER_REQUEST>
Requested team: Agent audit the UI, another agent reading the MD design skills, another agent working on the file using goal-driven execution.

TurboShare is a high-speed, 2-way cross-device local file transfer hub (PC-to-PC direct Ethernet, Wi-Fi, and hotspot). This task audits and overhauls the interface and core host controls so folder selection, OS explorer integration, network connection navigation, and file transfers are completely seamless across both desktop and mobile devices.

Working directory: c:\Users\piklu\Documents\turboshare
Integrity mode: development

## Requirements

### R1. Robust Host Folder Picker & OS Explorer Integration
- Provide a 100% reliable folder selection experience:
  - An interactive in-browser file/folder browser modal that allows navigating host drives (C:\, D:\, etc.) and choosing any folder directly from any connected device (desktop, phone, or tablet) without hanging.
  - A fallback native OS dialog trigger via PowerShell STA FolderBrowserDialog with proper focus and timeout handling on Windows.
  - Manual path input with instant validation and drive free-space detection.
- Fix "Open in OS": Ensure explorer.exe opens the specified folder in the foreground on Windows, with clear UI toast feedback and in-browser folder viewing for non-host clients.

### R2. Responsive & Scrollable Network Links (Desktop + Mobile)
- Fix the network connection links for both desktop and mobile:
  - Desktop: Native horizontal wheel-scroll support (wheel event handler translating deltaY to scrollLeft), left/right scroll navigation buttons, and custom subtle scrollbar styling.
  - Mobile: Native touch-drag swipe gesture with momentum scrolling (-webkit-overflow-scrolling: touch), min-height 44px touch targets, and a toggleable wrap/grid view so all active adapters (Wi-Fi, Ethernet, Hotspot) are immediately accessible without clipping.

### R3. Mobile-First & Desktop UI Overhaul (Linear / Raycast / Claude Design)
- Elevate the UI beyond generic templates to match top-tier developer tools:
  - Full mobile responsiveness: Stack layout cleanly on screens under 860px (workbench cards stack above/below file explorer), eliminate horizontal page overflow, set viewport min-h-[100dvh].
  - Eliminate dead empty space in the file explorer table with sensible layout density, active file search/filter, and visual storage bars.
  - Add smooth drag-and-drop feedback across the entire window with an active drop overlay (and native mobile file/folder upload picker).
  - Ensure zero cartoon emojis, maintaining 100% crisp vector SVG icons and refined typography (Inter + JetBrains Mono).

## Acceptance Criteria

### Functional Verifications
- [ ] Clicking "Choose Folder" opens either the in-browser server folder navigator or the native Windows dialog without hanging or silent errors.
- [ ] Clicking "Open in OS" successfully launches Windows Explorer on the host to the active directory.
- [ ] The Network Links bar can be smoothly scrolled horizontally using mouse wheel on desktop and touch-swipe on mobile without clipping.
- [ ] Both "Received Files" and "Host Shared Files" tabs display accurate file lists, sizes, and instant download/ZIP actions.
- [ ] Interrupted transfers auto-resume from the last received byte via /api/check and append mode.
- [ ] Mobile viewport tested down to 360px width with touch-friendly targets (>= 44px) and zero horizontal window blowout.

### Visual & Architectural Quality
- [ ] Zero console errors in Chrome DevTools during full user flow navigation.
- [ ] Dark obsidian palette (#090a0c) with consistent surface hierarchy and WCAG AA contrast.
- [ ] Verified live via Chrome screenshot and HTTP status checks on port 8080.
</USER_REQUEST>
