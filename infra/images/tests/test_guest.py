import os
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from infra.images import browser_policy, build, codex_package, guest, prepare
from infra.images.tests import test_codex_package

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

    @staticmethod
    def elf(machine):
        header = bytearray(64)
        header[:16] = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
        header[16:18] = (2).to_bytes(2, "little")
        header[18:20] = machine.to_bytes(2, "little")
        header[20:24] = (1).to_bytes(4, "little")
        header[52:54] = (64).to_bytes(2, "little")
        return bytes(header) + b"synthetic fixture, never executable"

    def test_extracts_browser_without_privileged_modes(self):
        archive = self.archive([
            ("chrome-linux64/chrome", stat.S_IFREG | 0o6755, self.elf(62)),
            ("chrome-linux64/locales/ko.pak", stat.S_IFREG | 0o666, b"synthetic locale"),
        ])
        target = self.root / "browser"
        binary = guest.extract_browser(archive, target)
        self.assertEqual(binary.read_bytes(), self.elf(62))
        self.assertEqual(stat.S_IMODE(binary.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((target / "chrome-linux64/locales/ko.pak").stat().st_mode),
                         0o644)

    def test_extracts_arm64_browser_from_the_architecture_specific_root(self):
        archive = self.archive([
            ("chrome-linux-arm64/chrome", stat.S_IFREG | 0o755, self.elf(183)),
            ("chrome-linux-arm64/locales/en-US.pak", stat.S_IFREG | 0o644, b"locale"),
        ])
        target = self.root / "arm-browser"
        binary = guest.extract_browser(archive, target, "arm64")
        self.assertEqual(binary, target / "chrome-linux-arm64/chrome")
        self.assertEqual(binary.read_bytes(), self.elf(183))

    def test_rejects_cross_architecture_browser_roots_and_executables(self):
        for index, (architecture, archive_root, machine) in enumerate([
            ("amd64", "chrome-linux-arm64", 183),
            ("arm64", "chrome-linux64", 62),
        ]):
            with self.subTest(architecture=architecture, kind="root"):
                source = self.archive([
                    (f"{archive_root}/chrome", stat.S_IFREG | 0o755, self.elf(machine)),
                ], name=f"wrong-root-{index}.zip")
                target = self.root / f"wrong-root-{index}"
                with self.assertRaisesRegex(prepare.InputError, "unsafe browser archive path"):
                    guest.extract_browser(source, target, architecture)
                self.assertFalse(target.exists())
        for index, (architecture, archive_root, machine) in enumerate([
            ("amd64", "chrome-linux64", 183),
            ("arm64", "chrome-linux-arm64", 62),
        ]):
            with self.subTest(architecture=architecture, kind="ELF"):
                source = self.archive([
                    (f"{archive_root}/chrome", stat.S_IFREG | 0o755, self.elf(machine)),
                ], name=f"wrong-elf-{index}.zip")
                with self.assertRaisesRegex(prepare.InputError, f"{architecture} ELF"):
                    guest.extract_browser(source, self.root / f"wrong-elf-{index}", architecture)

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
            ("chrome-linux64/chrome", stat.S_IFREG | 0o755, self.elf(62)),
            ("chrome-linux64/Chrome", stat.S_IFREG | 0o755, self.elf(62)),
        ])
        with self.assertRaisesRegex(prepare.InputError, "duplicate browser archive path"):
            guest.extract_browser(source, self.root / "out")

    def test_limits_browser_expansion_before_creating_output(self):
        source = self.archive([("chrome-linux64/chrome", stat.S_IFREG | 0o755, self.elf(62))])
        with patch.object(guest, "MAX_EXPANDED_BROWSER_BYTES", 3):
            with self.assertRaisesRegex(prepare.InputError, "browser expansion limit exceeded"):
                guest.extract_browser(source, self.root / "out")
        self.assertFalse((self.root / "out").exists())

    def test_browser_requires_executable_without_overwriting_existing_output(self):
        source = self.archive([("chrome-linux64/chrome", stat.S_IFREG | 0o644, self.elf(62))])
        with self.assertRaisesRegex(prepare.InputError, "browser executable is missing"):
            guest.extract_browser(source, self.root / "out")
        sentinel = self.root / "existing"
        sentinel.mkdir()
        (sentinel / "keep").write_bytes(b"preserve")
        with self.assertRaises(FileExistsError):
            guest.extract_browser(source, sentinel)
        self.assertEqual((sentinel / "keep").read_bytes(), b"preserve")

    def test_browser_installation_records_verified_policy_before_executing_chromium(self):
        records = {"synthetic": "inventory"}
        binary = Path("/opt/kelpie/browser/chrome-linux-arm64/chrome")
        events = []
        with patch.object(guest, "extract_browser", return_value=binary), \
                patch.object(browser_policy, "inventory", return_value=records), \
                patch.object(browser_policy, "install_policy",
                             side_effect=lambda arch: events.append("install")), \
                patch.object(browser_policy, "verify_policy",
                             side_effect=lambda arch: events.append("verify")), \
                patch.object(browser_policy, "verify_inventory") as verify_inventory, \
                patch.object(guest.prepare, "write_json") as write_json, \
                patch.object(guest, "write_file") as write_file, \
                patch.object(guest, "command", side_effect=lambda *args, **kwargs:
                             events.append("version") or "Chrome 1.2.3.4"):
            guest.install_browser({"file": "browser.zip", "version": "1.2.3.4"},
                                  self.root, "arm64")
        self.assertEqual(events, ["install", "verify", "version"])
        verify_inventory.assert_called_once_with("arm64", records)
        write_json.assert_called_once_with(guest.INSTALL_ROOT / "browser-inventory.json", records)
        write_file.assert_called_once_with(Path("/usr/local/bin/chromium"),
                                           f'#!/bin/sh\nexec {binary} "$@"\n', 0o755)

    def test_browser_policy_failure_prevents_browser_execution_or_entrypoint_creation(self):
        with patch.object(guest, "extract_browser"), \
                patch.object(browser_policy, "inventory", return_value={}), \
                patch.object(browser_policy, "install_policy",
                             side_effect=prepare.InputError("synthetic policy failure")), \
                patch.object(guest, "command") as command, \
                patch.object(guest, "write_file") as write_file, \
                patch.object(guest.prepare, "write_json") as write_json:
            with self.assertRaises(prepare.InputError):
                guest.install_browser({"file": "browser.zip", "version": "1.2.3.4"},
                                      self.root, "arm64")
        command.assert_not_called()
        write_file.assert_not_called()
        write_json.assert_not_called()

    def test_apparmor_must_be_explicitly_pinned_before_configuring_apt(self):
        self.assertIn("apparmor", guest.GUEST_PACKAGES)
        packages = dict.fromkeys(guest.GUEST_PACKAGES - {"apparmor"}, "1.0-fixture")
        with patch.object(guest, "write_file") as write_file, \
                patch.object(guest, "command") as command:
            with self.assertRaisesRegex(prepare.InputError, "required guest packages"):
                guest.configure_apt("20260909T000000Z", packages, "arm64")
        write_file.assert_not_called()
        command.assert_not_called()

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

    def test_installs_complete_codex_package_without_executing_it(self):
        case = test_codex_package.CodexPackageTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        source = case.archive()
        item = build.measure(source) | {"version": case.version}
        entrypoint = self.root / "codex-entry"
        with patch.object(guest, "command") as command:
            guest.install_codex(item, source.parent, self.root, entrypoint)
        command.assert_not_called()
        self.assertEqual(entrypoint.readlink(),
                         self.root / "codex" / codex_package.PREFIX / "bin/codex")
        records = build.read_json(self.root / "codex-inventory.json")
        codex_package.verify_installed(self.root / "codex", case.version, records)
        self.assertEqual(set(records), set(case.files))
        with self.assertRaisesRegex(prepare.InputError, "entrypoint already exists"):
            guest.install_codex(item, source.parent, self.root, entrypoint)

    def test_incomplete_codex_package_never_creates_entrypoint_or_inventory(self):
        case = test_codex_package.CodexPackageTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        source = case.archive({codex_package.PREFIX + "codex-resources/bwrap": None})
        with self.assertRaises(prepare.InputError):
            guest.install_codex(build.measure(source) | {"version": case.version},
                                source.parent, self.root, self.root / "codex-entry")
        self.assertFalse(os.path.lexists(self.root / "codex-entry"))
        self.assertFalse((self.root / "codex-inventory.json").exists())
        self.assertFalse((self.root / "codex").exists())

    def test_threads_arm64_through_complete_codex_package_installation(self):
        source = self.root / "codex-arm64.tgz"
        source.write_bytes(b"synthetic archive, parsing is mocked by the package contract")
        item = build.measure(source) | {"version": "0.154.0"}
        destination = self.root / "codex"
        entrypoint = self.root / "codex-entry"
        records = {"synthetic": {"size_bytes": 1, "sha256": "0" * 64, "mode": 0o644}}
        with patch.object(codex_package, "extract", return_value=records) as extract, \
                patch.object(codex_package, "verify_installed") as verify:
            guest.install_codex(item, self.root, self.root, entrypoint, "arm64")
        extract.assert_called_once_with(source, destination, "0.154.0", "arm64")
        verify.assert_called_once_with(destination, "0.154.0", records, "arm64")
        self.assertEqual(entrypoint.readlink(),
                         destination / codex_package.runtime_prefix("arm64") / "bin/codex")

    def test_legacy_codex_elf_remains_supported_with_digest_and_architecture_checks(self):
        source = self.root / "codex-standalone"
        entrypoint = self.root / "codex-entry"
        source.write_bytes(self.elf(62))
        item = build.measure(source) | {"version": "0.0-fixture"}
        guest.install_codex(item, self.root, self.root, entrypoint)
        self.assertFalse(entrypoint.is_symlink())
        self.assertEqual(entrypoint.read_bytes(), source.read_bytes())
        self.assertEqual(stat.S_IMODE(entrypoint.stat().st_mode), 0o755)
        self.assertFalse((self.root / "codex-inventory.json").exists())
        entrypoint.unlink()
        with self.assertRaises(prepare.InputError):
            guest.install_codex(item | {"sha256": "0" * 64}, self.root, self.root, entrypoint)
        entrypoint.unlink(missing_ok=True)  # Only this test's failed copy, never reused.
        source.write_bytes(b"not an amd64 ELF")
        with self.assertRaisesRegex(prepare.InputError, "amd64 ELF"):
            guest.install_codex(build.measure(source), self.root, self.root, entrypoint)
        self.assertFalse(entrypoint.exists())

    def test_arm64_codex_elf_is_supported_and_cross_architecture_elf_is_rejected(self):
        source = self.root / "codex-standalone"
        entrypoint = self.root / "codex-entry"
        source.write_bytes(self.elf(183))
        guest.install_codex(build.measure(source), self.root, self.root, entrypoint, "arm64")
        self.assertEqual(entrypoint.read_bytes(), self.elf(183))
        entrypoint.unlink()
        for architecture, machine in (("amd64", 183), ("arm64", 62)):
            with self.subTest(architecture=architecture):
                source.write_bytes(self.elf(machine))
                with self.assertRaisesRegex(prepare.InputError, f"{architecture} ELF"):
                    guest.install_codex(build.measure(source), self.root, self.root, entrypoint,
                                        architecture)
                self.assertFalse(entrypoint.exists())

    def test_configures_architecture_specific_signed_snapshot_sources(self):
        snapshot = "20260901T000000Z"
        packages = dict.fromkeys(guest.GUEST_PACKAGES, "1.0-fixture")

        def fake_command(*args, **_kwargs):
            if args[0] == "dpkg-query":
                return "\n".join(f"{name}\t{version}" for name, version in packages.items())
            return ""

        for architecture, uri, snapshot_field in (
            ("amd64", "https://archive.ubuntu.com/ubuntu/", True),
            ("arm64", f"https://snapshot.ubuntu.com/ubuntu/{snapshot}/", False),
        ):
            sources = self.root / f"sources-{architecture}"
            sources.mkdir()
            legacy = self.root / f"legacy-{architecture}.list"

            def fixed_path(value, sources=sources, legacy=legacy):
                return {
                    "/etc/apt/sources.list.d": sources,
                    "/etc/apt/sources.list": legacy,
                }.get(value, Path(value))

            with self.subTest(architecture=architecture), \
                    patch.object(guest, "Path", side_effect=fixed_path), \
                    patch.object(guest, "command", side_effect=fake_command) as command, \
                    patch.object(guest.prepare, "write_json") as write_json:
                if architecture == "amd64":
                    guest.configure_apt(snapshot, packages)
                else:
                    guest.configure_apt(snapshot, packages, architecture)
            content = (sources / "ubuntu.sources").read_text()
            self.assertIn(f"URIs: {uri}\n", content)
            self.assertEqual(f"Snapshot: {snapshot}\n" in content, snapshot_field)
            self.assertIn("Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n", content)
            self.assertEqual(command.call_args_list[0].args, ("apt-get", "update"))
            self.assertEqual(command.call_args_list[1].args[:4],
                             ("apt-get", "install", "-y", "--no-install-recommends"))
            write_json.assert_called_once()

    def test_rejects_unsupported_architecture_before_installation_helpers(self):
        with patch.object(guest, "write_file") as write_file, \
                patch.object(guest, "command") as command:
            with self.assertRaisesRegex(prepare.InputError, "unsupported guest architecture"):
                guest.configure_apt("20260901T000000Z", {}, "riscv64")
        write_file.assert_not_called()
        command.assert_not_called()

    def test_invalid_snapshot_is_rejected_before_filesystem_access_or_apt(self):
        sources = self.root / "snapshot-sources"
        sources.mkdir()
        packages = dict.fromkeys(guest.GUEST_PACKAGES, "1.0-fixture")
        inventory = "\n".join(f"{name}\t{version}" for name, version in packages.items())
        for architecture in ("amd64", "arm64"):
            for snapshot in (None, [], "latest", "../20260909T000000Z",
                             "20260909T000000Z\nTrusted: yes", "20260230T000000Z",
                             "20260909T250000Z", "20260909T000000Z/"):
                with self.subTest(architecture=architecture, snapshot=snapshot), \
                        patch.object(guest, "Path", return_value=sources) as path, \
                        patch.object(guest, "write_file") as write_file, \
                        patch.object(guest, "command", return_value=inventory) as command, \
                        patch.object(guest.prepare, "write_json"):
                    # Treat the legacy source as absent, without touching system paths.
                    with patch.object(Path, "exists", return_value=False):
                        with self.assertRaisesRegex(prepare.InputError, "snapshot"):
                            guest.configure_apt(snapshot, packages, architecture)
                    path.assert_not_called()
                    write_file.assert_not_called()
                    command.assert_not_called()

    def test_guest_guard_accepts_only_native_x86_64_and_aarch64(self):
        release = {"ID": "ubuntu", "VERSION_ID": "24.04"}
        for machine in ("x86_64", "aarch64"):
            with self.subTest(machine=machine), \
                    patch.object(guest.platform, "system", return_value="Linux"), \
                    patch.object(guest.os, "geteuid", return_value=0), \
                    patch.object(Path, "read_text", return_value=guest.BUILD_PRODUCT), \
                    patch.object(guest.platform, "freedesktop_os_release", return_value=release), \
                    patch.object(guest.platform, "machine", return_value=machine), \
                    patch.object(guest, "command", return_value="kvm") as command:
                self.assertIsNone(guest.guard_guest())
                command.return_value = "qemu"
                with self.assertRaisesRegex(prepare.InputError, "requires a KVM guest"):
                    guest.guard_guest()
        for machine in ("arm64", "riscv64", "AMD64", ""):
            with self.subTest(machine=machine), \
                    patch.object(guest.platform, "system", return_value="Linux"), \
                    patch.object(guest.os, "geteuid", return_value=0), \
                    patch.object(Path, "read_text", return_value=guest.BUILD_PRODUCT), \
                    patch.object(guest.platform, "freedesktop_os_release", return_value=release), \
                    patch.object(guest.platform, "machine", return_value=machine), \
                    patch.object(guest, "command") as command:
                with self.assertRaisesRegex(prepare.InputError, "amd64 or arm64 guest"):
                    guest.guard_guest()
                command.assert_not_called()

    def test_install_rejects_cross_architecture_manifest_before_destinations_change(self):
        installation = self.root / "installation"
        with patch.object(guest, "INSTALL_ROOT", installation), \
                patch.object(guest, "STAGING", self.root / "staging"), \
                patch.object(guest, "guard_guest"), \
                patch.object(guest.platform, "machine", return_value="aarch64"), \
                patch.object(guest, "command") as command, \
                patch.object(guest, "reject_credentials"), \
                patch.object(guest.prepare, "read_manifest",
                             return_value={"architecture": "amd64"}), \
                patch.object(guest.os.path, "lexists") as lexists:
            with self.assertRaisesRegex(prepare.InputError,
                                        "manifest architecture differs from native guest"):
                guest.install()
        self.assertFalse(installation.exists())
        lexists.assert_not_called()
        command.assert_called_once_with("cloud-init", "status", "--wait", timeout=600)

    def test_installs_browser_probe_with_all_standalone_health_dependencies(self):
        installation = self.root / "installation"
        installation.mkdir()
        staging = self.root / "staging"
        tooling = staging / "tooling"
        tooling.mkdir(parents=True)
        names = {"health.py", "prepare.py", "guest.py", "codex_package.py", "browser_probe.py",
                 "browser_policy.py"}
        for name in names:
            (tooling / name).write_text(f"# synthetic {name}\n")
        with patch.object(guest, "INSTALL_ROOT", installation), \
                patch.object(guest, "STAGING", staging):
            guest.install_health_tools()
            with self.assertRaises(FileExistsError):
                guest.install_health_tools()
        installed = installation / "image-health"
        self.assertEqual({path.name for path in installed.iterdir()}, names)
        for name in names:
            self.assertEqual((installed / name).read_text(), f"# synthetic {name}\n")
            self.assertEqual(stat.S_IMODE((installed / name).stat().st_mode), 0o644)

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
