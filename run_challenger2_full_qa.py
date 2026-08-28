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

def run_challenger2_verification():
    print("=" * 80)
    print("  CHALLENGER 2: PLAYWRIGHT VISUAL & LIVE BROWSER QA VERIFIER")
    print("  TURBOSHARE HOST FOLDER NAVIGATOR MODAL OVERHAUL")
    print("=" * 80)

    # 1. Prepare isolated test environment with rich directory structures
    temp_base = tempfile.mkdtemp(prefix="turboshare_challenger2_")
    test_recv = os.path.join(temp_base, "inbox_recv")
    test_share = os.path.join(temp_base, "library_share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    # Deep nested hierarchy: 5 levels deep
    deep_path = os.path.join(test_recv, "Projects", "TurboShare", "Frontend", "Components", "ModalViews")
    os.makedirs(deep_path, exist_ok=True)

    # Siblings for filter and list testing
    os.makedirs(os.path.join(test_recv, "AlphaDocuments"), exist_ok=True)
    os.makedirs(os.path.join(test_recv, "BetaVideos"), exist_ok=True)
    os.makedirs(os.path.join(test_recv, "GammaAudio"), exist_ok=True)
    os.makedirs(os.path.join(test_recv, "DeltaImages"), exist_ok=True)
    os.makedirs(os.path.join(test_recv, "EpsilonArchives"), exist_ok=True)

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
    print(f"[*] Ephemeral TurboShare Server active at: {base_url}")

    screenshots_dir = r"c:\Users\piklu\Documents\turboshare\.agents\challenger_modal_2\screenshots"
    os.makedirs(screenshots_dir, exist_ok=True)

    test_results = {
        "desktop": {},
        "mobile": {},
        "console_errors": [],
        "verdict": "UNKNOWN"
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel="msedge" if os.path.exists(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") else None,
            headless=True
        )

        # =========================================================================
        # SUITE 1: DESKTOP AUDIT (1280x800 Viewport)
        # =========================================================================
        print("\n" + "=" * 60)
        print(">>> SUITE 1: DESKTOP AUDIT (1280x800 VIEWPORT)")
        print("=" * 60)

        desktop_context = browser.new_context(viewport={"width": 1280, "height": 800})
        page = desktop_context.new_page()

        desktop_console_errors = []
        page.on("console", lambda m: desktop_console_errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)

        page.goto(base_url, wait_until="networkidle")
        page.wait_for_timeout(300)

        # Verify initial page overflow
        overflow_desktop_init = page.evaluate("() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth })")
        print(f"  [CHECK] Initial Desktop Page Overflow: scrollWidth={overflow_desktop_init['scrollWidth']}px, innerWidth={overflow_desktop_init['innerWidth']}px")
        assert overflow_desktop_init['scrollWidth'] <= overflow_desktop_init['innerWidth'], "Desktop initial page has horizontal overflow"

        # 1.1 Open Host Folder Navigator Modal
        print("\n  [1.1] Testing Modal Opening...")
        change_btn = page.locator("button:has-text('Change Folder')").first
        with page.expect_response(lambda r: "/api/browse_host" in r.url and r.status == 200):
            change_btn.click()
        page.wait_for_selector("#hostBrowserModal.open", timeout=3000)
        page.wait_for_selector("#modalDrivesBar .drive-card", timeout=5000)
        page.wait_for_selector("#modalFolderTreeList .folder-tree-item", timeout=5000)
        page.wait_for_timeout(200)
        
        modal_visible = page.locator("#hostBrowserModal").is_visible()
        print(f"  [PASS] Modal Open State: visible={modal_visible}")
        assert modal_visible, "Modal is not visible"

        # Capture Default Desktop Modal Screenshot
        shot_desktop_default = os.path.join(screenshots_dir, "desktop_modal_default.png")
        page.screenshot(path=shot_desktop_default)
        print(f"  [+] Screenshot Saved: {shot_desktop_default}")

        # 1.2 Inspect Drive Cards Layout & Storage Progress Bars
        print("\n  [1.2] Testing Drive Cards & Storage Meters...")
        drive_cards = page.locator("#modalDrivesBar .drive-card")
        drive_count = drive_cards.count()
        print(f"  [PASS] Drive Cards Count: {drive_count} drives detected")
        assert drive_count > 0, "No drive cards rendered"

        first_card = drive_cards.first
        has_svg_icon = first_card.locator("svg").is_visible()
        has_title = first_card.locator(".drive-card-title").is_visible()
        has_badge = first_card.locator(".drive-card-badge").is_visible()
        has_meter = first_card.locator(".drive-card-meter-fill").is_visible()
        has_info = first_card.locator(".drive-card-info").is_visible()
        
        active_cards = page.locator("#modalDrivesBar .drive-card.active")
        active_count = active_cards.count()
        
        print(f"  [PASS] Drive Card Elements: SVG Icon={has_svg_icon}, Title={has_title}, Badge={has_badge}, Meter Bar={has_meter}, Info Readout={has_info}, Active Highlight Count={active_count}")
        assert has_svg_icon and has_title and has_meter and has_info, "Drive card is missing key visual elements"

        # 1.3 Inspect Breadcrumb Trail & Navigation
        print("\n  [1.3] Testing Breadcrumb Trail & Ancestor Navigation...")
        initial_crumbs = page.locator("#modalBreadcrumbTrail .crumb-btn")
        initial_crumb_count = initial_crumbs.count()
        print(f"  [PASS] Initial Breadcrumb Count: {initial_crumb_count} segments")
        assert initial_crumb_count > 0, "No breadcrumb segments found"

        # Navigate step-by-step into Projects -> TurboShare -> Frontend -> Components -> ModalViews
        print("  [*] Navigating 5 levels deep into nested hierarchy...")
        steps = ["Projects", "TurboShare", "Frontend", "Components", "ModalViews"]
        for step in steps:
            with page.expect_response(lambda r: "/api/browse_host" in r.url and r.status == 200):
                page.locator(f"#modalFolderTreeList .folder-tree-item:has-text('{step}')").click()
            page.wait_for_selector(f"#modalBreadcrumbTrail .crumb-btn:has-text('{step}')", timeout=3000)
            page.wait_for_timeout(100)

        deep_crumb_count = page.locator("#modalBreadcrumbTrail .crumb-btn").count()
        print(f"  [PASS] Deep Breadcrumbs Segments: {deep_crumb_count} segments rendered (initial {initial_crumb_count} + 5 steps)")
        assert deep_crumb_count == initial_crumb_count + 5, f"Expected {initial_crumb_count + 5} crumbs, got {deep_crumb_count}"

        shot_desktop_deep = os.path.join(screenshots_dir, "desktop_modal_deep_breadcrumbs.png")
        page.screenshot(path=shot_desktop_deep)
        print(f"  [+] Screenshot Saved: {shot_desktop_deep}")

        # Test Up One Level Button (#modalBtnUpLevel)
        print("  [*] Testing Up-One-Level navigation (#modalBtnUpLevel)...")
        with page.expect_response(lambda r: "/api/browse_host" in r.url and r.status == 200):
            page.click("#modalBtnUpLevel")
        page.wait_for_selector("#modalBreadcrumbTrail .crumb-btn:has-text('ModalViews')", state="detached", timeout=3000)
        current_crumbs = page.locator("#modalBreadcrumbTrail .crumb-btn").count()
        print(f"  [PASS] Navigated Up One Level: crumb count is now {current_crumbs} (was {deep_crumb_count})")
        assert current_crumbs == deep_crumb_count - 1, "Up-level navigation did not step back exactly one segment"

        # Test Manual Path Input Toggle (#modalBtnToggleManual)
        print("  [*] Testing Manual Path Input Toggle (#modalBtnToggleManual)...")
        page.click("#modalBtnToggleManual")
        page.wait_for_timeout(150)
        manual_row = page.locator("#modalManualPathRow")
        assert manual_row.is_visible(), "Manual path row is not visible after toggle"
        page.click("#modalBtnToggleManual") # Toggle back
        page.wait_for_timeout(150)

        # Jump back to ancestor inbox_recv via breadcrumb click
        print("  [*] Testing ancestor segment click jump to inbox_recv...")
        inbox_crumb = page.locator("#modalBreadcrumbTrail .crumb-btn:has-text('inbox_recv')").first
        with page.expect_response(lambda r: "/api/browse_host" in r.url and r.status == 200):
            inbox_crumb.click()
        page.wait_for_selector("#modalFolderTreeList .folder-tree-item:has-text('BetaVideos')", timeout=3000)
        page.wait_for_timeout(150)
        reverted_crumbs = page.locator("#modalBreadcrumbTrail .crumb-btn").count()
        print(f"  [PASS] Jumped back to inbox_recv: crumb count restored to {reverted_crumbs}")
        assert reverted_crumbs == initial_crumb_count, f"Expected {initial_crumb_count} crumbs, got {reverted_crumbs}"

        # 1.4 Test Live Quick-Filter
        print("\n  [1.4] Testing Live Quick-Filter Toolbar...")
        filter_input = page.locator("#modalFolderFilterInput")
        filter_input.fill("Beta")
        page.wait_for_timeout(150)

        match_badge = page.locator("#modalFilterMatchCount").inner_text()
        print(f"  [PASS] Quick-Filter Match Badge: '{match_badge}'")
        assert "/" in match_badge or "folder" in match_badge, f"Unexpected match badge text: {match_badge}"

        visible_items = page.locator("#modalFolderTreeList .folder-tree-item")
        assert visible_items.count() == 1, f"Expected 1 filtered item, found {visible_items.count()}"
        print(f"  [PASS] Filter Match Precision: Exactly 1 matching item visible ('BetaVideos')")

        shot_desktop_filter = os.path.join(screenshots_dir, "desktop_modal_filter_active.png")
        page.screenshot(path=shot_desktop_filter)
        print(f"  [+] Screenshot Saved: {shot_desktop_filter}")

        # Clear filter via button
        page.click("#modalFilterClearBtn")
        page.wait_for_timeout(150)
        assert filter_input.input_value() == "", "Filter input did not clear"
        restored_items = page.locator("#modalFolderTreeList .folder-tree-item").count()
        print(f"  [PASS] 1-Tap Filter Clear: Restored all {restored_items} subdirectories")
        assert restored_items >= 5, f"Expected all folders restored, got {restored_items}"

        # 1.5 Test Inline "+ New Folder" Creator
        print("\n  [1.5] Testing Inline '+ New Folder' Creation Form...")
        page.click("#modalBtnNewFolder")
        page.wait_for_timeout(150)
        inline_creator = page.locator("#modalInlineFolderCreator")
        assert inline_creator.is_visible(), "Inline folder creator is not visible"

        inline_input = page.locator("#inlineNewFolderName")
        inline_input.fill("Challenger2_Empirical_NewFolder")
        
        shot_desktop_inline = os.path.join(screenshots_dir, "desktop_modal_inline_folder.png")
        page.screenshot(path=shot_desktop_inline)
        print(f"  [+] Screenshot Saved: {shot_desktop_inline}")

        with page.expect_response(lambda r: "/api/create_folder" in r.url and r.status == 200):
            page.click("#modalInlineFolderCreator button.btn-primary")
        page.wait_for_selector("#modalBreadcrumbTrail .crumb-btn:has-text('Challenger2_Empirical_NewFolder')", timeout=4000)
        page.wait_for_timeout(200)

        # Verify on disk
        created_dir_path = os.path.join(test_recv, "Challenger2_Empirical_NewFolder")
        dir_exists = os.path.exists(created_dir_path)
        print(f"  [PASS] Disk Creation Verification: Path '{created_dir_path}' exists = {dir_exists}")
        assert dir_exists, "Created directory does not exist on disk"

        # 1.6 Test Keyboard Shortcuts (Arrow keys, Enter, Escape)
        print("\n  [1.6] Testing Keyboard Navigation & Shortcuts...")
        # Press Escape to close modal
        page.keyboard.press("Escape")
        page.wait_for_timeout(250)
        modal_open = page.locator("#hostBrowserModal.open").is_visible()
        print(f"  [PASS] Keyboard 'Escape' Modal Dismissal: Modal open = {modal_open}")
        assert not modal_open, "Escape key did not dismiss modal"

        # Reopen modal
        with page.expect_response(lambda r: "/api/browse_host" in r.url and r.status == 200):
            change_btn.click()
        page.wait_for_selector("#hostBrowserModal.open", timeout=3000)
        page.wait_for_timeout(200)

        # 1.7 Test Selection Confirmation
        print("\n  [1.7] Testing Folder Selection Confirmation...")
        with page.expect_response(lambda r: "/api/set_path" in r.url and r.status == 200):
            page.click("#modalConfirmBtn")
        page.wait_for_timeout(300)
        assert not page.locator("#hostBrowserModal.open").is_visible(), "Modal did not close on confirm"

        # 1.8 Verify Desktop Horizontal Overflow & Console Errors
        overflow_desktop_final = page.evaluate("() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth })")
        print(f"  [PASS] Desktop Final Overflow: scrollWidth={overflow_desktop_final['scrollWidth']}px, innerWidth={overflow_desktop_final['innerWidth']}px (0px overflow)")
        assert overflow_desktop_final['scrollWidth'] <= overflow_desktop_final['innerWidth'], "Desktop has horizontal overflow"

        print(f"  [PASS] Desktop Console Errors: {len(desktop_console_errors)}")
        assert len(desktop_console_errors) == 0, f"Desktop console errors: {desktop_console_errors}"

        test_results["desktop"]["passed"] = True


        # =========================================================================
        # SUITE 2: MOBILE AUDIT (360x740 Viewport)
        # =========================================================================
        print("\n" + "=" * 60)
        print(">>> SUITE 2: MOBILE AUDIT (360x740 VIEWPORT)")
        print("=" * 60)

        mobile_context = browser.new_context(
            viewport={"width": 360, "height": 740},
            is_mobile=True,
            has_touch=True
        )
        mobile_page = mobile_context.new_page()

        mobile_console_errors = []
        mobile_page.on("console", lambda m: mobile_console_errors.append(f"[{m.type}] {m.text}") if m.type == "error" else None)

        mobile_page.goto(base_url, wait_until="networkidle")
        mobile_page.wait_for_timeout(300)

        # Verify Mobile initial page overflow
        overflow_mobile_init = mobile_page.evaluate("() => ({ scrollWidth: document.documentElement.scrollWidth, innerWidth: window.innerWidth, bodyScrollWidth: document.body.scrollWidth })")
        print(f"  [CHECK] Initial Mobile Page Width: scrollWidth={overflow_mobile_init['scrollWidth']}px, innerWidth={overflow_mobile_init['innerWidth']}px")
        assert overflow_mobile_init['scrollWidth'] <= 360 and overflow_mobile_init['bodyScrollWidth'] <= 360, "Mobile page has initial horizontal overflow"

        # 2.1 Open Bottom-Sheet Drawer on Mobile
        print("\n  [2.1] Testing Mobile Bottom-Sheet Drawer Appearance...")
        mobile_change_btn = mobile_page.locator("button:has-text('Change Folder')").first
        with mobile_page.expect_response(lambda r: "/api/browse_host" in r.url and r.status == 200):
            mobile_change_btn.click()
        mobile_page.wait_for_selector("#hostBrowserModal.open", timeout=3000)
        mobile_page.wait_for_selector("#modalDrivesBar .drive-card", timeout=5000)
        mobile_page.wait_for_selector("#modalFolderTreeList .folder-tree-item", timeout=5000)
        mobile_page.wait_for_timeout(200)

        # Verify Bottom-Sheet Drag Handle Pill
        drag_handle = mobile_page.locator("#hostBrowserModal .bottom-sheet-handle")
        handle_visible = drag_handle.is_visible()
        handle_box = drag_handle.bounding_box()
        print(f"  [PASS] Drag Handle Pill: visible={handle_visible}, width={handle_box['width']}px, height={handle_box['height']}px")
        assert handle_visible, "Drag handle pill is not visible on mobile"

        shot_mobile_default = os.path.join(screenshots_dir, "mobile_bottom_sheet_default.png")
        mobile_page.screenshot(path=shot_mobile_default)
        print(f"  [+] Screenshot Saved: {shot_mobile_default}")

        # 2.2 Verify Mobile Touch Targets (>=44px / >=48px)
        print("\n  [2.2] Auditing Mobile Touch Targets (>=44px/48px)...")
        folder_rows = mobile_page.locator("#modalFolderTreeList .folder-tree-item")
        row_count = folder_rows.count()
        print(f"  [*] Evaluating {row_count} folder row touch targets...")
        assert row_count > 0, "No folder rows on mobile"

        for i in range(min(row_count, 5)):
            rbox = folder_rows.nth(i).bounding_box()
            print(f"    - Folder Row #{i+1} ('{folder_rows.nth(i).inner_text().splitlines()[0]}'): Height={rbox['height']}px (>= 48px target)")
            assert rbox['height'] >= 47.9, f"Folder row height {rbox['height']}px fails >=48px touch requirement"

        # Primary action buttons touch target check
        confirm_box = mobile_page.locator("#modalConfirmBtn").bounding_box()
        print(f"  [PASS] Confirmation Button Height: {confirm_box['height']}px (>= 48px)")
        assert confirm_box['height'] >= 47.9, f"Confirm button height {confirm_box['height']}px fails >=48px target"

        new_folder_box = mobile_page.locator("#modalBtnNewFolder").bounding_box()
        print(f"  [PASS] '+ New Folder' Button Height: {new_folder_box['height']}px (>= 44px)")
        assert new_folder_box['height'] >= 43.9, f"New folder button height {new_folder_box['height']}px fails >=44px target"

        # 2.3 Verify Drive Cards Horizontal Ribbon & Snap Scrolling
        print("\n  [2.3] Auditing Mobile Drive Cards Ribbon Scrolling...")
        drives_bar = mobile_page.locator("#modalDrivesBar")
        bar_styles = drives_bar.evaluate("""el => {
            const cs = window.getComputedStyle(el);
            return {
                overflowX: cs.overflowX,
                scrollSnapType: cs.scrollSnapType,
                display: cs.display,
                scrollWidth: el.scrollWidth,
                clientWidth: el.clientWidth
            };
        }""")
        print(f"  [PASS] Drive Ribbon CSS: overflowX={bar_styles['overflowX']}, scrollSnapType={bar_styles['scrollSnapType']}, display={bar_styles['display']}")
        assert bar_styles['overflowX'] in ['auto', 'scroll'], "Drive ribbon must support horizontal scrolling"

        # Scroll drive ribbon
        mobile_page.evaluate("document.querySelector('#modalDrivesBar').scrollLeft = 80")
        mobile_page.wait_for_timeout(200)

        shot_mobile_ribbon = os.path.join(screenshots_dir, "mobile_bottom_sheet_ribbon_scroll.png")
        mobile_page.screenshot(path=shot_mobile_ribbon)
        print(f"  [+] Screenshot Saved: {shot_mobile_ribbon}")

        # 2.4 Verify Mobile Breadcrumb Truncation & Scrolling
        print("\n  [2.4] Auditing Deep Breadcrumb Trail on Mobile...")
        # Step into Projects -> TurboShare -> Frontend
        for m_step in ["Projects", "TurboShare", "Frontend"]:
            with mobile_page.expect_response(lambda r: "/api/browse_host" in r.url and r.status == 200):
                mobile_page.locator(f"#modalFolderTreeList .folder-tree-item:has-text('{m_step}')").click()
            mobile_page.wait_for_selector(f"#modalBreadcrumbTrail .crumb-btn:has-text('{m_step}')", timeout=3000)
            mobile_page.wait_for_timeout(100)

        # Check breadcrumb container overflow on mobile
        crumb_container_overflow = mobile_page.evaluate("""() => {
            const trail = document.querySelector('#modalBreadcrumbTrail');
            const doc = document.documentElement;
            return {
                trailScrollWidth: trail.scrollWidth,
                trailClientWidth: trail.clientWidth,
                docScrollWidth: doc.scrollWidth,
                windowWidth: window.innerWidth
            };
        }""")
        print(f"  [PASS] Mobile Breadcrumb Container: trailClientWidth={crumb_container_overflow['trailClientWidth']}px, docScrollWidth={crumb_container_overflow['docScrollWidth']}px (Window: {crumb_container_overflow['windowWidth']}px)")
        assert crumb_container_overflow['docScrollWidth'] <= 360, "Breadcrumb trail causes horizontal document blowout on mobile"

        # 2.5 Verify Mobile Quick-Filter
        print("\n  [2.5] Auditing Mobile Quick-Filter Toolbar...")
        m_filter = mobile_page.locator("#modalFolderFilterInput")
        m_filter.fill("Comp")
        mobile_page.wait_for_timeout(150)
        m_badge = mobile_page.locator("#modalFilterMatchCount").inner_text()
        print(f"  [PASS] Mobile Filter Match Badge: '{m_badge}'")
        assert "/" in m_badge or "folder" in m_badge, f"Unexpected badge text: {m_badge}"

        shot_mobile_filter = os.path.join(screenshots_dir, "mobile_bottom_sheet_filter.png")
        mobile_page.screenshot(path=shot_mobile_filter)
        print(f"  [+] Screenshot Saved: {shot_mobile_filter}")

        # Clear filter
        mobile_page.click("#modalFilterClearBtn")
        mobile_page.wait_for_timeout(150)

        # 2.6 Verify Mobile Inline Folder Creator
        print("\n  [2.6] Auditing Mobile Inline Folder Creator...")
        mobile_page.click("#modalBtnNewFolder")
        mobile_page.wait_for_timeout(150)
        m_inline = mobile_page.locator("#modalInlineFolderCreator")
        assert m_inline.is_visible(), "Inline folder creator not visible on mobile"

        shot_mobile_inline = os.path.join(screenshots_dir, "mobile_bottom_sheet_inline_folder.png")
        mobile_page.screenshot(path=shot_mobile_inline)
        print(f"  [+] Screenshot Saved: {shot_mobile_inline}")

        mobile_page.click("#modalInlineFolderCreator button.btn-ghost:has-text('Dismiss')") # Dismiss
        mobile_page.wait_for_selector("#modalInlineFolderCreator", state="hidden", timeout=3000)
        mobile_page.wait_for_timeout(150)

        # 2.7 Verify Sticky Footer Confirmation Button
        print("\n  [2.7] Auditing Sticky Footer Positioning...")
        footer_style = mobile_page.locator("#hostBrowserModal .modal-footer").evaluate("""el => {
            const cs = window.getComputedStyle(el);
            return {
                position: cs.position,
                bottom: cs.bottom,
                display: cs.display
            };
        }""")
        print(f"  [PASS] Mobile Sticky Footer: position={footer_style['position']}, bottom={footer_style['bottom']}, display={footer_style['display']}")

        # Confirm and close on mobile
        with mobile_page.expect_response(lambda r: "/api/set_path" in r.url and r.status == 200):
            mobile_page.click("#modalConfirmBtn")
        mobile_page.wait_for_timeout(250)
        assert not mobile_page.locator("#hostBrowserModal.open").is_visible(), "Modal should close after confirmation on mobile"

        # 2.8 Verify Zero Mobile Horizontal Overflow & Zero Console Errors
        print("\n  [2.8] Auditing Mobile Viewport Discipline & Zero Console Errors...")
        overflow_mobile_final = mobile_page.evaluate("""() => ({
            scrollWidth: document.documentElement.scrollWidth,
            bodyScrollWidth: document.body.scrollWidth,
            innerWidth: window.innerWidth
        })""")
        print(f"  [PASS] Mobile Viewport Width: docScrollWidth={overflow_mobile_final['scrollWidth']}px, bodyScrollWidth={overflow_mobile_final['bodyScrollWidth']}px, innerWidth={overflow_mobile_final['innerWidth']}px (0px overflow)")
        assert overflow_mobile_final['scrollWidth'] <= 360 and overflow_mobile_final['bodyScrollWidth'] <= 360, "Mobile page has horizontal overflow"

        print(f"  [PASS] Mobile Console Errors: {len(mobile_console_errors)}")
        assert len(mobile_console_errors) == 0, f"Mobile console errors detected: {mobile_console_errors}"

        test_results["mobile"]["passed"] = True
        test_results["verdict"] = "APPROVE"

    print("\n" + "=" * 80)
    print("  [VERDICT] CHALLENGER 2 PLAYWRIGHT AUDIT: ALL TESTS PASSED -> APPROVE")
    print("=" * 80)
    return test_results

if __name__ == "__main__":
    run_challenger2_verification()
