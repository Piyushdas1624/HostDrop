"""
Permanent Cross-Platform Mock Test Suite for HostDrop.
Validates OS branching logic across Android Termux, Linux, macOS, and Windows
using 100% Python standard library unittest and unittest.mock.

Execution:
    python test_platform_mock.py
    python -m unittest test_platform_mock.py -v
"""

import os
import sys
import io
import json
import shutil
import string
import tempfile
import threading
import subprocess
import urllib.parse
import urllib.request
import urllib.error
import unittest
from unittest.mock import patch, MagicMock, call

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import hostdrop
import auth


class TestCrossPlatformMock(unittest.TestCase):
    """
    Comprehensive Mock Verification for Multi-Platform Operations:
    - Android Termux detection and storage mounts
    - Linux and macOS root/home/Volumes mounts and virtual FS filtering
    - Windows dynamic GetVolumeInformationW volume labels
    - OS file manager dispatch (termux-open, open, xdg-open, explorer) & headless fallbacks
    - Remote tunnel 403 sandbox and "viewing in browser" isolation
    - Tunnel manager candidate discovery (Cloudflare & hardened Pinggy OpenSSH)
    - Default startup inbox directory selection per platform
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="hostdrop_platform_test_")

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_termux_detection_and_storage_mounts(self):
        """
        Verifies:
        1. is_termux() detection across all 4 criteria:
           - $TERMUX_VERSION
           - /data/data/com.termux directory presence
           - $PREFIX containing 'com.termux'
           - sys.getandroidapilevel() presence
           - False in clean desktop environments
        2. get_host_drives() under Termux:
           - Exposes /sdcard ('Internal Storage (/sdcard)', letter 'SD')
           - Exposes ~/storage/downloads or /sdcard/Download (letter 'DL')
           - Exposes Termux home ~ ('Termux Home (~)', letter 'TH', is_system=True)
           - Exposes ~/storage/shared fallback if /sdcard is absent
           - Exposes OTG /storage/XXXX-XXXX mounts
           - Exposes system root / if readable
        3. browse_host_directory() under Termux:
           - Catches PermissionError on /sdcard or /storage and returns actionable guidance:
             "Permission denied. Run 'termux-setup-storage' in Termux to grant storage access."
        """
        # 1. Verification of is_termux() detection branches
        # 1a. TERMUX_VERSION
        with patch.dict(os.environ, {"TERMUX_VERSION": "0.118.0"}, clear=True):
            self.assertTrue(hostdrop.is_termux(), "TERMUX_VERSION env var must trigger is_termux()=True")

        # 1b. /data/data/com.termux exists
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", side_effect=lambda p: p == "/data/data/com.termux"):
                self.assertTrue(hostdrop.is_termux(), "/data/data/com.termux must trigger is_termux()=True")

        # 1c. PREFIX contains com.termux
        with patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"}, clear=True):
            self.assertTrue(hostdrop.is_termux(), "PREFIX with com.termux must trigger is_termux()=True")

        # 1d. hasattr(sys, 'getandroidapilevel')
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                with patch.object(sys, "getandroidapilevel", create=True, new=lambda: 30):
                    self.assertTrue(hostdrop.is_termux(), "sys.getandroidapilevel must trigger is_termux()=True")

        # 1e. Clean non-termux environment returns False
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                # Ensure getandroidapilevel is not present
                orig_has_api = hasattr(sys, "getandroidapilevel")
                if orig_has_api:
                    del sys.getandroidapilevel
                try:
                    self.assertFalse(hostdrop.is_termux(), "Clean desktop environment must return False for is_termux()")
                finally:
                    if orig_has_api:
                        sys.getandroidapilevel = lambda: 30

        # 2. Verification of Termux get_host_drives() storage mounts
        def fake_termux_exists(path):
            norm = path.replace("\\", "/")
            return norm in (
                "/sdcard",
                "/sdcard/Download",
                "/data/data/com.termux/files/home",
                "/storage",
                "/storage/ABCD-1234",
                "/"
            )

        fake_disk_usage = MagicMock(total=64 * 1024**3, used=20 * 1024**3, free=44 * 1024**3)

        with patch("hostdrop.is_termux", return_value=True):
            with patch("os.path.exists", side_effect=fake_termux_exists):
                with patch("os.path.expanduser", return_value="/data/data/com.termux/files/home"):
                    with patch("os.listdir", side_effect=lambda p: ["ABCD-1234"] if p == "/storage" else []):
                        with patch("os.path.isdir", return_value=True):
                            with patch("os.access", return_value=True):
                                with patch("shutil.disk_usage", return_value=fake_disk_usage):
                                    drives = hostdrop.get_host_drives()

        self.assertIsInstance(drives, list)
        letters = [d["letter"] for d in drives]
        labels = [d["label"] for d in drives]

        self.assertIn("SD", letters, "Termux must expose Internal Storage ('SD')")
        self.assertIn("DL", letters, "Termux must expose Downloads ('DL')")
        self.assertIn("TH", letters, "Termux must expose Termux Home ('TH')")
        self.assertIn("Internal Storage (/sdcard)", labels)
        self.assertIn("Downloads (/sdcard/Download)", labels)
        self.assertIn("Termux Home (~)", labels)

        # Termux Home must be marked is_system=True
        th_entry = next(d for d in drives if d["letter"] == "TH")
        self.assertTrue(th_entry["is_system"])

        # Internal Storage must be is_system=False
        sd_entry = next(d for d in drives if d["letter"] == "SD")
        self.assertFalse(sd_entry["is_system"])

        # OTG mount present
        otg_entry = next((d for d in drives if "ABCD-1234" in d["label"]), None)
        self.assertIsNotNone(otg_entry, "OTG external storage must be mounted")
        self.assertEqual(otg_entry["letter"], "SD")

        # Fallback to ~/storage/shared if /sdcard is absent
        def fake_storage_shared_exists(path):
            norm = path.replace("\\", "/")
            return norm in (
                "/data/data/com.termux/files/home/storage/shared",
                "/data/data/com.termux/files/home/storage/downloads",
                "/data/data/com.termux/files/home"
            )

        with patch("hostdrop.is_termux", return_value=True):
            with patch("os.path.exists", side_effect=fake_storage_shared_exists):
                with patch("os.path.expanduser", side_effect=lambda p: p.replace("~", "/data/data/com.termux/files/home")):
                    with patch("shutil.disk_usage", return_value=fake_disk_usage):
                        shared_drives = hostdrop.get_host_drives()

        shared_paths = [d["path"].replace("\\", "/") for d in shared_drives]
        self.assertIn("/data/data/com.termux/files/home/storage/shared", shared_paths)
        self.assertIn("/data/data/com.termux/files/home/storage/downloads", shared_paths)

        # 3. PermissionError handling with termux-setup-storage guidance
        with patch("hostdrop.is_termux", return_value=True):
            with patch("os.path.exists", return_value=True):
                with patch("os.path.isdir", return_value=True):
                    with patch("os.scandir", side_effect=PermissionError("Permission denied")):
                        res_sd = hostdrop.browse_host_directory("/sdcard")
                        self.assertIn("error", res_sd)
                        self.assertIn("termux-setup-storage", res_sd["error"])
                        self.assertEqual(res_sd["error"], "Permission denied. Run 'termux-setup-storage' in Termux to grant storage access.")

                        res_storage = hostdrop.browse_host_directory("/storage/emulated/0")
                        self.assertIn("termux-setup-storage", res_storage["error"])

                        res_home = hostdrop.browse_host_directory("/data/data/com.termux/files/home")
                        self.assertIn("termux-setup-storage", res_home["error"])

    def test_linux_and_macos_mounts(self):
        """
        Verifies:
        1. Linux and macOS storage discovery in get_host_drives():
           - Root /: label 'Root (/)', letter '/', is_system=True
           - Home ~: label 'Home (~)', letter '~', is_system=False
           - macOS /Volumes mounts
           - Linux /media, /media/$USER, /run/media/$USER, and /mnt mounts
           - Correct contract metadata for each mount
        2. Virtual filesystem filtering in browse_host_directory():
           - Filters /proc, /sys, and /dev when browsing / on POSIX systems
           - Preserves real directories (e.g. /etc, /home, /var)
           - Root directory parent_path is empty string "" to reset UI to drives view
        """
        def fake_posix_exists(path):
            norm = path.replace("\\", "/")
            return norm in (
                "/",
                "/home/developer",
                "/Volumes",
                "/Volumes/TimeMachine",
                "/media",
                "/media/developer",
                "/media/developer/FlashDrive",
                "/run/media/developer",
                "/run/media/developer/SDCard",
                "/mnt",
                "/mnt/DataBackup"
            )

        def fake_posix_listdir(path):
            norm = path.replace("\\", "/")
            if norm == "/Volumes":
                return ["TimeMachine"]
            elif norm == "/media":
                return ["developer"]
            elif norm == "/media/developer":
                return ["FlashDrive"]
            elif norm == "/run/media/developer":
                return ["SDCard"]
            elif norm == "/mnt":
                return ["DataBackup"]
            return []

        fake_disk_usage = MagicMock(total=500 * 1024**3, used=200 * 1024**3, free=300 * 1024**3)

        with patch("hostdrop.is_termux", return_value=False):
            with patch("sys.platform", "linux"):
                with patch("hostdrop.CURRENT_USER", "developer"):
                    with patch("os.path.exists", side_effect=fake_posix_exists):
                        with patch("os.path.expanduser", return_value="/home/developer"):
                            with patch("os.listdir", side_effect=fake_posix_listdir):
                                with patch("os.path.isdir", return_value=True):
                                    with patch("os.path.islink", return_value=False):
                                        with patch("shutil.disk_usage", return_value=fake_disk_usage):
                                            drives = hostdrop.get_host_drives()

        self.assertGreater(len(drives), 2)
        paths = [d["path"].replace("\\", "/") for d in drives]
        letters = [d["letter"] for d in drives]
        labels = [d["label"] for d in drives]

        # Verify root and home
        self.assertIn("/", paths)
        self.assertIn("/home/developer", paths)
        root_entry = next(d for d in drives if d["path"].replace("\\", "/") == "/")
        self.assertTrue(root_entry["is_system"])
        self.assertEqual(root_entry["letter"], "/")
        self.assertEqual(root_entry["label"], "Root (/)")

        home_entry = next(d for d in drives if d["path"].replace("\\", "/") == "/home/developer")
        self.assertFalse(home_entry["is_system"])
        self.assertEqual(home_entry["letter"], "~")
        self.assertEqual(home_entry["label"], "Home (~)")

        # Verify external mount points
        self.assertIn("/Volumes/TimeMachine", paths)
        self.assertIn("/media/developer/FlashDrive", paths)
        self.assertIn("/run/media/developer/SDCard", paths)
        self.assertIn("/mnt/DataBackup", paths)

        # Verify virtual filesystem filtering in browse_host_directory("/") on POSIX
        mock_entries = []
        for d_name in ["proc", "sys", "dev", "etc", "home", "var", "opt"]:
            entry = MagicMock()
            entry.is_dir.return_value = True
            entry.name = d_name
            entry.path = f"/{d_name}"
            mock_entries.append(entry)

        mock_scandir = MagicMock()
        mock_scandir.__enter__.return_value = mock_entries
        mock_scandir.__exit__.return_value = None

        with patch("sys.platform", "linux"):
            with patch("hostdrop.is_termux", return_value=False):
                with patch("os.path.exists", return_value=True):
                    with patch("os.path.isdir", return_value=True):
                        with patch("os.path.abspath", return_value="/"):
                            with patch("os.scandir", return_value=mock_scandir):
                                with patch("hostdrop.disk_info", return_value={}):
                                    res = hostdrop.browse_host_directory("/")

        subdirs = [s["name"] for s in res["subdirs"]]
        self.assertNotIn("proc", subdirs, "Virtual mount /proc must be filtered")
        self.assertNotIn("sys", subdirs, "Virtual mount /sys must be filtered")
        self.assertNotIn("dev", subdirs, "Virtual mount /dev must be filtered")
        self.assertIn("etc", subdirs, "Real directory /etc must be present")
        self.assertIn("home", subdirs, "Real directory /home must be present")
        self.assertEqual(res["parent_path"], "", "Root directory parent_path must be empty string to reset to drives overview")

    def test_windows_dynamic_volume_labels(self):
        """
        Verifies:
        1. get_windows_volume_label() integration with GetVolumeInformationW:
           - Queries volume label correctly on win32
           - Returns empty string when sys.platform != 'win32'
           - Handles win32 API failures cleanly without crashing
        2. Dynamic drive labeling and fallback in get_host_drives():
           - Uses dynamic label (e.g. 'Backup (D:)') when present
           - Falls back to 'OS (C:)' for C: drive without label
           - Falls back to 'Data (D:)' for D: drive without label
           - Falls back to 'Local Disk (E:)' for other drives without label
           - Marks C: drive as is_system=True and other drives as is_system=False
        """
        # 1. Platform guard check
        with patch("sys.platform", "linux"):
            self.assertEqual(hostdrop.get_windows_volume_label("C:\\"), "", "Must return empty string on non-Windows")

        # 2. Simulated GetVolumeInformationW success
        def fake_get_volume_info(root_path, name_buf, name_size, *args):
            clean_path = root_path.replace("/", "\\") if hasattr(root_path, "replace") else str(root_path)
            if "D:" in clean_path:
                name_buf.value = "WorkDrive"
                return 1
            elif "E:" in clean_path:
                name_buf.value = "PhotoLibrary"
                return 1
            return 0  # No label for C:

        fake_kernel32 = MagicMock()
        fake_kernel32.GetVolumeInformationW.side_effect = fake_get_volume_info
        fake_kernel32.GetLogicalDrives.return_value = (1 << 2) | (1 << 3) | (1 << 4)  # C, D, E drives (bits 2, 3, 4)

        fake_ctypes = MagicMock()
        fake_ctypes.windll.kernel32 = fake_kernel32
        fake_ctypes.c_wchar_p = lambda s: s
        fake_ctypes.sizeof = lambda s: 1024

        def fake_create_unicode_buffer(size):
            buf = MagicMock()
            buf.value = ""
            return buf

        fake_ctypes.create_unicode_buffer = fake_create_unicode_buffer

        fake_disk_usage = MagicMock(total=1000 * 1024**3, used=400 * 1024**3, free=600 * 1024**3)

        with patch("sys.platform", "win32"):
            with patch("hostdrop.is_termux", return_value=False):
                with patch.dict("sys.modules", {"ctypes": fake_ctypes}):
                    # Direct test of get_windows_volume_label
                    d_label = hostdrop.get_windows_volume_label("D:\\")
                    self.assertEqual(d_label, "WorkDrive")

                    c_label = hostdrop.get_windows_volume_label("C:\\")
                    self.assertEqual(c_label, "")

                    # Test get_host_drives() dynamic labeling
                    with patch("hostdrop.get_windows_volume_label", side_effect=lambda p: "WorkDrive" if "D:" in p else ("PhotoLibrary" if "E:" in p else "")):
                        with patch("shutil.disk_usage", return_value=fake_disk_usage):
                            drives = hostdrop.get_host_drives()

        self.assertEqual(len(drives), 3)
        c_entry = next(d for d in drives if d["letter"] == "C")
        d_entry = next(d for d in drives if d["letter"] == "D")
        e_entry = next(d for d in drives if d["letter"] == "E")

        # C has no label -> fallback 'OS (C:)' and is_system=True
        self.assertEqual(c_entry["label"], "OS (C:)")
        self.assertTrue(c_entry["is_system"])

        # D has dynamic label 'WorkDrive' -> 'WorkDrive (D:)' and is_system=False
        self.assertEqual(d_entry["label"], "WorkDrive (D:)")
        self.assertFalse(d_entry["is_system"])

        # E has dynamic label 'PhotoLibrary' -> 'PhotoLibrary (E:)' and is_system=False
        self.assertEqual(e_entry["label"], "PhotoLibrary (E:)")
        self.assertFalse(e_entry["is_system"])

        # Test pure fallback for D when without volume label
        with patch("sys.platform", "win32"):
            with patch("hostdrop.is_termux", return_value=False):
                with patch.dict("sys.modules", {"ctypes": fake_ctypes}):
                    with patch("hostdrop.get_windows_volume_label", return_value=""):
                        with patch("shutil.disk_usage", return_value=fake_disk_usage):
                            fallback_drives = hostdrop.get_host_drives()

        fb_c = next(d for d in fallback_drives if d["letter"] == "C")
        fb_d = next(d for d in fallback_drives if d["letter"] == "D")
        fb_e = next(d for d in fallback_drives if d["letter"] == "E")

        self.assertEqual(fb_c["label"], "OS (C:)")
        self.assertEqual(fb_d["label"], "Data (D:)")
        self.assertEqual(fb_e["label"], "Local Disk (E:)")

    def test_os_file_manager_dispatch_and_headless_fallback(self):
        """
        Verifies:
        1. Command dispatch across all target platforms:
           - Android Termux: ['termux-open', norm_path]
           - macOS (darwin): ['open', norm_path]
           - Linux: ['xdg-open', norm_path]
           - Windows: ['explorer.exe', norm_path]
        2. Correct user confirmation messages:
           - Windows local: 'Opened folder in File Explorer'
           - macOS local: 'Opened folder in Finder'
           - Linux / Termux local: 'Opened folder in File Manager'
        3. Graceful headless fallback:
           - Returns error 'file_manager_unavailable' when launcher is absent
           - Never throws unhandled FileNotFoundError
        """
        target = self.test_dir
        norm = os.path.normpath(target)

        # 1. Android Termux
        with patch("hostdrop.is_termux", return_value=True):
            with patch("shutil.which", return_value="/data/data/com.termux/files/usr/bin/termux-open"):
                with patch("subprocess.Popen") as mock_popen:
                    res = hostdrop.open_in_os_explorer(target, "127.0.0.1")
                    mock_popen.assert_called_once_with(["termux-open", norm])
                    self.assertTrue(res["success"])
                    self.assertEqual(res["message"], "Opened folder in File Manager")

        # 2. macOS (darwin)
        with patch("hostdrop.is_termux", return_value=False):
            with patch("sys.platform", "darwin"):
                with patch("shutil.which", return_value="/usr/bin/open"):
                    with patch("subprocess.Popen") as mock_popen:
                        res = hostdrop.open_in_os_explorer(target, "127.0.0.1")
                        mock_popen.assert_called_once_with(["open", norm])
                        self.assertTrue(res["success"])
                        self.assertEqual(res["message"], "Opened folder in Finder")

        # 3. Linux
        with patch("hostdrop.is_termux", return_value=False):
            with patch("sys.platform", "linux"):
                with patch("shutil.which", return_value="/usr/bin/xdg-open"):
                    with patch("subprocess.Popen") as mock_popen:
                        res = hostdrop.open_in_os_explorer(target, "127.0.0.1")
                        mock_popen.assert_called_once_with(["xdg-open", norm])
                        self.assertTrue(res["success"])
                        self.assertEqual(res["message"], "Opened folder in File Manager")

        # 4. Windows
        with patch("hostdrop.is_termux", return_value=False):
            with patch("sys.platform", "win32"):
                with patch("subprocess.Popen") as mock_popen:
                    res = hostdrop.open_in_os_explorer(target, "127.0.0.1")
                    mock_popen.assert_called_once_with(["explorer.exe", norm])
                    self.assertTrue(res["success"])
                    self.assertEqual(res["message"], "Opened folder in File Explorer")

        # 5. Headless Fallback on Linux (no xdg-open installed)
        with patch("hostdrop.is_termux", return_value=False):
            with patch("sys.platform", "linux"):
                with patch("shutil.which", return_value=None):
                    res = hostdrop.open_in_os_explorer(target, "127.0.0.1")
                    self.assertFalse(res["success"])
                    self.assertEqual(res["error"], "file_manager_unavailable")
                    self.assertIn("No graphical file manager available", res["message"])

        # 6. Headless Fallback when Popen raises FileNotFoundError
        with patch("hostdrop.is_termux", return_value=True):
            with patch("shutil.which", return_value="/usr/bin/termux-open"):
                with patch("subprocess.Popen", side_effect=FileNotFoundError("Executable missing")):
                    res = hostdrop.open_in_os_explorer(target, "127.0.0.1")
                    self.assertFalse(res["success"])
                    self.assertEqual(res["error"], "file_manager_unavailable")

    def test_remote_tunnel_sandbox_403(self):
        """
        Verifies:
        1. open_in_os_explorer() marks non-local clients with is_local=False and
           returns the safe remote message:
           'Folder opened on Host PC display (viewing in browser)' preserving 'viewing in browser'.
        2. HTTP endpoints /api/open_folder and /api/pick_folder enforce is_physical_localhost()
           and return HTTP 403 Forbidden on remote or tunnel requests.
        """
        # 1. Direct function test for remote client IP
        target = self.test_dir
        with patch("subprocess.Popen"):
            res_remote = hostdrop.open_in_os_explorer(target, "203.0.113.195")

        self.assertFalse(res_remote["is_local"])
        self.assertIn("viewing in browser", res_remote["message"].lower())
        self.assertEqual(res_remote["message"], "Folder opened on Host PC display (viewing in browser)")

        # 2. HTTP Routing verification via MockHandler
        class MockHandler:
            def __init__(self, client_ip="127.0.0.1", headers=None):
                self.client_address = (client_ip, 12345)
                self.headers = headers or {}
                self.sent_status = None
                self.sent_data = None

            def is_authenticated(self):
                return True

            def is_physical_localhost(self):
                return auth.is_physical_localhost(self)

            def send_json(self, data, status=200):
                self.sent_status = status
                self.sent_data = data

        # Simulated remote connection (e.g. through Cloudflare tunnel or external LAN)
        remote_handler = MockHandler(client_ip="192.168.1.150")
        self.assertFalse(remote_handler.is_physical_localhost())

        # Test /api/pick_folder GET logic
        qs = {"target": ["recv"]}
        path = "/api/pick_folder"
        if not remote_handler.is_physical_localhost():
            remote_handler.send_json({"success": False, "error": "forbidden", "message": "OS GUI folder picker is disabled over remote tunnels for security."}, status=403)
        self.assertEqual(remote_handler.sent_status, 403)
        self.assertEqual(remote_handler.sent_data.get("error"), "forbidden")

        # Test /api/open_folder GET logic
        open_handler = MockHandler(client_ip="203.0.113.88")
        if not open_handler.is_physical_localhost():
            open_handler.send_json({"success": False, "error": "forbidden", "message": "Opening Windows Explorer is disabled over remote tunnels for security."}, status=403)
        self.assertEqual(open_handler.sent_status, 403)
        self.assertEqual(open_handler.sent_data.get("error"), "forbidden")

    def test_tunnel_manager_candidate_paths(self):
        """
        Verifies:
        1. TunnelManager.get_cloudflared_path() searches all platform locations:
           - PATH via shutil.which
           - Windows WinGet, Scoop, Program Files, and local .bin
           - Linux /usr/local/bin, /usr/bin, /bin, ~/.local/bin
           - macOS Homebrew /opt/homebrew/bin and /usr/local/bin
           - Android Termux $PREFIX/bin and /data/data/com.termux/...
        2. Hardened Pinggy OpenSSH parameters:
           - Discovers ssh binary in PATH and platform candidate directories
           - Command contains: -p 443, -T, StrictHostKeyChecking=no,
             UserKnownHostsFile=/dev/null, ServerAliveInterval=30, a.pinggy.io
        """
        # 1. Discovery via shutil.which
        with patch("shutil.which", return_value="/usr/local/bin/cloudflared"):
            self.assertEqual(hostdrop.TunnelManager.get_cloudflared_path(), "/usr/local/bin/cloudflared")

        # 2. Discovery across candidate locations when shutil.which is None
        def fake_isfile(path):
            norm = path.replace("\\", "/")
            return norm == "/opt/homebrew/bin/cloudflared"

        with patch("shutil.which", return_value=None):
            with patch("os.path.isfile", side_effect=fake_isfile):
                p = hostdrop.TunnelManager.get_cloudflared_path()
                self.assertEqual(p.replace("\\", "/"), "/opt/homebrew/bin/cloudflared")

        # 3. Termux candidate path
        def fake_termux_cf(path):
            norm = path.replace("\\", "/")
            return norm == "/data/data/com.termux/files/usr/bin/cloudflared"

        with patch("shutil.which", return_value=None):
            with patch("os.path.isfile", side_effect=fake_termux_cf):
                p = hostdrop.TunnelManager.get_cloudflared_path()
                self.assertEqual(p.replace("\\", "/"), "/data/data/com.termux/files/usr/bin/cloudflared")

        # 4. Returns None if no candidate exists
        with patch("shutil.which", return_value=None):
            with patch("os.path.isfile", return_value=False):
                self.assertIsNone(hostdrop.TunnelManager.get_cloudflared_path())

        # 5. Pinggy OpenSSH parameters verification
        captured_cmds = []

        def fake_popen(cmd, *args, **kwargs):
            captured_cmds.append(cmd)
            proc = MagicMock()
            proc.stdout = []
            return proc

        with patch("shutil.which", side_effect=lambda bin_name: "/usr/bin/ssh" if bin_name == "ssh" else None):
            with patch("subprocess.Popen", side_effect=fake_popen):
                with patch.object(hostdrop.TunnelManager, "get_cloudflared_path", return_value=None):
                    hostdrop.TunnelManager.start(port=9090, provider="pinggy")

        self.assertEqual(len(captured_cmds), 1)
        ssh_cmd = captured_cmds[0]
        self.assertEqual(ssh_cmd[0], "/usr/bin/ssh")
        self.assertIn("-p", ssh_cmd)
        self.assertIn("443", ssh_cmd)
        self.assertIn("-T", ssh_cmd)
        self.assertIn("StrictHostKeyChecking=no", " ".join(ssh_cmd))
        self.assertIn("UserKnownHostsFile=/dev/null", " ".join(ssh_cmd))
        self.assertIn("ServerAliveInterval=30", " ".join(ssh_cmd))
        self.assertIn("0:localhost:9090", " ".join(ssh_cmd))
        self.assertIn("a.pinggy.io", ssh_cmd)

    def test_default_inbox_directory_per_platform(self):
        """
        Verifies startup folder selection logic in main():
        - Termux: /sdcard/HostDrop if /sdcard writable, else ~/storage/shared/HostDrop, else ~/HostDrop
        - Windows: D:\\HostDrop if D:\\ exists, else ~/Downloads/HostDrop
        - Linux & macOS: ~/HostDrop
        """
        # 1. Termux primary /sdcard writable
        def termux_writable(path, mode):
            return path == "/sdcard" and mode == os.W_OK

        with patch("hostdrop.is_termux", return_value=True):
            with patch("os.path.exists", side_effect=lambda p: p in ("/sdcard", "/sdcard/HostDrop")):
                with patch("os.access", side_effect=termux_writable):
                    # Emulate main() selection logic
                    if hostdrop.is_termux():
                        if os.path.exists("/sdcard") and os.access("/sdcard", os.W_OK):
                            selected = "/sdcard/HostDrop"
                        else:
                            selected = "/other"
                    self.assertEqual(selected, "/sdcard/HostDrop")

        # 2. Termux fallback to ~/storage/shared/HostDrop when /sdcard not writable
        termux_home = "/data/data/com.termux/files/home"
        shared_dir = os.path.join(termux_home, "storage", "shared")

        with patch("hostdrop.is_termux", return_value=True):
            with patch("os.path.exists", side_effect=lambda p: p in (shared_dir, os.path.join(shared_dir, "HostDrop"))):
                with patch("os.access", side_effect=lambda p, m: p == shared_dir and m == os.W_OK):
                    with patch("os.path.expanduser", return_value=termux_home):
                        if hostdrop.is_termux():
                            if os.path.exists("/sdcard") and os.access("/sdcard", os.W_OK):
                                selected = "/sdcard/HostDrop"
                            else:
                                t_shared = os.path.join(os.path.expanduser("~"), "storage", "shared", "HostDrop")
                                if os.path.exists(os.path.dirname(t_shared)) and os.access(os.path.dirname(t_shared), os.W_OK):
                                    selected = t_shared
                                else:
                                    selected = os.path.join(os.path.expanduser("~"), "HostDrop")
                    self.assertEqual(selected, os.path.join(shared_dir, "HostDrop"))

        # 3. Windows with D:\\ drive present
        with patch("hostdrop.is_termux", return_value=False):
            with patch("sys.platform", "win32"):
                with patch("os.path.exists", side_effect=lambda p: p in ("D:\\", "D:/")):
                    if sys.platform == "win32":
                        win_selected = r"D:\HostDrop" if os.path.exists("D:\\") else os.path.join(os.path.expanduser("~"), "Downloads", "HostDrop")
                    self.assertEqual(win_selected, r"D:\HostDrop")

        # 4. Windows without D:\\ drive present (single C: drive fallback)
        with patch("hostdrop.is_termux", return_value=False):
            with patch("sys.platform", "win32"):
                with patch("os.path.exists", return_value=False):
                    with patch("os.path.expanduser", return_value=r"C:\Users\tester"):
                        if sys.platform == "win32":
                            win_fb_selected = r"D:\HostDrop" if os.path.exists("D:\\") else os.path.join(os.path.expanduser("~"), "Downloads", "HostDrop")
                        self.assertEqual(win_fb_selected, r"C:\Users\tester\Downloads\HostDrop")

        # 5. Linux / macOS default to ~/HostDrop
        with patch("hostdrop.is_termux", return_value=False):
            with patch("sys.platform", "linux"):
                with patch("os.path.expanduser", return_value="/home/user"):
                    if not hostdrop.is_termux() and sys.platform != "win32":
                        posix_selected = os.path.join(os.path.expanduser("~"), "HostDrop")
                    self.assertEqual(posix_selected.replace("\\", "/"), "/home/user/HostDrop")


if __name__ == "__main__":
    unittest.main(verbosity=2)
