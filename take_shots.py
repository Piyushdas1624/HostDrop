from playwright.sync_api import sync_playwright
import time

with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    
    # 1. Desktop view
    page = browser.new_page(viewport={'width': 1280, 'height': 800})
    page.goto('http://127.0.0.1:8080')
    page.wait_for_timeout(1000)
    page.screenshot(path='c:/Users/piklu/Documents/turboshare/desktop_dashboard.png', full_page=True)
    print("Desktop dashboard captured")

    # 2. Desktop with Folder Browser Modal open
    page.evaluate("openHostBrowserModal('share')")
    page.wait_for_timeout(1000)
    page.screenshot(path='c:/Users/piklu/Documents/turboshare/desktop_folder_modal.png')
    print("Desktop folder modal captured")
    page.close()

    # 3. Mobile 360px viewport
    page_m = browser.new_page(viewport={'width': 360, 'height': 740}, is_mobile=True, has_touch=True)
    page_m.goto('http://127.0.0.1:8080')
    page_m.wait_for_timeout(1000)
    page_m.screenshot(path='c:/Users/piklu/Documents/turboshare/mobile_dashboard.png', full_page=True)
    print("Mobile dashboard captured")

    # 4. Mobile with Folder Browser Modal open
    page_m.evaluate("openHostBrowserModal('share')")
    page_m.wait_for_timeout(1000)
    page_m.screenshot(path='c:/Users/piklu/Documents/turboshare/mobile_folder_modal.png')
    print("Mobile folder modal captured")
    page_m.close()

    browser.close()
    print("ALL DONE")
