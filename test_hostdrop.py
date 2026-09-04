import os
import sys
import io
import time
import json
import socket
import shutil
import tempfile
import urllib.request
import urllib.parse
import zipfile
import threading

import hostdrop

def run_all_tests():
    print("=" * 64)
    print("  RUNNING HOSTDROP TEST SUITE")
    print("=" * 64)

    test_dir = tempfile.mkdtemp(prefix="hostdrop_test_")
    test_recv = os.path.join(test_dir, "recv")
    test_share = os.path.join(test_dir, "share")
    os.makedirs(test_recv, exist_ok=True)
    os.makedirs(test_share, exist_ok=True)

    # Initialize global state
    hostdrop.UPLOAD_DIR = test_recv
    hostdrop.HOST_SHARE = test_share
    test_port = 8899
    hostdrop.SERVER_PORT = test_port

    server = hostdrop.ThreadingHTTPServer(("127.0.0.1", test_port), hostdrop.HostDropHandler)
    server.allow_reuse_address = True
    server.daemon_threads = True

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    time.sleep(0.5)

    base_url = f"http://127.0.0.1:{test_port}"
    passed = 0
    failed = 0

    def assert_true(cond, name):
        nonlocal passed, failed
        if cond:
            print(f"  [PASS] {name}")
            passed += 1
        else:
            print(f"  [FAIL] {name}")
            failed += 1

    try:
        # 1. Main Page Test
        req = urllib.request.urlopen(f"{base_url}/")
        html_content = req.read().decode("utf-8")
        assert_true(req.status == 200, "Main Dashboard HTTP 200")
        assert_true("<!DOCTYPE html>" in html_content, "HTML5 Document Structure")
        assert_true("#090a0c" in html_content, "Obsidian Dark Theme Palette (#090a0c)")
        assert_true("font-variant-numeric: tabular-nums" in html_content, "Tabular Numerics CSS")
        # Check no cartoon emojis in HTML
        emojis = ["⚡", "🌐", "💻", "📶", "📡", "🔌", "🔵", "📂", "📱", "❓", "🔄", "📄", "📁", "📦", "⬇", "🔒"]
        has_emoji = any(e in html_content for e in emojis)
        assert_true(not has_emoji, "Strictly Zero Cartoon Emojis in Web UI")

        # 2. Interfaces API Test
        req = urllib.request.urlopen(f"{base_url}/api/interfaces")
        ifaces_data = json.loads(req.read().decode("utf-8"))
        assert_true(req.status == 200, "GET /api/interfaces HTTP 200")
        assert_true("interfaces" in ifaces_data and isinstance(ifaces_data["interfaces"], list), "Valid interfaces array")

        # 3. QR Code API Test
        req = urllib.request.urlopen(f"{base_url}/api/qr?url={urllib.parse.quote('http://127.0.0.1:8080')}")
        qr_bytes = req.read()
        assert_true(req.status == 200 and len(qr_bytes) > 50, "GET /api/qr generates valid image payload")

        # 4. Host Filesystem Browser Test
        req = urllib.request.urlopen(f"{base_url}/api/browse_host")
        roots_data = json.loads(req.read().decode("utf-8"))
        assert_true("drives" in roots_data and len(roots_data["drives"]) > 0, "GET /api/browse_host returns host drives")

        req = urllib.request.urlopen(f"{base_url}/api/browse_host?path={urllib.parse.quote(test_dir)}")
        dir_data = json.loads(req.read().decode("utf-8"))
        assert_true(dir_data.get("current_path") == os.path.abspath(test_dir), "GET /api/browse_host navigates target directory")
        assert_true(len(dir_data.get("subdirs", [])) == 2, "Directory scan detects subdirectories (recv & share)")

        # 5. Create New Folder on Host Test
        create_payload = json.dumps({"parent": test_dir, "name": "new_project_folder"}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/create_folder", data=create_payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        create_data = json.loads(resp.read().decode("utf-8"))
        assert_true(resp.status == 200 and create_data.get("success"), "POST /api/create_folder creates new subfolder")
        assert_true(os.path.exists(os.path.join(test_dir, "new_project_folder")), "Created directory exists on disk")

        # 6. Set Path API Test
        new_recv = os.path.join(test_dir, "new_project_folder")
        set_payload = json.dumps({"target": "recv", "path": new_recv}).encode("utf-8")
        req = urllib.request.Request(f"{base_url}/api/set_path", data=set_payload, headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req)
        set_data = json.loads(resp.read().decode("utf-8"))
        assert_true(resp.status == 200 and set_data.get("status") == "ok", "POST /api/set_path updates UPLOAD_DIR")
        assert_true(hostdrop.UPLOAD_DIR == new_recv, "Server internal UPLOAD_DIR state updated")

        # Reset UPLOAD_DIR for upload testing
        hostdrop.UPLOAD_DIR = test_recv

        # 7. Resumable Chunked Upload & Smart Resume Test
        # Step A: Fresh Upload (offset=0, chunk 1: 50 KB of 'A')
        total_data = b"A" * (50 * 1024) + b"B" * (50 * 1024) # 100 KB total
        part1 = total_data[:50 * 1024]
        part2 = total_data[50 * 1024:]

        req = urllib.request.Request(
            f"{base_url}/api/upload?path=benchmark_video.mp4&offset=0&target=recv",
            data=part1,
            headers={"Content-Length": str(len(part1))}
        )
        resp = urllib.request.urlopen(req)
        upload_resp1 = json.loads(resp.read().decode("utf-8"))
        assert_true(resp.status == 200 and upload_resp1.get("bytes") == 50 * 1024, "POST /api/upload chunk 1 (50KB)")

        # Step B: Smart Resume Byte Check
        req = urllib.request.urlopen(f"{base_url}/api/check?path=benchmark_video.mp4&target=recv")
        check_data = json.loads(req.read().decode("utf-8"))
        assert_true(check_data.get("exists") == True and check_data.get("size") == 50 * 1024, "GET /api/check verifies 50KB on disk")

        # Step C: Resumed Upload (offset=50KB, chunk 2: 50 KB of 'B')
        req = urllib.request.Request(
            f"{base_url}/api/upload?path=benchmark_video.mp4&offset={50 * 1024}&target=recv",
            data=part2,
            headers={"Content-Length": str(len(part2))}
        )
        resp = urllib.request.urlopen(req)
        upload_resp2 = json.loads(resp.read().decode("utf-8"))
        assert_true(resp.status == 200 and upload_resp2.get("bytes") == 50 * 1024, "POST /api/upload resumed chunk 2 (50KB)")

        # Step D: Verify Complete 100KB File Integrity
        saved_file = os.path.join(test_recv, "benchmark_video.mp4")
        with open(saved_file, "rb") as f:
            disk_content = f.read()
        assert_true(len(disk_content) == 100 * 1024, "Final file size is exactly 100 KB")
        assert_true(disk_content == total_data, "Resumed file content is bit-exact with zero corruption")

        # 8. Directory Listing API Test
        req = urllib.request.urlopen(f"{base_url}/api/list?tab=recv")
        list_data = json.loads(req.read().decode("utf-8"))
        assert_true("items" in list_data and len(list_data["items"]) >= 1, "GET /api/list returns uploaded file items")
        file_item = next((i for i in list_data["items"] if i["name"] == "benchmark_video.mp4"), None)
        assert_true(file_item is not None and file_item["size"] == 100 * 1024, "File item metadata has correct name and size")

        # 9. Download Single File Test
        req = urllib.request.urlopen(f"{base_url}/download?tab=recv&path=benchmark_video.mp4")
        dl_bytes = req.read()
        assert_true(req.status == 200 and len(dl_bytes) == 100 * 1024, "GET /download streams single file")
        assert_true(dl_bytes == total_data, "Downloaded file matches source bit-for-bit")

        # 10. Folder ZIP Streaming Test
        subfolder = os.path.join(test_share, "archive_test_folder")
        os.makedirs(os.path.join(subfolder, "nested_empty"), exist_ok=True)
        with open(os.path.join(subfolder, "document.txt"), "w", encoding="utf-8") as f:
            f.write("Hello HostDrop ZIP Engine!")

        req = urllib.request.urlopen(f"{base_url}/api/zip?tab=share&path=archive_test_folder")
        zip_bytes = req.read()
        assert_true(req.status == 200 and len(zip_bytes) > 100, "GET /api/zip streams valid ZIP archive")
        
        # Verify ZIP contents
        zip_buf = io.BytesIO(zip_bytes)
        with zipfile.ZipFile(zip_buf, "r") as zf:
            namelist = zf.namelist()
            assert_true("document.txt" in namelist or any("document.txt" in n for n in namelist), "ZIP contains nested file")
            assert_true(any("nested_empty" in n for n in namelist), "ZIP preserves empty directories")

        # 11. Host OS Explorer Foreground Spawner Test
        req = urllib.request.urlopen(f"{base_url}/api/open_folder?type=recv")
        open_data = json.loads(req.read().decode("utf-8"))
        assert_true(open_data.get("success") == True and open_data.get("is_local") == True, "GET /api/open_folder executes with is_local: true")

        # 12. Security Directory Traversal Guard Test
        bad_path = hostdrop.safe_path(test_recv, "../../../Windows/System32/cmd.exe")
        assert_true(bad_path is None, "safe_path blocks directory traversal attacks (returns None)")

        print("=" * 64)
        print(f"  TEST RESULTS: {passed} PASSED, {failed} FAILED")
        print("=" * 64)

    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(test_dir, ignore_errors=True)

    if failed > 0:
        sys.exit(1)

if __name__ == "__main__":
    run_all_tests()
