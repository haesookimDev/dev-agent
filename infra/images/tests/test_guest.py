import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from infra.images import guest, prepare

ROOT = Path(__file__).resolve().parents[3]


class GuestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kelpie-guest-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, members, *, name="browser.zip"):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as archive:
            for filename, mode, content in members:
                info = zipfile.ZipInfo(filename)
                info.external_attr = mode << 16
                archive.writestr(info, content)
        return path

    def test_extracts_browser_without_privileged_modes(self):
        archive = self.archive([
            ("chrome-linux64/chrome", stat.S_IFREG | 0o6755, b"synthetic executable"),
            ("chrome-linux64/locales/ko.pak", stat.S_IFREG | 0o666, b"synthetic locale"),
        ])
        target = self.root / "browser"
        binary = guest.extract_browser(archive, target)
        self.assertEqual(binary.read_bytes(), b"synthetic executable")
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((target / "chrome-linux64/locales/ko.pak").stat().st_mode),
                         0o644)

    def test_rejects_browser_path_escapes_and_special_files_before_extraction(self):
        for index, (name, mode) in enumerate([
            ("/outside", stat.S_IFREG), ("chrome-linux64/../../outside", stat.S_IFREG),
            ("other/chrome", stat.S_IFREG), ("chrome-linux64\\outside", stat.S_IFREG),
            ("chrome-linux64/link", stat.S_IFLNK), ("chrome-linux64/fifo", stat.S_IFIFO),
            ("chrome-linux64/device", stat.S_IFBLK),
        ]):
            with self.subTest(name=name):
                source = self.archive([(name, mode | 0o644, b"synthetic")],
                                      name=f"case-{index}.zip")
                target = self.root / f"out-{index}"
                with self.assertRaises(prepare.InputError):
                    guest.extract_browser(source, target)
                self.assertFalse(target.exists())

    def test_rejects_browser_aliases(self):
        source = self.archive([
            ("chrome-linux64/chrome", stat.S_IFREG | 0o755, b"one"),
            ("chrome-linux64/Chrome", stat.S_IFREG | 0o755, b"two"),
        ])
        with self.assertRaisesRegex(prepare.InputError, "duplicate browser archive path"):
            guest.extract_browser(source, self.root / "out")

    def test_limits_browser_expansion_before_creating_output(self):
        source = self.archive([("chrome-linux64/chrome", stat.S_IFREG | 0o755, b"long")])
        with patch.object(guest, "MAX_EXPANDED_BROWSER_BYTES", 3):
            with self.assertRaisesRegex(prepare.InputError, "browser expansion limit exceeded"):
                guest.extract_browser(source, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_browser_requires_executable_without_overwriting_existing_output(self):
        source = self.archive([("chrome-linux64/chrome", stat.S_IFREG | 0o644, b"synthetic")])
        with self.assertRaisesRegex(prepare.InputError, "browser executable is missing"):
            guest.extract_browser(source, self.root / "out")
        sentinel = self.root / "existing"
        sentinel.mkdir()
        (sentinel / "keep").write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            guest.extract_browser(source, sentinel)
        self.assertEqual((sentinel / "keep").read_bytes(), b"preserve")

    def test_wheel_metadata_must_match_locked_identity(self):
        wheel = self.archive([
            ("kelpie_vm_runner-0.1.0.dist-info/METADATA", stat.S_IFREG | 0o644,
             b"Metadata-Version: 2.1\nName: kelpie-vm-runner\nVersion: 0.1.0\n"),
        ], name="runner.whl")
        guest.verify_wheel(wheel, {"name": "kelpie-vm-runner", "version": "0.1.0"})
        for item in ({"name": "another-package", "version": "0.1.0"},
                     {"name": "kelpie-vm-runner", "version": "0.2.0"}):
            with self.assertRaisesRegex(prepare.InputError, "wheel identity differs from lock"):
                guest.verify_wheel(wheel, item)

    def test_rejects_ambiguous_wheel_metadata(self):
        for index, entries in enumerate([
            [],
            [("a.dist-info/METADATA", stat.S_IFREG | 0o644,
              b"Name: runner\nName: runner\nVersion: 1\n")],
            [("a.dist-info/METADATA", stat.S_IFREG | 0o644, b"Name: a\nVersion: 1\n"),
             ("b.dist-info/METADATA", stat.S_IFREG | 0o644, b"Name: b\nVersion: 1\n")],
        ]):
            source = self.archive(entries, name=f"wheel-{index}.whl")
            with self.assertRaisesRegex(prepare.InputError, "invalid wheel metadata"):
                guest.verify_wheel(source, {"name": "runner", "version": "1"})

    def test_real_cli_refuses_host_mutation_before_any_guest_operations(self):
        # Never let this negative probe run as root, even inside a real build VM.
        self.assertNotEqual(os.geteuid(), 0, "run the image test suite as a non-root user")
        for phase in ("install", "seal"):
            with self.subTest(phase=phase):
                result = subprocess.run(
                    [sys.executable, str(ROOT / "infra/images/guest.py"), phase],
                    cwd=ROOT, capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("image guest rejected:", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_install_and_seal_call_guard_before_commands_or_writes(self):
        for function in (guest.install, guest.seal):
            with self.subTest(function=function.__name__):
                with patch.object(guest, "guard_guest", side_effect=prepare.InputError("guard")), \
                        patch.object(guest, "command") as command, \
                        patch.object(guest, "write_file") as write_file:
                    with self.assertRaises(prepare.InputError):
                        function()
                    command.assert_not_called()
                    write_file.assert_not_called()

    def test_write_file_refuses_symlinks(self):
        target = self.root / "target"
        target.write_text("preserve")
        link = self.root / "link"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            guest.write_file(link, "overwrite")
        self.assertEqual(target.read_text(), "preserve")

    def test_write_file_refuses_hardlinks_without_truncating_target(self):
        target = self.root / "target"
        target.write_text("preserve")
        link = self.root / "link"
        os.link(target, link)
        with self.assertRaisesRegex(prepare.InputError, "single-link regular file"):
            guest.write_file(link, "overwrite")
        self.assertEqual(target.read_text(), "preserve")

    def test_command_discards_diagnostics_and_ambient_credentials(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-not-a-real-key"}), \
                patch.object(guest.subprocess, "run") as run:
            guest.command("synthetic-command")
        kwargs = run.call_args.kwargs
        self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertFalse(kwargs.get("shell", False))


if __name__ == "__main__":
    unittest.main()
