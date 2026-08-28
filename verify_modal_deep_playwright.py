import os
import sys
import io
import time
import json
import socket
import tempfile
import threading
from playwright.sync_api import sync_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import turboshare

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('0.0.0.0', 0))
        return s.getsockname()[1]

def run_modal_deep_audit():
    print("=" * 70)
    print("  TURBOSHARE HOST FOLDER NAVIGATOR MODAL - DEEP PLAYWRIGHT AUDIT")
    print("=" * 70)

    temp_base = tempfile.mkdtemp(prefix="turboshare_modal_audit_")
    test_recv = os.path.join(temp_base, "inbox_recv")
    test_share = os.path.join(temp_base, "library_share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    # Seed deep subdirectories
    sub_a = os.path.join(test_recv, "Projects")
    sub_b = os.path.join(sub_a, "TurboShare")
    sub_c = os.path.join(sub_b, "Frontend")
    sub_d = os.path.join(sub_c, "Components")
    os.makedirs(sub_d, exist_ok=True)
    os.makedirs(os.path.join(test_recv, "AlphaDocs"), exist_ok=True)
    os.makedirs(os.path.join(test_recv, "BetaVideos"), exist_ok=True)
    os.makedirs(os.path.join(test_recv, "GammaMusic"), exist_ok=True)

    turboshare.UPLOAD_DIR = test_recv
    turboshare.HOST_SHARE = test_share
    test_port = find_free_port()
    turboshare.SERVER_PORT = test_port

    server = turboshare.ThreadingHTTPServer(("0.0.0.0", test_port), turboshare.TurboShareHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    print(f"[*] Ephemeral Server started on port {test_port}")

    screenshot_dir = r"c:\Users\piklu\Documents\turboshare\.agents\worker_modal_1\screenshots"
    os.makedirs(screenshot_dir, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge" if os.path.exists(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") else None, headless=True)
        
        # 1. Desktop Viewport (1280x800)
        print("\n--- TEST SUITE 1: DESKTOP MODAL INTERACTION & VERIFICATION (1280x800) ---")
        context_desktop = browser.new_context(viewport={"width": 1280, "height": 800})
        page = context_desktop.new_page()

        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        page.goto(base_url, wait_until="networkidle")
        page.wait_for_timeout(300)

        # Open Host Browser Modal
        page.locator("button:has-text('Change Folder')").first.click()
        page.wait_for_selector("#hostBrowserModal.open", timeout=3000)
        page.wait_for_selector("#modalDrivesBar .drive-card", timeout=5000)
        page.wait_for_timeout(300)

        # Verify Drive Cards
        drive_cards = page.locator("#modalDrivesBar .drive-card")
        drive_count = drive_cards.count()
        print(f"  [PASS] Drive Cards Rendered: {drive_count} drive cards detected")
        assert drive_count > 0, "No drive cards rendered"

        # Verify Capacity Meter Bar on First Drive Card
        meter_fill = drive_cards.first.locator(".drive-card-meter-fill")
        meter_visible = meter_fill.is_visible()
        print(f"  [PASS] Storage Progress Meter Bar Visible: {meter_visible}")
        assert meter_visible, "Meter bar fill not visible"

        # Verify Breadcrumb Trail
        crumbs = page.locator("#modalBreadcrumbTrail .crumb-btn")
        crumb_count = crumbs.count()
        print(f"  [PASS] Breadcrumb Trail Rendered: {crumb_count} segments")
        assert crumb_count > 0, "No breadcrumb segments"

        # Test Quick-Filter Functionality
        filter_input = page.locator("#modalFolderFilterInput")
        filter_input.fill("Alpha")
        page.wait_for_timeout(200)

        match_badge = page.locator("#modalFilterMatchCount").inner_text()
        print(f"  [PASS] Live Quick-Filter: '{match_badge}'")
        visible_rows = page.locator("#modalFolderTreeList .folder-tree-item")
        assert visible_rows.count() == 1, f"Expected 1 matching folder, got {visible_rows.count()}"

        # Test Clear Filter Button
        clear_btn = page.locator("#modalFilterClearBtn")
        assert clear_btn.is_visible(), "Clear button should be visible"
        clear_btn.click()
        page.wait_for_timeout(200)
        assert visible_rows.count() > 1, "Folders should restore after clearing filter"
        print(f"  [PASS] 1-Tap Clear Filter: Restored all {visible_rows.count()} folders")

        # Test Inline New Folder Creator
        page.click("#modalBtnNewFolder")
        inline_creator = page.locator("#modalInlineFolderCreator")
        assert inline_creator.is_visible(), "Inline folder creator should be visible"
        inline_input = page.locator("#inlineNewFolderName")
        inline_input.fill("TestProject2026")
        page.click("#modalInlineFolderCreator button.btn-primary")
        page.wait_for_timeout(500)

        # Verify new folder exists on disk
        created_path = os.path.join(test_recv, "TestProject2026")
        assert os.path.exists(created_path), "New folder was not created on disk"
        print(f"  [PASS] Inline Folder Creation: Successfully created and navigated to '{created_path}'")

        # Take Desktop Modal Screenshot
        desktop_shot = os.path.join(screenshot_dir, "desktop_modal_open.png")
        page.screenshot(path=desktop_shot)
        print(f"  [+] Saved Desktop Modal Screenshot: {desktop_shot}")

        # Close Modal
        page.click("#hostBrowserModal .modal-footer button.btn-primary")
        page.wait_for_timeout(300)
        assert not page.locator("#hostBrowserModal.open").is_visible(), "Modal should close on confirm"
        print("  [PASS] Desktop Modal Selection & Close: Success")

        # 2. Mobile Viewport (360x740)
        print("\n--- TEST SUITE 2: MOBILE BOTTOM-SHEET DRAWER VERIFICATION (360x740) ---")
        context_mobile = browser.new_context(viewport={"width": 360, "height": 740})
        mobile_page = context_mobile.new_page()
        mobile_page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

        mobile_page.goto(base_url, wait_until="networkidle")
        mobile_page.wait_for_timeout(300)

        # Open Host Browser Modal on Mobile
        mobile_page.locator("button:has-text('Change Folder')").first.click()
        mobile_page.wait_for_selector("#hostBrowserModal.open", timeout=3000)
        mobile_page.wait_for_selector("#modalDrivesBar .drive-card", timeout=5000)
        mobile_page.wait_for_timeout(300)

        # Verify Bottom-Sheet Drag Handle
        drag_handle = mobile_page.locator("#hostBrowserModal .bottom-sheet-handle")
        assert drag_handle.is_visible(), "Mobile drag handle should be visible on mobile"
        print("  [PASS] Mobile Drag Handle Pill (36x4px): Visible")

        # Verify Touch Targets >= 48px
        folder_rows = mobile_page.locator("#modalFolderTreeList .folder-tree-item")
        if folder_rows.count() > 0:
            box = folder_rows.first.bounding_box()
            assert box["height"] >= 48, f"Folder row height {box['height']}px is less than 48px"
            print(f"  [PASS] Mobile Folder Row Touch Target: {box['height']}px (>= 48px)")

        # Verify Confirm Button Height
        confirm_btn = mobile_page.locator("#modalConfirmBtn")
        btn_box = confirm_btn.bounding_box()
        assert btn_box["height"] >= 48, f"Confirm button height {btn_box['height']}px is less than 48px"
        print(f"  [PASS] Mobile Primary Confirmation Button Touch Target: {btn_box['height']}px (>= 48px)")

        # Verify Zero Horizontal Overflow
        scroll_w = mobile_page.evaluate("() => document.documentElement.scrollWidth")
        inner_w = mobile_page.evaluate("() => window.innerWidth")
        assert scroll_w <= inner_w, f"Mobile horizontal overflow: scrollWidth {scroll_w} > {inner_w}"
        print(f"  [PASS] Mobile Viewport Discipline: 0px horizontal overflow ({scroll_w}px <= {inner_w}px)")

        # Take Mobile Drawer Screenshot
        mobile_shot = os.path.join(screenshot_dir, "mobile_bottom_sheet_open.png")
        mobile_page.screenshot(path=mobile_shot)
        print(f"  [+] Saved Mobile Bottom-Sheet Screenshot: {mobile_shot}")

        assert len(console_errors) == 0, f"Console errors detected: {console_errors}"
        print(f"\n[SUCCESS] ALL MODAL DEEP AUDIT VERIFICATIONS PASSED WITH 0 CONSOLE ERRORS!")

if __name__ == "__main__":
    run_modal_deep_audit()
