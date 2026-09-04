import unittest
import os
import sys
import shutil
import tempfile
import string
import subprocess
from unittest.mock import patch, MagicMock, mock_open

# Ensure project root is on sys.path
ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
import hostdrop


class TestDriveEnumerationContract(unittest.TestCase):
    """
    Adversarial contract verification for get_host_drives() across all platforms.
    Every drive entry must strictly contain all expected keys with valid types.
    """

    REQUIRED_KEYS = {
        "path": str,
        "name": str,
        "label": str,
        "letter": str,
        "total": int,
        "used": int,
        "free": int,
        "percent": (int, float),
        "free_gb": str,
        "total_gb": str,
        "used_gb": str,
        "used_pct": int,
        "used_percent": (int, float),
        "is_system": bool
    }

    def _verify_drive_entry(self, entry):
        for key, expected_type in self.REQUIRED_KEYS.items():
            self.assertIn(key, entry, f"Missing required key: '{key}' in drive entry: {entry}")
            val = entry[key]
            self.assertIsInstance(val, expected_type, f"Key '{key}' has type {type(val)}, expected {expected_type}")
        
        # Invariant constraints
        self.assertGreaterEqual(entry["total"], 0, "total bytes must be >= 0")
        self.assertGreaterEqual(entry["used"], 0, "used bytes must be >= 0")
        self.assertGreaterEqual(entry["free"], 0, "free bytes must be >= 0")
        self.assertGreaterEqual(entry["percent"], 0.0, "percent must be >= 0.0")
        self.assertLessEqual(entry["percent"], 100.0, "percent must be <= 100.0")
        self.assertGreaterEqual(entry["used_pct"], 0, "used_pct must be >= 0")
        self.assertLessEqual(entry["used_pct"], 100, "used_pct must be <= 100")
        self.assertTrue(len(entry["letter"]) > 0, "letter must not be empty")

    def test_live_environment_contract(self):
        """Verify contract on actual current host environment."""
        drives = hostdrop.get_host_drives()
        self.assertIsInstance(drives, list)
        self.assertGreater(len(drives), 0, "Must return at least one drive")
        for d in drives:
            self._verify_drive_entry(d)

    def test_disk_usage_failure_graceful_recovery(self):
        """When disk_usage fails with OSError or PermissionError, entries must still satisfy contract."""
        with patch("shutil.disk_usage", side_effect=OSError("Disk unreadable / IO Error")):
            drives = hostdrop.get_host_drives()
            self.assertGreater(len(drives), 0)
            for d in drives:
                self._verify_drive_entry(d)
                self.assertEqual(d["total"], 0)
                self.assertEqual(d["used"], 0)
                self.assertEqual(d["free"], 0)
                self.assertEqual(d["percent"], 0.0)
                self.assertEqual(d["free_gb"], "?")
                self.assertEqual(d["total_gb"], "?")
                self.assertEqual(d["used_gb"], "?")
                self.assertEqual(d["used_pct"], 0)

    def test_zero_capacity_division_by_zero_prevention(self):
        """When total disk capacity is reported as 0, percent calculation must not throw ZeroDivisionError."""
        fake_usage = MagicMock(total=0, used=0, free=0)
        with patch("shutil.disk_usage", return_value=fake_usage):
            drives = hostdrop.get_host_drives()
            for d in drives:
                self._verify_drive_entry(d)
                self.assertEqual(d["percent"], 0.0)
                self.assertEqual(d["used_pct"], 0)


class TestTermuxSimulation(unittest.TestCase):
    """
    Simulated Android Termux environment testing.
    Validates detection triggers, storage discovery, permission handling, and mount paths.
    """

    def test_termux_detection_triggers(self):
        """Test all 4 detection conditions for is_termux()."""
        # 1. TERMUX_VERSION env var
        with patch.dict(os.environ, {"TERMUX_VERSION": "0.118.0"}, clear=False):
            self.assertTrue(hostdrop.is_termux())

        # 2. /data/data/com.termux directory
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", side_effect=lambda p: p == "/data/data/com.termux"):
                self.assertTrue(hostdrop.is_termux())

        # 3. PREFIX with com.termux
        with patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr"}, clear=True):
            self.assertTrue(hostdrop.is_termux())

        # 4. sys.getandroidapilevel
        with patch.dict(os.environ, {}, clear=True):
            with patch.object(sys, "getandroidapilevel", create=True, return_value=33):
                self.assertTrue(hostdrop.is_termux())

        # 5. Non-Termux clean environment
        with patch.dict(os.environ, {}, clear=True):
            with patch("os.path.exists", return_value=False):
                if hasattr(sys, "getandroidapilevel"):
                    delattr(sys, "getandroidapilevel")
                self.assertFalse(hostdrop.is_termux())

    def test_termux_drive_enumeration_and_keys(self):
        """Under Termux, must expose Internal Storage (/sdcard), Downloads, Termux Home, and OTG mounts."""
        mock_paths = {
            "/sdcard": True,
            "/sdcard/Download": True,
            os.path.expanduser("~"): True,
            "/storage": True,
            "/storage/ABCD-1234": True,
            "/": True
        }

        def mock_exists(p):
            norm = os.path.normpath(p)
            return any(os.path.normpath(k) == norm for k in mock_paths if mock_paths[k])

        def mock_isdir(p):
            return mock_exists(p)

        def mock_listdir(p):
            if p == "/storage":
                return ["emulated", "self", "ABCD-1234", ".hidden"]
            return []

        with patch("hostdrop.is_termux", return_value=True), \
             patch("os.path.exists", side_effect=mock_exists), \
             patch("os.path.isdir", side_effect=mock_isdir), \
             patch("os.listdir", side_effect=mock_listdir), \
             patch("os.access", return_value=True):
            
            drives = hostdrop.get_host_drives()
            self.assertGreaterEqual(len(drives), 4)
            
            letters = [d["letter"] for d in drives]
            self.assertIn("SD", letters, "Must contain 'SD' for internal storage")
            self.assertIn("DL", letters, "Must contain 'DL' for downloads")
            self.assertIn("TH", letters, "Must contain 'TH' for Termux home")

            # Check specific path mappings
            sd_drive = next(d for d in drives if d["letter"] == "SD" and "Internal Storage" in d["name"])
            self.assertEqual(sd_drive["path"], "/sdcard")
            self.assertFalse(sd_drive["is_system"])
            self.assertEqual(sd_drive["label"], "Internal Storage (/sdcard)")

            dl_drive = next(d for d in drives if d["letter"] == "DL")
            self.assertTrue(dl_drive["path"].endswith("Download"))
            self.assertFalse(dl_drive["is_system"])

            th_drive = next(d for d in drives if d["letter"] == "TH")
            self.assertTrue(th_drive["is_system"])
            self.assertEqual(th_drive["label"], "Termux Home (~)")

            otg_drive = next((d for d in drives if "ABCD-1234" in d["name"]), None)
            self.assertIsNotNone(otg_drive, "Must enumerate MicroSD/OTG mount /storage/ABCD-1234")
            self.assertEqual(otg_drive["letter"], "SD")

    def test_termux_storage_permission_denied_guidance(self):
        """When accessing /sdcard or /storage throws PermissionError, user must receive helpful Termux instructions."""
        with patch("hostdrop.is_termux", return_value=True):
            with patch("os.path.exists", return_value=True), \
                 patch("os.path.isdir", return_value=True), \
                 patch("os.scandir", side_effect=PermissionError("Permission denied")):
                
                res = hostdrop.browse_host_directory("/sdcard")
                self.assertFalse(res["is_root"])
                self.assertIn("termux-setup-storage", res.get("error", ""))


class TestPOSIXSimulation(unittest.TestCase):
    """
    Simulated Linux and macOS (Darwin) environments testing.
    Validates root (/), home (~), mount points (/Volumes, /media, /run/media, /mnt),
    virtual directory filtering (/proc, /sys, /dev), and parent navigation.
    """

    def test_linux_drive_enumeration(self):
        """Simulate Linux environment without Termux."""
        def mock_exists(p):
            norm = os.path.normpath(p)
            valid = ["/", os.path.expanduser("~"), "/media", "/mnt", "/run/media"]
            return any(norm == os.path.normpath(v) or norm.startswith("/media/") for v in valid)

        def mock_listdir(p):
            if p == "/media":
                return ["usb_drive_1", "broken_symlink"]
            return []

        def mock_islink(p):
            return "broken_symlink" in p

        def mock_isdir(p):
            return "usb_drive_1" in p

        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "linux"), \
             patch("os.path.exists", side_effect=mock_exists), \
             patch("os.listdir", side_effect=mock_listdir), \
             patch("os.path.islink", side_effect=mock_islink), \
             patch("os.path.isdir", side_effect=mock_isdir):

            drives = hostdrop.get_host_drives()
            self.assertGreaterEqual(len(drives), 2)

            root_drive = next(d for d in drives if d["path"] == "/")
            self.assertEqual(root_drive["letter"], "/")
            self.assertEqual(root_drive["label"], "Root (/)")
            self.assertTrue(root_drive["is_system"])

            home_drive = next(d for d in drives if d["path"] == os.path.expanduser("~"))
            self.assertEqual(home_drive["letter"], "~")
            self.assertEqual(home_drive["label"], "Home (~)")
            self.assertFalse(home_drive["is_system"])

            media_drive = next((d for d in drives if "usb_drive_1" in d["path"]), None)
            self.assertIsNotNone(media_drive, "Must discover /media/usb_drive_1")
            self.assertFalse(any("broken_symlink" in d["path"] for d in drives), "Must ignore symlinks")

    def test_macos_drive_enumeration(self):
        """Simulate macOS (darwin) with /Volumes mounts."""
        def mock_exists(p):
            norm = os.path.normpath(p)
            valid = ["/", os.path.expanduser("~"), "/Volumes"]
            return any(norm == os.path.normpath(v) or norm.startswith("/Volumes/") for v in valid)

        def mock_listdir(p):
            if p == "/Volumes":
                return ["Macintosh HD", "ExternalBackup"]
            return []

        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "darwin"), \
             patch("os.path.exists", side_effect=mock_exists), \
             patch("os.listdir", side_effect=mock_listdir), \
             patch("os.path.islink", return_value=False), \
             patch("os.path.isdir", return_value=True):

            drives = hostdrop.get_host_drives()
            names = [d["name"] for d in drives]
            self.assertIn("Root (/)", names)
            self.assertIn("Home", names)
            self.assertIn("ExternalBackup", names)

    def test_posix_root_parent_navigation(self):
        """When browsing root directory '/', parent_path must be empty string to return to overview."""
        with tempfile.TemporaryDirectory() as td:
            # Test browsing a regular directory has a parent
            res_child = hostdrop.browse_host_directory(td)
            self.assertEqual(res_child["parent_path"], os.path.dirname(os.path.abspath(td)))

        # When path is root '/', parent_path must be ''
        with patch("os.path.abspath", return_value="/"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.isdir", return_value=True), \
             patch("os.path.dirname", return_value="/"), \
             patch("os.scandir", return_value=MagicMock(__enter__=MagicMock(return_value=[]), __exit__=MagicMock())):

            res_root = hostdrop.browse_host_directory("/")
            self.assertEqual(res_root["parent_path"], "", "Browsing '/' must set parent_path to ''")

    def test_virtual_directory_filtering_on_posix(self):
        """On Linux/macOS browsing root, /proc, /sys, /dev must be filtered out for security and speed."""
        mock_entries = []
        for name in ["proc", "sys", "dev", "etc", "home", "var"]:
            entry = MagicMock()
            entry.name = name
            entry.is_dir.return_value = True
            entry.is_file.return_value = False
            entry.is_symlink.return_value = False
            entry.path = f"/{name}"
            mock_entries.append(entry)

        with patch("sys.platform", "linux"), \
             patch("hostdrop.is_termux", return_value=False), \
             patch("os.path.abspath", return_value="/"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.isdir", return_value=True), \
             patch("os.scandir", return_value=MagicMock(__enter__=MagicMock(return_value=mock_entries), __exit__=MagicMock())):

            res = hostdrop.browse_host_directory("/")
            subnames = [s["name"] for s in res["subdirs"]]
            self.assertNotIn("proc", subnames, "/proc should be filtered out")
            self.assertNotIn("sys", subnames, "/sys should be filtered out")
            self.assertNotIn("dev", subnames, "/dev should be filtered out")
            self.assertIn("etc", subnames)
            self.assertIn("home", subnames)

    def test_windows_system_folder_filtering(self):
        """On Windows, system protected folders like Recovery and System Volume Information must be filtered."""
        mock_entries = []
        for name in ["$Recycle.Bin", "System Volume Information", "Recovery", "Users", "Program Files"]:
            entry = MagicMock()
            entry.name = name
            entry.is_dir.return_value = True
            entry.is_file.return_value = False
            entry.is_symlink.return_value = False
            entry.path = f"C:\\{name}"
            mock_entries.append(entry)

        with patch("sys.platform", "win32"), \
             patch("hostdrop.is_termux", return_value=False), \
             patch("os.path.abspath", return_value="C:\\"), \
             patch("os.path.exists", return_value=True), \
             patch("os.path.isdir", return_value=True), \
             patch("os.scandir", return_value=MagicMock(__enter__=MagicMock(return_value=mock_entries), __exit__=MagicMock())):

            res = hostdrop.browse_host_directory("C:\\")
            subnames = [s["name"] for s in res["subdirs"]]
            self.assertNotIn("$Recycle.Bin", subnames)
            self.assertNotIn("System Volume Information", subnames)
            self.assertNotIn("Recovery", subnames)
            self.assertIn("Users", subnames)
            self.assertIn("Program Files", subnames)


class TestWindowsVolumeLabels(unittest.TestCase):
    """
    Dynamic volume label retrieval and Windows drive configuration tests.
    """

    def test_dynamic_volume_label_query(self):
        """Verify get_windows_volume_label extracts volume name via ctypes."""
        if sys.platform == "win32":
            # Test actual call on C:
            lbl = hostdrop.get_windows_volume_label("C:\\")
            self.assertIsInstance(lbl, str)

        # Test non-win32 returns empty string immediately
        with patch("sys.platform", "linux"):
            self.assertEqual(hostdrop.get_windows_volume_label("C:\\"), "")

    def test_windows_dynamic_labels_in_drive_enumeration(self):
        """When GetVolumeInformationW returns custom labels, verify drive entry reflects it."""
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "win32"):

            def mock_get_label(drive_path):
                if drive_path.startswith("C"):
                    return "WindowsOS"
                if drive_path.startswith("D"):
                    return "NVMeStorage"
                return ""

            # Bitmask 0b0000011 (C and D drives: 1<<2 and 1<<3 = 4 | 8 = 12)
            mock_bitmask = (1 << 2) | (1 << 3)
            with patch("ctypes.windll.kernel32.GetLogicalDrives", return_value=mock_bitmask, create=True), \
                 patch("hostdrop.get_windows_volume_label", side_effect=mock_get_label):

                drives = hostdrop.get_host_drives()
                self.assertEqual(len(drives), 2)

                c_drv = next(d for d in drives if d["letter"] == "C")
                self.assertEqual(c_drv["label"], "WindowsOS (C:)")
                self.assertTrue(c_drv["is_system"])

                d_drv = next(d for d in drives if d["letter"] == "D")
                self.assertEqual(d_drv["label"], "NVMeStorage (D:)")
                self.assertFalse(d_drv["is_system"])

    def test_windows_fallback_labels_when_query_fails(self):
        """When volume label query returns empty, fall back to OS (C:) and Data (D:)."""
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "win32"):

            mock_bitmask = (1 << 2) | (1 << 3) | (1 << 4)  # C, D, E
            with patch("ctypes.windll.kernel32.GetLogicalDrives", return_value=mock_bitmask, create=True), \
                 patch("hostdrop.get_windows_volume_label", return_value=""):

                drives = hostdrop.get_host_drives()
                c_drv = next(d for d in drives if d["letter"] == "C")
                d_drv = next(d for d in drives if d["letter"] == "D")
                e_drv = next(d for d in drives if d["letter"] == "E")

                self.assertEqual(c_drv["label"], "OS (C:)")
                self.assertEqual(d_drv["label"], "Data (D:)")
                self.assertEqual(e_drv["label"], "Local Disk (E:)")


class TestOSFileManagerLaunch(unittest.TestCase):
    """
    File explorer / manager command execution across platforms and headless fallbacks.
    """

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_windows_explorer_launch(self):
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "win32"), \
             patch("hostdrop.get_local_ip_set", return_value={"127.0.0.1"}), \
             patch("subprocess.Popen") as mock_popen:

            res = hostdrop.open_in_os_explorer(self.temp_dir, "127.0.0.1")
            self.assertTrue(res["success"])
            self.assertEqual(res["message"], "Opened folder in File Explorer")
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], "explorer.exe")
            self.assertEqual(args[1], os.path.normpath(self.temp_dir))

    def test_macos_open_launch(self):
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "darwin"), \
             patch("shutil.which", return_value="/usr/bin/open"), \
             patch("hostdrop.get_local_ip_set", return_value={"127.0.0.1"}), \
             patch("subprocess.Popen") as mock_popen:

            res = hostdrop.open_in_os_explorer(self.temp_dir, "127.0.0.1")
            self.assertTrue(res["success"])
            self.assertEqual(res["message"], "Opened folder in Finder")
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], "open")
            self.assertEqual(args[1], os.path.normpath(self.temp_dir))

    def test_linux_xdg_open_launch(self):
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "linux"), \
             patch("shutil.which", return_value="/usr/bin/xdg-open"), \
             patch("hostdrop.get_local_ip_set", return_value={"127.0.0.1"}), \
             patch("subprocess.Popen") as mock_popen:

            res = hostdrop.open_in_os_explorer(self.temp_dir, "127.0.0.1")
            self.assertTrue(res["success"])
            self.assertEqual(res["message"], "Opened folder in File Manager")
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], "xdg-open")
            self.assertEqual(args[1], os.path.normpath(self.temp_dir))

    def test_termux_termux_open_launch(self):
        with patch("hostdrop.is_termux", return_value=True), \
             patch("shutil.which", return_value="/data/data/com.termux/files/usr/bin/termux-open"), \
             patch("hostdrop.get_local_ip_set", return_value={"127.0.0.1"}), \
             patch("subprocess.Popen") as mock_popen:

            res = hostdrop.open_in_os_explorer(self.temp_dir, "127.0.0.1")
            self.assertTrue(res["success"])
            self.assertEqual(res["message"], "Opened folder in File Manager")
            mock_popen.assert_called_once()
            args = mock_popen.call_args[0][0]
            self.assertEqual(args[0], "termux-open")
            self.assertEqual(args[1], os.path.normpath(self.temp_dir))

    def test_headless_fallback_when_command_missing(self):
        """When xdg-open or open is missing in a headless environment, return graceful error dict."""
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "linux"), \
             patch("shutil.which", return_value=None), \
             patch("hostdrop.get_local_ip_set", return_value={"127.0.0.1"}):

            res = hostdrop.open_in_os_explorer(self.temp_dir, "127.0.0.1")
            self.assertFalse(res["success"])
            self.assertEqual(res["error"], "file_manager_unavailable")
            self.assertIn("No graphical file manager available", res["message"])

    def test_remote_client_display_message(self):
        """When called with a remote IP, must return viewer message with 'viewing in browser'."""
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "win32"), \
             patch("hostdrop.get_local_ip_set", return_value={"127.0.0.1"}), \
             patch("subprocess.Popen"):

            res = hostdrop.open_in_os_explorer(self.temp_dir, "192.168.1.100")
            self.assertTrue(res["success"])
            self.assertFalse(res["is_local"])
            self.assertIn("viewing in browser", res["message"])


class TestNativeFolderPicker(unittest.TestCase):
    """
    Test native folder picker dialogs, fallbacks, and alias.
    """

    def test_powershell_alias_contract(self):
        """pick_folder_powershell must be an exact alias of pick_folder_native for backward compatibility."""
        self.assertIs(hostdrop.pick_folder_powershell, hostdrop.pick_folder_native)

    def test_termux_returns_unsupported_platform(self):
        """Under Termux, must immediately return (None, 'unsupported_platform') to use in-browser modal."""
        with patch("hostdrop.is_termux", return_value=True):
            path, err = hostdrop.pick_folder_native()
            self.assertIsNone(path)
            self.assertEqual(err, "unsupported_platform")

    def test_windows_powershell_picker_success(self):
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "win32"), \
             patch("subprocess.run") as mock_run, \
             patch("os.path.isdir", return_value=True):

            mock_run.return_value = MagicMock(returncode=0, stdout="C:\\TestFolder\n", stderr="")
            chosen, err = hostdrop.pick_folder_native()
            self.assertEqual(chosen, "C:\\TestFolder")
            self.assertIsNone(err)

    def test_windows_powershell_picker_cancellation(self):
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "win32"), \
             patch("subprocess.run") as mock_run:

            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            with patch("tkinter.Tk", side_effect=Exception("No Tkinter")):
                chosen, err = hostdrop.pick_folder_native()
                self.assertIsNone(chosen)
                self.assertEqual(err, "cancelled")

    def test_macos_osascript_picker_success(self):
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "darwin"), \
             patch("subprocess.run") as mock_run, \
             patch("os.path.isdir", return_value=True):

            mock_run.return_value = MagicMock(returncode=0, stdout="/Users/test/Folder\n", stderr="")
            chosen, err = hostdrop.pick_folder_native()
            self.assertEqual(chosen, "/Users/test/Folder")
            self.assertIsNone(err)

    def test_linux_zenity_picker_success(self):
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "linux"), \
             patch("shutil.which", side_effect=lambda x: "/usr/bin/zenity" if x == "zenity" else None), \
             patch("subprocess.run") as mock_run, \
             patch("os.path.isdir", return_value=True):

            mock_run.return_value = MagicMock(returncode=0, stdout="/home/user/Folder\n", stderr="")
            chosen, err = hostdrop.pick_folder_native()
            self.assertEqual(chosen, "/home/user/Folder")
            self.assertIsNone(err)

    def test_picker_timeout_handling(self):
        """When user dialog times out, return (None, 'dialog_timeout')."""
        with patch("hostdrop.is_termux", return_value=False), \
             patch("sys.platform", "win32"), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="powershell", timeout=5)):

            with patch("tkinter.Tk", side_effect=Exception("No Tkinter")):
                chosen, err = hostdrop.pick_folder_native(timeout_sec=5)
                self.assertIsNone(chosen)
                self.assertEqual(err, "dialog_timeout")


class TestTunnelManagerCloudflaredPaths(unittest.TestCase):
    """
    Verify candidate path lookup across Windows, Linux, macOS, and Android Termux.
    """

    def test_path_lookup_via_shutil_which(self):
        """When cloudflared is present in system PATH, return it directly."""
        with patch("shutil.which", return_value="/usr/local/bin/cloudflared"):
            res = hostdrop.TunnelManager.get_cloudflared_path()
            self.assertEqual(res, "/usr/local/bin/cloudflared")

    def test_windows_winget_and_scoop_candidates(self):
        """Verify Windows candidates when which() returns None."""
        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {"LOCALAPPDATA": "C:\\Users\\test\\AppData\\Local", "USERPROFILE": "C:\\Users\\test"}):

            expected_winget = os.path.join("C:\\Users\\test\\AppData\\Local", "Microsoft", "WinGet", "Links", "cloudflared.exe")
            with patch("os.path.isfile", side_effect=lambda p: p == expected_winget):
                res = hostdrop.TunnelManager.get_cloudflared_path()
                self.assertEqual(res, expected_winget)

            expected_scoop = os.path.join("C:\\Users\\test", "scoop", "shims", "cloudflared.exe")
            with patch("os.path.isfile", side_effect=lambda p: p == expected_scoop):
                res = hostdrop.TunnelManager.get_cloudflared_path()
                self.assertEqual(res, expected_scoop)

    def test_linux_candidates(self):
        """Verify Linux standard candidates."""
        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {"LOCALAPPDATA": "", "USERPROFILE": ""}):

            for linux_path in ["/usr/local/bin/cloudflared", "/usr/bin/cloudflared", "/bin/cloudflared"]:
                with patch("os.path.isfile", side_effect=lambda p, target=linux_path: p == target):
                    res = hostdrop.TunnelManager.get_cloudflared_path()
                    self.assertEqual(res, linux_path)

    def test_macos_candidates(self):
        """Verify macOS Homebrew candidates."""
        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {"LOCALAPPDATA": "", "USERPROFILE": ""}):

            with patch("os.path.isfile", side_effect=lambda p: p == "/opt/homebrew/bin/cloudflared"):
                res = hostdrop.TunnelManager.get_cloudflared_path()
                self.assertEqual(res, "/opt/homebrew/bin/cloudflared")

    def test_termux_candidates(self):
        """Verify Android Termux candidate lookup."""
        with patch("shutil.which", return_value=None), \
             patch.dict(os.environ, {"PREFIX": "/data/data/com.termux/files/usr", "LOCALAPPDATA": "", "USERPROFILE": ""}):

            expected = "/data/data/com.termux/files/usr/bin/cloudflared"
            with patch("os.path.isfile", side_effect=lambda p: p == expected):
                res = hostdrop.TunnelManager.get_cloudflared_path()
                self.assertEqual(res, expected)

    def test_candidate_missing_returns_none(self):
        """When cloudflared does not exist anywhere, return None."""
        with patch("shutil.which", return_value=None), \
             patch("os.path.isfile", return_value=False):
            res = hostdrop.TunnelManager.get_cloudflared_path()
            self.assertIsNone(res)



class TestSecurityAndPathTraversal(unittest.TestCase):
    def test_is_physical_localhost_detection(self):
        handler = MagicMock()
        handler.client_address = ('127.0.0.1', 54321)
        handler.headers = {}
        self.assertTrue(hostdrop.HostDropHandler.is_physical_localhost(handler))
        for hdr in ['CF-Connecting-IP', 'X-Forwarded-For', 'X-Real-IP', 'Forwarded', 'True-Client-IP']:
            handler.headers = {hdr: '203.0.113.195'}
            self.assertFalse(hostdrop.HostDropHandler.is_physical_localhost(handler), f'Failed on {hdr}')
        handler.headers = {}
        handler.client_address = ('192.168.1.55', 54321)
        self.assertFalse(hostdrop.HostDropHandler.is_physical_localhost(handler))

    def test_browse_host_root_keywords(self):
        for kw in ['', 'roots', 'drives', 'root', 'ROOT', '   ']:
            res = hostdrop.browse_host_directory(kw)
            self.assertTrue(res['is_root'])
            self.assertEqual(res['current_path'], '')
            self.assertEqual(res['parent_path'], '')
            self.assertGreater(len(res['drives']), 0)

    def test_directory_not_found(self):
        res = hostdrop.open_in_os_explorer('C:\\non_existent_folder_xyz_12345', '127.0.0.1')
        self.assertFalse(res['success'])
        self.assertEqual(res['error'], 'directory_not_found')

if __name__ == "__main__":
    unittest.main(verbosity=2)
