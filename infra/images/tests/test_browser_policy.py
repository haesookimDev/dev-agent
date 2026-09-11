import copy
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from infra.images import browser_policy, prepare


class BrowserPolicyTests(unittest.TestCase):
    MACHINES = {"amd64": 62, "arm64": 183}
    DIRECTORIES = {"amd64": "chrome-linux64", "arm64": "chrome-linux-arm64"}

    @staticmethod
    def elf(machine):
        header = bytearray(64)
        header[:7] = b"\x7fELF\x02\x01\x01"
        header[18:20] = machine.to_bytes(2, "little")
        return bytes(header) + b"synthetic browser"

    @contextmanager
    def fixture(self, architecture="amd64", *, runtime=False):
        with tempfile.TemporaryDirectory(prefix="kelpie-browser-policy-") as temporary:
            root = Path(temporary)
            root.chmod(0o700)
            browser_root = root / "opt/kelpie/browser"
            browser_root.mkdir(parents=True, mode=0o755)
            package = browser_root / self.DIRECTORIES[architecture]
            package.mkdir(mode=0o755)
            chrome = package / "chrome"
            chrome.write_bytes(self.elf(self.MACHINES[architecture]))
            chrome.chmod(0o755)
            (package / "resources.pak").write_bytes(b"resource bytes")
            (package / "resources.pak").chmod(0o644)
            if runtime:
                self.make_runtime(root)
            # mkdir(parents=True) leaves intermediate modes to the caller's
            # umask (002 in the Linux lab). The baseline models immutable paths.
            for path in root.rglob("*"):
                if path.is_dir():
                    path.chmod(0o755)
            with patch.object(browser_policy, "ROOT", root), \
                    patch.object(browser_policy, "ROOT_UID", os.getuid()), \
                    patch.object(browser_policy, "ROOT_GID", os.getgid()):
                yield root

    @staticmethod
    def put(root, relative, value, mode=0o644):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        path.write_text(value)
        path.chmod(mode)
        return path

    def make_runtime(self, root):
        self.put(root, "sys/module/apparmor/parameters/enabled", "Y\n")
        self.put(root, "proc/sys/kernel/apparmor_restrict_unprivileged_userns", "1\n")
        self.put(root, "sys/kernel/security/apparmor/profiles", "")
        (root / "etc/apparmor.d/disable").mkdir(parents=True, mode=0o755)
        (root / "etc/apparmor.d/force-complain").mkdir(mode=0o755)

    def expected_policy(self, architecture):
        native = f"/opt/kelpie/browser/{self.DIRECTORIES[architecture]}/chrome"
        return (
            "abi <abi/4.0>,\n"
            f"profile kelpie-image-browser {native} flags=(unconfined) {{\n"
            "  userns,\n"
            "}\n"
        )

    def write_policy(self, root, architecture="amd64"):
        return self.put(
            root,
            "etc/apparmor.d/kelpie-image-browser",
            self.expected_policy(architecture),
        )

    def fake_commands(
        self,
        root,
        *,
        service_returncode=0,
        parser_returncode=0,
        loaded_mode="unconfined",
    ):
        calls = []

        def run(args, **kwargs):
            calls.append(tuple(args))
            self.assertEqual(kwargs["timeout"], 10)
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(
                kwargs["env"],
                {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            )
            if args[0] == "/usr/bin/systemctl":
                self.assertEqual(
                    args,
                    ["/usr/bin/systemctl", "is-active", "--quiet", "apparmor.service"],
                )
                return subprocess.CompletedProcess(args, service_returncode)
            self.assertEqual(
                args,
                [
                    "/usr/sbin/apparmor_parser",
                    "--add",
                    str(root / "etc/apparmor.d/kelpie-image-browser"),
                ],
            )
            if parser_returncode == 0 and loaded_mode is not None:
                self.put(
                    root,
                    "sys/kernel/security/apparmor/profiles",
                    f"kelpie-image-browser ({loaded_mode})\n",
                )
            return subprocess.CompletedProcess(args, parser_returncode)

        return calls, run

    def test_inventory_supports_both_native_architectures_and_rejects_cross_architecture(self):
        for architecture in ("amd64", "arm64"):
            with self.subTest(architecture=architecture), self.fixture(architecture) as root:
                records = browser_policy.inventory(architecture)
                executable = f"{self.DIRECTORIES[architecture]}/chrome"
                self.assertEqual(
                    set(records),
                    {"schema_version", "architecture", "browser_path", "entries"},
                )
                self.assertEqual(records["schema_version"], 1)
                self.assertEqual(records["architecture"], architecture)
                self.assertEqual(
                    records["browser_path"], f"/opt/kelpie/browser/{executable}"
                )
                self.assertEqual(records["entries"][executable]["kind"], "file")
                self.assertEqual(
                    set(records["entries"][executable]),
                    {"kind", "sha256", "size_bytes", "mode", "uid", "gid"},
                )
                browser_policy.verify_inventory(architecture, records)
                with self.assertRaisesRegex(prepare.InputError, "browser executable"):
                    browser_policy.inventory("arm64" if architecture == "amd64" else "amd64")
                self.assertTrue((root / "opt/kelpie/browser" / executable).exists())

    def test_inventory_detects_package_byte_mode_owner_add_and_remove_mutations(self):
        for mutation in ("bytes", "mode", "owner", "add", "remove"):
            with self.subTest(mutation=mutation), self.fixture() as root:
                records = browser_policy.inventory("amd64")
                package = root / "opt/kelpie/browser/chrome-linux64"
                resource = package / "resources.pak"
                fstat = None
                if mutation == "bytes":
                    resource.write_bytes(b"changed bytes!")
                elif mutation == "mode":
                    resource.chmod(0o666)
                elif mutation == "add":
                    (package / "extra").write_bytes(b"extra")
                elif mutation == "remove":
                    resource.unlink()
                else:
                    inode = resource.stat().st_ino
                    real_fstat = os.fstat

                    def changed_owner(fd, real_fstat=real_fstat, inode=inode):
                        info = real_fstat(fd)
                        if info.st_ino == inode:
                            values = list(info)
                            values[4] = os.getuid() + 1
                            return os.stat_result(values)
                        return info

                    fstat = patch.object(browser_policy.os, "fstat", side_effect=changed_owner)
                context = fstat if fstat is not None else patch.object(
                    browser_policy, "MAX_ENTRIES", browser_policy.MAX_ENTRIES
                )
                with context, self.assertRaises(prepare.InputError):
                    browser_policy.verify_inventory("amd64", records)

    def test_inventory_rejects_invalid_schema_and_bool_integer_aliases(self):
        with self.fixture() as _root:
            records = browser_policy.inventory("amd64")
            executable = "chrome-linux64/chrome"
            mutations = []
            extra = copy.deepcopy(records)
            extra["extra"] = True
            mutations.append(extra)
            boolean = copy.deepcopy(records)
            boolean["entries"][executable]["size_bytes"] = True
            mutations.append(boolean)
            missing = copy.deepcopy(records)
            del missing["entries"][executable]
            mutations.append(missing)
            wrong_arch = copy.deepcopy(records)
            wrong_arch["architecture"] = "arm64"
            mutations.append(wrong_arch)
            for altered in mutations:
                with self.subTest(keys=altered.keys()), self.assertRaises(prepare.InputError):
                    browser_policy.verify_inventory("amd64", altered)

    def test_inventory_rejects_symlink_hardlink_and_special_entries(self):
        for kind in ("symlink", "hardlink", "fifo"):
            with self.subTest(kind=kind), self.fixture() as root:
                package = root / "opt/kelpie/browser/chrome-linux64"
                target = package / "resources.pak"
                extra = package / "unsafe"
                if kind == "symlink":
                    extra.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, extra)
                else:
                    os.mkfifo(extra)
                with self.assertRaises(prepare.InputError):
                    browser_policy.inventory("amd64")

    def test_inventory_rejects_writable_ancestor_and_bounds(self):
        with self.fixture() as root:
            (root / "opt").chmod(0o777)
            with self.assertRaisesRegex(prepare.InputError, "browser directory"):
                browser_policy.inventory("amd64")
        for constant, value in (("MAX_ENTRIES", 2), ("MAX_BYTES", 10)):
            with self.subTest(constant=constant), self.fixture() as _root, \
                    patch.object(browser_policy, constant, value), \
                    self.assertRaises(prepare.InputError):
                browser_policy.inventory("amd64")

    def test_install_and_verify_exact_policy_for_both_architectures(self):
        for architecture in ("amd64", "arm64"):
            with self.subTest(architecture=architecture), \
                    self.fixture(architecture, runtime=True) as root:
                calls, fake = self.fake_commands(root)
                with patch.object(browser_policy.subprocess, "run", side_effect=fake):
                    browser_policy.install_policy(architecture)
                    browser_policy.verify_policy(architecture)
                policy_path = root / "etc/apparmor.d/kelpie-image-browser"
                self.assertEqual(policy_path.read_text(), self.expected_policy(architecture))
                self.assertEqual(stat.S_IMODE(policy_path.stat().st_mode), 0o644)
                self.assertEqual(sum(call[0] == "/usr/sbin/apparmor_parser" for call in calls), 1)

    def test_existing_policy_and_loaded_name_collision_precede_parser(self):
        for collision in ("file", "loaded"):
            with self.subTest(collision=collision), self.fixture(runtime=True) as root:
                if collision == "file":
                    self.write_policy(root)
                else:
                    self.put(
                        root,
                        "sys/kernel/security/apparmor/profiles",
                        "kelpie-image-browser (enforce)\n",
                    )
                calls, fake = self.fake_commands(root)
                with patch.object(browser_policy.subprocess, "run", side_effect=fake), \
                        self.assertRaises(prepare.InputError):
                    browser_policy.install_policy("amd64")
                self.assertFalse(any(call[0] == "/usr/sbin/apparmor_parser" for call in calls))

    def test_install_rejects_corrupt_browser_before_parser(self):
        with self.fixture(runtime=True) as root:
            (root / "opt/kelpie/browser/chrome-linux64/chrome").write_bytes(b"not ELF")
            calls, fake = self.fake_commands(root)
            with patch.object(browser_policy.subprocess, "run", side_effect=fake), \
                    self.assertRaises(prepare.InputError):
                browser_policy.install_policy("amd64")
            self.assertEqual(calls, [])

    def test_policy_rejects_unsafe_content_mode_link_and_override(self):
        for mutation in ("content", "mode", "hardlink", "symlink", "override"):
            with self.subTest(mutation=mutation), self.fixture(runtime=True) as root:
                policy_path = self.write_policy(root)
                if mutation == "content":
                    policy_path.write_text(self.expected_policy("amd64") + "# extra\n")
                elif mutation == "mode":
                    policy_path.chmod(0o666)
                elif mutation == "hardlink":
                    os.link(policy_path, root / "policy-hardlink")
                elif mutation == "symlink":
                    policy_path.unlink()
                    policy_path.symlink_to(root / "outside")
                else:
                    override = root / "etc/apparmor.d/disable/kelpie-image-browser"
                    override.symlink_to("../kelpie-image-browser")
                self.put(
                    root,
                    "sys/kernel/security/apparmor/profiles",
                    "kelpie-image-browser (unconfined)\n",
                )
                _calls, fake = self.fake_commands(root)
                with patch.object(browser_policy.subprocess, "run", side_effect=fake), \
                        self.assertRaises(prepare.InputError):
                    browser_policy.verify_policy("amd64")

    def test_install_rejects_dangling_disable_and_force_complain_links(self):
        for directory in ("disable", "force-complain"):
            with self.subTest(directory=directory), self.fixture(runtime=True) as root:
                (root / f"etc/apparmor.d/{directory}/kelpie-image-browser").symlink_to(
                    "/missing"
                )
                calls, fake = self.fake_commands(root)
                with patch.object(browser_policy.subprocess, "run", side_effect=fake), \
                        self.assertRaises(prepare.InputError):
                    browser_policy.install_policy("amd64")
                self.assertFalse(any(call[0] == "/usr/sbin/apparmor_parser" for call in calls))

    def test_verify_rejects_disabled_or_malformed_runtime_and_loaded_mode(self):
        variants = (
            ("module-disabled", "sys/module/apparmor/parameters/enabled", "N\n", 0),
            ("module-malformed", "sys/module/apparmor/parameters/enabled", "maybe\n", 0),
            (
                "sysctl-disabled",
                "proc/sys/kernel/apparmor_restrict_unprivileged_userns",
                "0\n",
                0,
            ),
            (
                "sysctl-malformed",
                "proc/sys/kernel/apparmor_restrict_unprivileged_userns",
                "1 0\n",
                0,
            ),
            (
                "loaded-mode",
                "sys/kernel/security/apparmor/profiles",
                "kelpie-image-browser (enforce)\n",
                0,
            ),
            ("service", None, None, 1),
        )
        for name, relative, value, service_returncode in variants:
            with self.subTest(name=name), self.fixture(runtime=True) as root:
                self.write_policy(root)
                self.put(
                    root,
                    "sys/kernel/security/apparmor/profiles",
                    "kelpie-image-browser (unconfined)\n",
                )
                if relative is not None:
                    self.put(root, relative, value)
                _calls, fake = self.fake_commands(
                    root, service_returncode=service_returncode
                )
                with patch.object(browser_policy.subprocess, "run", side_effect=fake), \
                        self.assertRaises(prepare.InputError):
                    browser_policy.verify_policy("amd64")

    def test_parser_failure_never_reports_policy_success(self):
        with self.fixture(runtime=True) as root:
            calls, fake = self.fake_commands(root, parser_returncode=1)
            with patch.object(browser_policy.subprocess, "run", side_effect=fake), \
                    self.assertRaisesRegex(prepare.InputError, "parser"):
                browser_policy.install_policy("amd64")
            self.assertTrue(any(call[0] == "/usr/sbin/apparmor_parser" for call in calls))
            self.assertEqual(
                (root / "sys/kernel/security/apparmor/profiles").read_text(), ""
            )

    def test_parser_must_load_unconfined_profile(self):
        with self.fixture(runtime=True) as root:
            _calls, fake = self.fake_commands(root, loaded_mode="enforce")
            with patch.object(browser_policy.subprocess, "run", side_effect=fake), \
                    self.assertRaisesRegex(prepare.InputError, "loaded"):
                browser_policy.install_policy("amd64")

    def test_unknown_architecture_is_rejected_without_filesystem_or_commands(self):
        with patch.object(browser_policy.subprocess, "run") as run:
            for function, args in (
                (browser_policy.inventory, ("ppc64le",)),
                (browser_policy.verify_inventory, ("ppc64le", {})),
                (browser_policy.install_policy, ("ppc64le",)),
                (browser_policy.verify_policy, ("ppc64le",)),
            ):
                with self.subTest(function=function.__name__), self.assertRaises(
                    prepare.InputError
                ):
                    function(*args)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
