import os
import sys
import io
import time
import json
import socket
import shutil
import tempfile
import threading
from playwright.sync_api import sync_playwright

# Force UTF-8 output
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import turboshare

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('0.0.0.0', 0))
        return s.getsockname()[1]

def run_empirical_playwright_test():
    print("=" * 70)
    print("  TURBOSHARE EMPIRICAL PLAYWRIGHT BROWSER TEST HARNESS")
    print("=" * 70)

    temp_base = tempfile.mkdtemp(prefix="turboshare_pw_")
    test_recv = os.path.join(temp_base, "inbox_recv")
    test_share = os.path.join(temp_base, "library_share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    # Seed mock files in Inbox
    with open(os.path.join(test_recv, "incoming_photo.jpg"), "wb") as f:
        f.write(b"\xFF\xD8\xFF" + b"A" * 102400)
    with open(os.path.join(test_recv, "annual_report.pdf"), "wb") as f:
        f.write(b"%PDF-1.4" + b"B" * 51200)

    # Seed mock files and subfolder in Library
    with open(os.path.join(test_share, "presentation.mp4"), "wb") as f:
        f.write(b"MP4_DATA" + b"C" * 204800)
    sub_dir = os.path.join(test_share, "project_assets")
    os.makedirs(sub_dir, exist_ok=True)
    with open(os.path.join(sub_dir, "logo.png"), "wb") as f:
        f.write(b"PNG_DATA" + b"D" * 10240)

    # Configure server
    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = find_free_port()
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("0.0.0.0", test_port), turboshare.TurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.6)

    lan_ips = [iface["ip"] for iface in turboshare.get_network_interfaces() if iface["kind"] in ["wifi", "lan", "hotspot", "ethernet"]]
    lan_ip = lan_ips[0] if lan_ips else "127.0.0.1"

    base_url_host = f"http://127.0.0.1:{test_port}"
    base_url_guest = f"http://{lan_ip}:{test_port}"
    print(f"[*] Ephemeral Server started on port {test_port}")
    print(f"[*] Host URL : {base_url_host}")
    print(f"[*] Guest URL: {base_url_guest}")

    report = {
        "desktop": {},
        "mobile": {},
        "guest_mode_desktop": {},
        "guest_mode_mobile": {},
        "touch_targets": [],
        "verdict": "PENDING"
    }

    screenshot_dir = r"c:\Users\piklu\Documents\antigravity\peaceful-darwin\.agents\challenger_1"
    os.makedirs(screenshot_dir, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = None
            for ch in ["chrome", "msedge", None]:
                try:
                    if ch:
                        browser = p.chromium.launch(channel=ch, headless=True)
                    else:
                        browser = p.chromium.launch(headless=True)
                    print(f"[*] Launched Chromium ({ch or 'bundled'}) version {browser.version}")
                    break
                except Exception as ex:
                    print(f"[-] Could not launch with channel {ch}: {ex}")
            
            if not browser:
                raise RuntimeError("Failed to launch any Chromium browser")

            # ─────────────────────────────────────────────────────────────
            # SUITE 1: DESKTOP VIEWPORT (1280x800) - HOST VIEW
            # ─────────────────────────────────────────────────────────────
            print("\n" + "=" * 50)
            print("  SUITE 1: DESKTOP VIEWPORT (1280x800) - HOST VIEW")
            print("=" * 50)

            context_desktop = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context_desktop.new_page()

            desktop_console = []
            desktop_errors = []
            desktop_warnings = []
            page_errors = []

            def on_console(msg):
                desktop_console.append({"type": msg.type, "text": msg.text})
                if msg.type == "error":
                    desktop_errors.append(msg.text)
                    print(f"  [CONSOLE ERROR] {msg.text}")
                elif msg.type == "warning":
                    desktop_warnings.append(msg.text)
                    print(f"  [CONSOLE WARN] {msg.text}")

            page.on("console", on_console)
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            print("[*] Navigating to Host dashboard (1280x800)...")
            page.goto(base_url_host, wait_until="networkidle")
            page.wait_for_timeout(500)

            # 1.1 Horizontal Overflow Check
            scroll_width = page.evaluate("() => document.documentElement.scrollWidth")
            inner_width = page.evaluate("() => window.innerWidth")
            overflow_px = scroll_width - inner_width
            desktop_overflow_pass = scroll_width <= inner_width
            print(f"  [{'PASS' if desktop_overflow_pass else 'FAIL'}] Desktop Horizontal Overflow: {overflow_px}px (scrollWidth {scroll_width} <= {inner_width})")

            # 1.2 Console Errors Check
            desktop_console_pass = (len(desktop_errors) == 0 and len(page_errors) == 0)
            print(f"  [{'PASS' if desktop_console_pass else 'FAIL'}] Desktop Console: {len(desktop_errors)} errors, {len(desktop_warnings)} warnings, {len(page_errors)} page errors")

            # 1.3 Role Badge Check (Host Computer)
            host_pill_text = page.locator("#hostStatusPill").inner_text()
            host_pill_visible = page.locator("#hostStatusPill").is_visible()
            guest_pill_visible = page.locator("#guestStatusPill").is_visible()
            guest_banner_visible = page.locator("#guestBanner").is_visible()
            host_role_pass = host_pill_visible and not guest_pill_visible and not guest_banner_visible
            print(f"  [{'PASS' if host_role_pass else 'FAIL'}] Host Role Badge: visible='{host_pill_text.strip()}', guestPill visible={guest_pill_visible}, guestBanner visible={guest_banner_visible}")

            # 1.4 Terminology & Plain-English IA Check
            body_text = page.locator("body").inner_text()
            has_inbox_card = "Inbox Folder on PC" in body_text
            has_inbox_sub = "Files sent from your phone or other computers are saved here on the PC" in body_text
            has_share_card = "Share Folder from PC" in body_text
            has_share_sub = "Pick a folder on your PC" in body_text
            has_upload_card = "Send Files to PC" in body_text
            has_upload_sub = "Drop photos, videos, or folders here to transfer them directly to the PC's Inbox" in body_text

            print(f"  [{'PASS' if has_inbox_card and has_inbox_sub else 'FAIL'}] Inbox Storage Card Terminology: title={has_inbox_card}, sub={has_inbox_sub}")
            print(f"  [{'PASS' if has_share_card and has_share_sub else 'FAIL'}] Shared Library Card Terminology: title={has_share_card}, sub={has_share_sub}")
            print(f"  [{'PASS' if has_upload_card and has_upload_sub else 'FAIL'}] Upload Action Card Terminology: title={has_upload_card}, sub={has_upload_sub}")

            # 1.5 Tab Switching (Inbox vs Library)
            tab_recv = page.locator("#tabRecvBtn")
            tab_share = page.locator("#tabShareBtn")
            helper_bar = page.locator("#tabHelperBar")
            recv_badge = page.locator("#recvCountBadge")
            share_badge = page.locator("#shareCountBadge")

            initial_helper = helper_bar.inner_text().strip()
            print(f"  [*] Initial active tab helper: '{initial_helper}'")
            print(f"  [*] Inbox badge count: '{recv_badge.inner_text().strip()}'")

            # Click Library Tab
            print("[*] Clicking 'Library (Shared by PC)' tab...")
            tab_share.click()
            page.wait_for_timeout(400)
            share_helper = helper_bar.inner_text().strip()
            share_tab_active = "active" in (tab_share.get_attribute("class") or "")
            table_content_share = page.locator("#fileTableBody").inner_text()
            share_tab_pass = share_tab_active and ("Files shared by this computer" in share_helper) and ("presentation.mp4" in table_content_share)
            print(f"  [{'PASS' if share_tab_pass else 'FAIL'}] Switch to Library Tab: helper='{share_helper}', items present: {'presentation.mp4' in table_content_share}")

            # Click Inbox Tab Back
            print("[*] Switching back to 'Inbox (Sent to PC)' tab...")
            tab_recv.click()
            page.wait_for_timeout(400)
            recv_helper = helper_bar.inner_text().strip()
            recv_tab_active = "active" in (tab_recv.get_attribute("class") or "")
            table_content_recv = page.locator("#fileTableBody").inner_text()
            recv_tab_pass = recv_tab_active and ("Files transferred to this computer" in recv_helper) and ("incoming_photo.jpg" in table_content_recv)
            print(f"  [{'PASS' if recv_tab_pass else 'FAIL'}] Switch back to Inbox Tab: helper='{recv_helper}', items present: {'incoming_photo.jpg' in table_content_recv}")

            # 1.6 Interactive Guide / FAQ Modal Test
            print("\n[*] Testing Interactive Guide / FAQ Modal on Desktop...")
            guide_btn = page.locator("button:has-text('Guide')")
            guide_btn.click()
            page.wait_for_timeout(300)

            guide_modal = page.locator("#guideModal")
            modal_open = "open" in (guide_modal.get_attribute("class") or "")
            print(f"  [{'PASS' if modal_open else 'FAIL'}] Guide Modal Opened: class='{guide_modal.get_attribute('class')}'")

            faq_items = page.locator(".faq-item")
            faq_count = faq_items.count()
            print(f"  [{'PASS' if faq_count == 9 else 'FAIL'}] Total FAQ Items Count: {faq_count} (Expected: 9)")

            # Test Accordion Manual Expand / Collapse
            q1_item = page.locator(".faq-item").first
            q1_btn = q1_item.locator(".faq-question")
            q1_btn.click()
            page.wait_for_timeout(200)
            q1_open = "open" in (q1_item.get_attribute("class") or "")
            print(f"  [{'PASS' if q1_open else 'FAIL'}] FAQ Accordion Manual Expand Q1: open={q1_open}")

            q1_btn.click()
            page.wait_for_timeout(200)
            q1_collapsed = "open" not in (q1_item.get_attribute("class") or "")
            print(f"  [{'PASS' if q1_collapsed else 'FAIL'}] FAQ Accordion Manual Collapse Q1: collapsed={q1_collapsed}")

            # Test Search Query: "speed"
            faq_search = page.locator("#faqSearchInput")
            faq_search.fill("speed")
            page.wait_for_timeout(200)
            visible_speed = page.locator(".faq-item:visible").count()
            print(f"  [{'PASS' if visible_speed >= 1 else 'FAIL'}] FAQ Search 'speed': {visible_speed} visible items")

            # Test Search Query: "hotspot"
            faq_search.fill("hotspot")
            page.wait_for_timeout(200)
            visible_hotspot = page.locator(".faq-item:visible").count()
            print(f"  [{'PASS' if visible_hotspot >= 1 else 'FAIL'}] FAQ Search 'hotspot': {visible_hotspot} visible items")

            # Test Search Query: "resumption"
            faq_search.fill("resumption")
            page.wait_for_timeout(200)
            visible_resume = page.locator(".faq-item:visible").count()
            print(f"  [{'PASS' if visible_resume >= 1 else 'FAIL'}] FAQ Search 'resumption': {visible_resume} visible items")

            # Clear search
            faq_search.fill("")
            page.wait_for_timeout(200)
            visible_all = page.locator(".faq-item:visible").count()
            print(f"  [{'PASS' if visible_all == 9 else 'FAIL'}] FAQ Search Cleared: {visible_all} items restored")

            # Capture FAQ modal screenshot
            faq_screenshot_path = os.path.join(screenshot_dir, "desktop_faq_modal.png")
            page.screenshot(path=faq_screenshot_path)
            print(f"  [*] Saved screenshot: {faq_screenshot_path}")

            # Close FAQ modal
            page.locator("#guideModal button.icon-btn-micro").click()
            page.wait_for_timeout(200)
            modal_closed = "open" not in (guide_modal.get_attribute("class") or "")
            print(f"  [{'PASS' if modal_closed else 'FAIL'}] Guide Modal Closed: open={not modal_closed}")

            # 1.7 Host Folder Picker Modal Test
            print("\n[*] Testing Host Folder Picker Modal...")
            change_folder_btn = page.locator("button:has-text('Change Folder')")
            change_folder_btn.click()
            page.wait_for_timeout(400)

            browser_modal = page.locator("#hostBrowserModal")
            browser_modal_open = "open" in (browser_modal.get_attribute("class") or "")
            print(f"  [{'PASS' if browser_modal_open else 'FAIL'}] Host Browser Modal Opened: class='{browser_modal.get_attribute('class')}'")

            # Close Host Browser Modal
            page.locator("#hostBrowserModal button:has-text('Cancel')").click()
            page.wait_for_timeout(200)
            browser_modal_closed = "open" not in (browser_modal.get_attribute("class") or "")
            print(f"  [{'PASS' if browser_modal_closed else 'FAIL'}] Host Browser Modal Closed: closed={browser_modal_closed}")

            # 1.8 QR Modal Test
            print("\n[*] Testing QR Connect Modal...")
            qr_btn = page.locator("button:has-text('QR Connect')")
            qr_btn.click()
            page.wait_for_timeout(300)
            qr_modal = page.locator("#qrModal")
            qr_modal_open = "open" in (qr_modal.get_attribute("class") or "")
            qr_img_src = page.locator("#qrModalImg").get_attribute("src") or ""
            print(f"  [{'PASS' if qr_modal_open and '/api/qr' in qr_img_src else 'FAIL'}] QR Modal: open={qr_modal_open}, src='{qr_img_src}'")
            page.locator("#qrModal button:has-text('Close')").click()
            page.wait_for_timeout(200)

            # Desktop Dashboard Screenshot
            desktop_screenshot_path = os.path.join(screenshot_dir, "desktop_dashboard_1280.png")
            page.screenshot(path=desktop_screenshot_path)
            print(f"  [*] Saved desktop dashboard screenshot: {desktop_screenshot_path}")

            report["desktop"] = {
                "scroll_width": scroll_width,
                "inner_width": inner_width,
                "overflow_px": overflow_px,
                "overflow_pass": desktop_overflow_pass,
                "console_errors": desktop_errors,
                "console_warnings": desktop_warnings,
                "page_errors": page_errors,
                "console_pass": desktop_console_pass,
                "host_pill_visible": host_pill_visible,
                "host_role_pass": host_role_pass,
                "ia_inbox_card": has_inbox_card,
                "ia_library_card": has_share_card,
                "ia_upload_card": has_upload_card,
                "faq_count": faq_count,
                "faq_filter_speed_matched": visible_speed,
                "faq_filter_hotspot_matched": visible_hotspot,
                "faq_filter_resumption_matched": visible_resume,
                "accordion_expand_pass": q1_open,
                "accordion_collapse_pass": q1_collapsed,
                "tab_switching_pass": (share_tab_pass and recv_tab_pass),
                "host_browser_modal_pass": (browser_modal_open and browser_modal_closed),
                "qr_modal_pass": qr_modal_open
            }

            context_desktop.close()

            # ─────────────────────────────────────────────────────────────
            # SUITE 2: MOBILE VIEWPORT (360x740) - HOST VIEW
            # ─────────────────────────────────────────────────────────────
            print("\n" + "=" * 50)
            print("  SUITE 2: MOBILE VIEWPORT (360x740)")
            print("=" * 50)

            context_mobile = browser.new_context(
                viewport={"width": 360, "height": 740},
                user_agent="Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
                is_mobile=True,
                has_touch=True
            )
            page_m = context_mobile.new_page()

            mobile_console = []
            mobile_errors = []
            mobile_warnings = []
            mobile_page_errors = []

            def on_mobile_console(msg):
                mobile_console.append({"type": msg.type, "text": msg.text})
                if msg.type == "error":
                    mobile_errors.append(msg.text)
                    print(f"  [MOBILE CONSOLE ERROR] {msg.text}")
                elif msg.type == "warning":
                    mobile_warnings.append(msg.text)
                    print(f"  [MOBILE CONSOLE WARN] {msg.text}")

            page_m.on("console", on_mobile_console)
            page_m.on("pageerror", lambda err: mobile_page_errors.append(str(err)))

            print("[*] Navigating to dashboard in Mobile (360px)...")
            page_m.goto(base_url_host, wait_until="networkidle")
            page_m.wait_for_timeout(500)

            # 2.1 Mobile Horizontal Overflow Check
            m_scroll_width = page_m.evaluate("() => document.documentElement.scrollWidth")
            m_inner_width = page_m.evaluate("() => window.innerWidth")
            m_overflow_px = m_scroll_width - m_inner_width
            m_overflow_pass = m_scroll_width <= 360
            print(f"  [METRIC] Mobile scrollWidth: {m_scroll_width}px, innerWidth: {m_inner_width}px (Overflow: {m_overflow_px}px)")
            print(f"  [{'PASS' if m_overflow_pass else 'FAIL'}] Mobile Horizontal Overflow: {m_overflow_px}px (scrollWidth {m_scroll_width} <= 360)")

            # Check individual container widths
            elements_checked = page_m.evaluate("""() => {
                const results = [];
                const selectors = ['.app-header', '.network-ribbon', '.sidebar', '.main-content', '.card', '.tab-bar', '.table-container', '.app-container'];
                selectors.forEach(sel => {
                    document.querySelectorAll(sel).forEach((el, idx) => {
                        const r = el.getBoundingClientRect();
                        results.push({
                            selector: sel + '[' + idx + ']',
                            width: Math.round(r.width * 10) / 10,
                            overflows: r.width > 360.0
                        });
                    });
                });
                return results;
            }""")
            container_overflows = [e for e in elements_checked if e["overflows"]]
            container_bounds_pass = len(container_overflows) == 0
            print(f"  [{'PASS' if container_bounds_pass else 'FAIL'}] Container Width Bounds (All <= 360px): {len(elements_checked)} containers verified, {len(container_overflows)} overflowing")
            for e in elements_checked:
                print(f"      - {e['selector']}: width={e['width']}px, overflows={e['overflows']}")

            # 2.2 Mobile Console Errors
            m_console_pass = (len(mobile_errors) == 0 and len(mobile_page_errors) == 0)
            print(f"  [{'PASS' if m_console_pass else 'FAIL'}] Mobile Console: {len(mobile_errors)} errors, {len(mobile_warnings)} warnings")

            # 2.3 Mobile Touch Targets Analysis (>= 44px)
            touch_targets = page_m.evaluate("""() => {
                const interactive = Array.from(document.querySelectorAll('button, input, a, .tab-btn, .btn, .faq-question, .chip-btn'));
                const results = [];
                interactive.forEach(el => {
                    const r = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    if (r.width > 0 && r.height > 0 && style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0') {
                        const isMicro = el.classList.contains('icon-btn-micro') || el.closest('.icon-btn-micro');
                        results.push({
                            tag: el.tagName.toLowerCase(),
                            id: el.id || '',
                            className: el.className || '',
                            text: (el.innerText || el.value || el.placeholder || '').trim().substring(0, 35),
                            width: Math.round(r.width * 10) / 10,
                            height: Math.round(r.height * 10) / 10,
                            isMicro: !!isMicro,
                            passes44: r.height >= 43.5
                        });
                    }
                });
                return results;
            }""")

            total_interactive = len(touch_targets)
            primary_targets = [t for t in touch_targets if not t["isMicro"]]
            primary_pass_44 = [t for t in primary_targets if t["passes44"]]

            print(f"  [*] Total visible interactive elements: {total_interactive}")
            print(f"  [*] Primary actionable controls: {len(primary_targets)}")
            print(f"  [*] Primary controls >= 44px height: {len(primary_pass_44)} / {len(primary_targets)}")

            # 2.4 Mobile FAQ Modal & Zero Blowout Check
            print("\n[*] Testing Mobile FAQ Modal...")
            page_m.locator("button:has-text('Guide')").click()
            page_m.wait_for_timeout(300)
            m_modal_scroll_w = page_m.evaluate("() => document.documentElement.scrollWidth")
            m_modal_content_w = page_m.locator("#guideModal .modal-content").bounding_box()["width"]
            m_modal_pass = m_modal_scroll_w <= 360 and m_modal_content_w <= 360
            print(f"  [{'PASS' if m_modal_pass else 'FAIL'}] Mobile FAQ Modal scrollWidth: {m_modal_scroll_w}px (<= 360px), modal-content: {m_modal_content_w}px")

            # Mobile FAQ Manual Accordion Expand & Collapse
            m_q1 = page_m.locator(".faq-item").first
            m_q1.locator(".faq-question").click()
            page_m.wait_for_timeout(200)
            m_q1_open = "open" in (m_q1.get_attribute("class") or "")
            print(f"  [{'PASS' if m_q1_open else 'FAIL'}] Mobile FAQ Manual Accordion Expand: open={m_q1_open}")

            m_q1.locator(".faq-question").click()
            page_m.wait_for_timeout(200)
            m_q1_collapsed = "open" not in (m_q1.get_attribute("class") or "")
            print(f"  [{'PASS' if m_q1_collapsed else 'FAIL'}] Mobile FAQ Manual Accordion Collapse: collapsed={m_q1_collapsed}")

            # Mobile FAQ Search & Auto-Expand Check
            page_m.locator("#faqSearchInput").fill("hotspot")
            page_m.wait_for_timeout(200)
            m_hotspot_visible = page_m.locator(".faq-item:visible").count()
            m_first_match_open = "open" in (page_m.locator(".faq-item:visible").first.get_attribute("class") or "")
            print(f"  [{'PASS' if m_hotspot_visible >= 1 and m_first_match_open else 'FAIL'}] Mobile FAQ Filter & Auto-Expand 'hotspot': {m_hotspot_visible} visible, open={m_first_match_open}")

            # Clear search
            page_m.locator("#faqSearchInput").fill("")
            page_m.wait_for_timeout(200)

            # Capture Mobile FAQ Screenshot
            mobile_faq_screenshot = os.path.join(screenshot_dir, "mobile_faq_modal_360.png")
            page_m.screenshot(path=mobile_faq_screenshot)
            print(f"  [*] Saved mobile FAQ screenshot: {mobile_faq_screenshot}")

            # Close Mobile FAQ Modal
            page_m.locator("#guideModal button.icon-btn-micro").click()
            page_m.wait_for_timeout(200)

            # Mobile Dashboard Screenshot
            mobile_screenshot_path = os.path.join(screenshot_dir, "mobile_dashboard_360.png")
            page_m.screenshot(path=mobile_screenshot_path)
            print(f"  [*] Saved mobile dashboard screenshot: {mobile_screenshot_path}")

            report["mobile"] = {
                "scroll_width": m_scroll_width,
                "inner_width": m_inner_width,
                "overflow_px": m_overflow_px,
                "overflow_pass": m_overflow_pass,
                "container_bounds_pass": container_bounds_pass,
                "console_errors": mobile_errors,
                "console_warnings": mobile_warnings,
                "page_errors": mobile_page_errors,
                "console_pass": m_console_pass,
                "containers_verified": len(elements_checked),
                "touch_targets_total": total_interactive,
                "primary_touch_targets": len(primary_targets),
                "primary_pass_44px": len(primary_pass_44),
                "mobile_faq_scroll_width": m_modal_scroll_w,
                "mobile_faq_pass": m_modal_pass,
                "mobile_accordion_expand_pass": m_q1_open,
                "mobile_accordion_collapse_pass": m_q1_collapsed
            }
            report["touch_targets"] = touch_targets

            context_mobile.close()

            # ─────────────────────────────────────────────────────────────
            # SUITE 3: GUEST DEVICE EXPERIENCE - DESKTOP VIEWPORT (1280x800)
            # ─────────────────────────────────────────────────────────────
            print("\n" + "=" * 50)
            print(f"  SUITE 3A: GUEST DEVICE EXPERIENCE - DESKTOP ({base_url_guest})")
            print("=" * 50)

            context_guest_d = browser.new_context(viewport={"width": 1280, "height": 800})
            page_gd = context_guest_d.new_page()

            page_gd.goto(base_url_guest, wait_until="networkidle")
            page_gd.wait_for_timeout(500)

            gd_guest_pill_vis = page_gd.locator("#guestStatusPill").is_visible()
            gd_host_pill_vis = page_gd.locator("#hostStatusPill").is_visible()
            gd_guest_banner_vis = page_gd.locator("#guestBanner").is_visible()
            gd_guest_pill_text = page_gd.locator("#guestStatusPill").inner_text()
            gd_guest_banner_text = page_gd.locator("#guestBanner").inner_text()

            gd_has_cue1 = "Send Files to the PC" in gd_guest_banner_text
            gd_has_cue2 = "Download Files from PC Library" in gd_guest_banner_text
            gd_role_pass = gd_guest_pill_vis and (not gd_host_pill_vis) and gd_guest_banner_vis and gd_has_cue1 and gd_has_cue2

            print(f"  [{'PASS' if gd_role_pass else 'FAIL'}] Desktop Guest Mode: guestPill visible={gd_guest_pill_vis} ('{gd_guest_pill_text.strip()}'), hostPill visible={gd_host_pill_vis}, banner visible={gd_guest_banner_vis}")
            report["guest_mode_desktop"] = {
                "guest_pill_visible": gd_guest_pill_vis,
                "host_pill_hidden": not gd_host_pill_vis,
                "guest_banner_visible": gd_guest_banner_vis,
                "guidance_cues_pass": gd_role_pass
            }

            context_guest_d.close()

            # ─────────────────────────────────────────────────────────────
            # SUITE 3B: GUEST DEVICE EXPERIENCE - MOBILE VIEWPORT (360x740)
            # ─────────────────────────────────────────────────────────────
            print("\n" + "=" * 50)
            print(f"  SUITE 3B: GUEST DEVICE EXPERIENCE - MOBILE ({base_url_guest})")
            print("=" * 50)

            context_guest_m = browser.new_context(
                viewport={"width": 360, "height": 740},
                user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
                is_mobile=True,
                has_touch=True
            )
            page_gm = context_guest_m.new_page()

            page_gm.goto(base_url_guest, wait_until="networkidle")
            page_gm.wait_for_timeout(500)

            gm_guest_banner_vis = page_gm.locator("#guestBanner").is_visible()
            gm_guest_banner_text = page_gm.locator("#guestBanner").inner_text()
            gm_has_cue1 = "Send Files to the PC" in gm_guest_banner_text
            gm_has_cue2 = "Download Files from PC Library" in gm_guest_banner_text
            gm_badge_pill = page_gm.locator("#guestBanner .guest-badge-pill").inner_text().strip()

            gm_role_pass = gm_guest_banner_vis and gm_has_cue1 and gm_has_cue2 and (gm_badge_pill == "GUEST DEVICE")
            print(f"  [{'PASS' if gm_role_pass else 'FAIL'}] Mobile Guest Mode: banner visible={gm_guest_banner_vis}, badge='{gm_badge_pill}', cue1={gm_has_cue1}, cue2={gm_has_cue2}")

            # Guest screenshot
            guest_screenshot_path = os.path.join(screenshot_dir, "guest_device_view_360.png")
            page_gm.screenshot(path=guest_screenshot_path)
            print(f"  [*] Saved guest device view screenshot: {guest_screenshot_path}")

            report["guest_mode_mobile"] = {
                "guest_banner_visible": gm_guest_banner_vis,
                "badge_pill": gm_badge_pill,
                "cue_1_present": gm_has_cue1,
                "cue_2_present": gm_has_cue2,
                "guidance_cues_pass": gm_role_pass
            }

            context_guest_m.close()
            browser.close()

    finally:
        print("\n[*] Shutting down ephemeral test server...")
        server.shutdown()
        server.server_close()
        shutil.rmtree(temp_base, ignore_errors=True)
        print("[*] Cleanup complete.")

    # Evaluate Overall Verdict
    all_passes = (
        report["desktop"].get("overflow_pass", False) and
        report["desktop"].get("console_pass", False) and
        report["desktop"].get("tab_switching_pass", False) and
        report["desktop"].get("accordion_expand_pass", False) and
        report["desktop"].get("accordion_collapse_pass", False) and
        report["desktop"].get("faq_count", 0) == 9 and
        report["desktop"].get("host_role_pass", False) and
        report["mobile"].get("overflow_pass", False) and
        report["mobile"].get("container_bounds_pass", False) and
        report["mobile"].get("console_pass", False) and
        report["mobile"].get("mobile_faq_pass", False) and
        report["mobile"].get("mobile_accordion_expand_pass", False) and
        report["mobile"].get("mobile_accordion_collapse_pass", False) and
        report["guest_mode_desktop"].get("guidance_cues_pass", False) and
        report["guest_mode_mobile"].get("guidance_cues_pass", False)
    )

    report["verdict"] = "APPROVE" if all_passes else "REQUEST_CHANGES"
    print("\n" + "=" * 70)
    print(f"  FINAL PLAYWRIGHT EMPIRICAL VERDICT: {report['verdict']}")
    print("=" * 70)

    # Save JSON report
    report_file = os.path.join(screenshot_dir, "test_report.json")
    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"[*] Saved test report to: {report_file}")

    return report

if __name__ == "__main__":
    run_empirical_playwright_test()
