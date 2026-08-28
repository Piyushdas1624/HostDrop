import subprocess
import time
from playwright.sync_api import sync_playwright

proc = subprocess.Popen(['python', 'c:/Users/piklu/Documents/turboshare/turboshare.py', 'D:/TurboShare'])
time.sleep(2)

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1280, 'height': 800})
        page.goto('http://127.0.0.1:8080')
        page.wait_for_timeout(1000)
        page.screenshot(path='c:/Users/piklu/Documents/turboshare/updated_dashboard.png', full_page=True)

        # Open guide modal
        page.evaluate("openModal('guideModal')")
        page.wait_for_timeout(800)
        page.screenshot(path='c:/Users/piklu/Documents/turboshare/updated_faq_modal.png')

        # Mobile view
        page_m = browser.new_page(viewport={'width': 360, 'height': 740}, is_mobile=True, has_touch=True)
        page_m.goto('http://127.0.0.1:8080')
        page_m.wait_for_timeout(1000)
        page_m.screenshot(path='c:/Users/piklu/Documents/turboshare/updated_mobile.png', full_page=True)

        browser.close()
        print('SCREENSHOTS CAPTURED')
finally:
    proc.terminate()
