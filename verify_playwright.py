import subprocess
import time
import sys
import os

def run_audit():
    server_proc = subprocess.Popen([sys.executable, 'turboshare.py'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(2.0)

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            # Launch browser (Edge / Chromium)
            browser = p.chromium.launch(channel='msedge', headless=True)
            page = browser.new_page()
            
            errors = []
            page.on('pageerror', lambda err: errors.append(f'PageError: {err}'))
            page.on('console', lambda msg: errors.append(f'ConsoleError: {msg.text}') if msg.type == 'error' else None)
            
            viewports = [320, 360, 375, 414, 480, 600, 768, 860, 1024, 1280]
            print('--- TESTING VIEWPORT HORIZONTAL OVERFLOW ---')
            for w in viewports:
                page.set_viewport_size({'width': w, 'height': 740})
                page.goto('http://127.0.0.1:8080/')
                page.wait_for_load_state('networkidle')
                
                doc_scroll = page.evaluate('document.documentElement.scrollWidth')
                doc_client = page.evaluate('document.documentElement.clientWidth')
                body_scroll = page.evaluate('document.body.scrollWidth')
                
                has_overflow = (doc_scroll > w) or (body_scroll > w)
                status = 'FAIL' if has_overflow else 'PASS'
                print(f'[{status}] Width {w}px: doc_scroll={doc_scroll}, doc_client={doc_client}, body_scroll={body_scroll}')
                assert not has_overflow, f'Viewport {w}px has overflow: doc={doc_scroll}, body={body_scroll}'
            
            # Test 360px interactive features
            page.set_viewport_size({'width': 360, 'height': 640})
            page.goto('http://127.0.0.1:8080/')
            page.wait_for_load_state('networkidle')
            
            print(f'Total Console/Page Errors: {len(errors)}')
            if errors:
                for e in errors:
                    print('  ERR:', e)
            assert len(errors) == 0, f'Page/Console errors detected: {errors}'
            
            # Test Tab Switching
            page.click('#tabShareBtn')
            time.sleep(0.3)
            is_share_active = page.evaluate('document.getElementById("tabShareBtn").classList.contains("active")')
            assert is_share_active, 'Share tab active'
            page.click('#tabRecvBtn')
            time.sleep(0.3)
            is_recv_active = page.evaluate('document.getElementById("tabRecvBtn").classList.contains("active")')
            assert is_recv_active, 'Recv tab active'
            print('[PASS] Tab switching interaction verified')
            
            # Test In-Browser Host Browser Modal
            page.click('.card-button-row button:first-child')
            time.sleep(0.5)
            modal_open = page.evaluate('document.getElementById("hostBrowserModal").classList.contains("open")')
            print(f'[PASS] Host browser modal open status: {modal_open}')
            assert modal_open, 'Host browser modal failed to open'
            
            # Close modal
            page.keyboard.press('Escape')
            time.sleep(0.3)
            modal_closed = not page.evaluate('document.getElementById("hostBrowserModal").classList.contains("open")')
            assert modal_closed, 'Modal closed on Escape'
            print('[PASS] Modal keyboard dismiss verified')
            
            # Test Guide Modal
            page.click('.header-actions button:nth-child(2)')
            time.sleep(0.3)
            guide_open = page.evaluate('document.getElementById("guideModal").classList.contains("open")')
            print(f'[PASS] Guide modal open status: {guide_open}')
            assert guide_open, 'Guide modal failed to open'
            
            # Test QR Modal
            page.click('#guideModal .icon-btn-micro')
            time.sleep(0.2)
            page.click('.header-actions button:nth-child(1)')
            time.sleep(0.3)
            qr_open = page.evaluate('document.getElementById("qrModal").classList.contains("open")')
            print(f'[PASS] QR modal open status: {qr_open}')
            assert qr_open, 'QR modal failed to open'

            # Close QR modal
            page.keyboard.press('Escape')
            time.sleep(0.2)
            
            # Take screenshot of 360px viewport
            os.makedirs('.agents/worker_2', exist_ok=True)
            page.screenshot(path='.agents/worker_2/viewport_360px.png')
            print('[PASS] Screenshot captured at .agents/worker_2/viewport_360px.png')
            
            browser.close()
            print('=== ALL PLAYWRIGHT LIVE AUDIT CHECKS PASSED (10/10 VIEWPORTS ZERO OVERFLOW, 0 ERRORS) ===')
    finally:
        try:
            server_proc.kill()
        except Exception:
            pass

if __name__ == '__main__':
    run_audit()
