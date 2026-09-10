import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infra.images import health, prepare


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kelpie-health-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = {
            "image_version": "synthetic.1", "apt_packages": {"synthetic-package": "1.0"},
            "runner_wheels": [{"name": "kelpie-vm-runner", "version": "1.0"}],
            "codex": {"version": "0.0-fixture"}, "browser": {"version": "0.0-fixture"},
        }
        self.put("opt/kelpie/image-manifest.json", json.dumps(self.manifest))
        self.digest = hashlib.sha256(
            (self.root / "opt/kelpie/image-manifest.json").read_bytes()).hexdigest()
        self.seal = {"schema_version": 1, "status": "sealed_candidate", "release_eligible": False,
                     "image_version": "synthetic.1", "manifest_sha256": self.digest}
        self.put("opt/kelpie/candidate.json", json.dumps(self.seal))
        self.put("etc/machine-id", "a" * 32)
        self.put("proc/sys/kernel/random/boot_id", "11111111-1111-4111-8111-111111111111")
        self.root_patch = patch.object(health, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def put(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)

    def test_sealed_identity_and_new_machine_identity_are_hashed(self):
        result = health.identities(self.manifest)
        self.assertEqual(result["manifest_sha256"], self.digest)
        self.assertEqual(result["machine_id_sha256"], hashlib.sha256(b"a" * 32).hexdigest())
        self.assertEqual(len(result["boot_id_sha256"]), 64)

    def test_rejects_unsealed_or_mismatched_candidates(self):
        for change in ({"release_eligible": True}, {"schema_version": True},
                       {"status": "installing"}, {"manifest_sha256": "b" * 64},
                       {"extra": True}):
            with self.subTest(change=change):
                self.put("opt/kelpie/candidate.json", json.dumps(self.seal | change))
                with self.assertRaises(prepare.InputError):
                    health.identities(self.manifest)

    def test_rejects_uninitialized_machine_identity(self):
        for machine in ("uninitialized", "0" * 32, "", "synthetic-sensitive-value"):
            with self.subTest(machine=machine):
                self.put("etc/machine-id", machine)
                with self.assertRaisesRegex(prepare.InputError, "machine identity"):
                    health.identities(self.manifest)

    def test_remaining_builder_access_including_dangling_links_is_rejected(self):
        path = self.root / "etc/sudoers.d/kelpie-image-builder"
        path.parent.mkdir()
        path.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(prepare.InputError, "access or assignment remains"):
            health.access_removed()

    def test_checks_locked_builder_without_reading_shadow_credentials(self):
        with patch.object(health.pwd, "getpwnam",
                          return_value=SimpleNamespace(pw_shell="/usr/sbin/nologin")), \
                patch.object(health, "command", return_value="kelpie-builder L") as command, \
                patch.object(health.guest, "reject_credentials") as caches:
            health.access_removed()
        command.assert_called_once_with("passwd", "--status", "kelpie-builder")
        caches.assert_called_once()

    def test_installation_must_match_inventory_lock_and_binary_versions(self):
        packages = self.manifest["apt_packages"]
        wheels = {"kelpie-vm-runner": "1.0"}
        self.put("opt/kelpie/apt-inventory.json", json.dumps(packages))
        self.put("opt/kelpie/python-inventory.json", json.dumps(wheels))

        def command(*args, **kwargs):
            if args[0] == "dpkg-query":
                return "synthetic-package\t1.0"
            if args[1] == "-c":
                self.assertIn("import kelpie_runner.main", args[2])
                return json.dumps(wheels)
            if args[-1] == "--version":
                return ("codex-cli 0.0-fixture" if args[-2].endswith("codex")
                        else "Chrome 0.0-fixture")
            return ""

        with patch.object(health, "command", side_effect=command):
            health.installed(self.manifest)
            self.manifest["apt_packages"]["synthetic-package"] = "2.0"
            with self.assertRaisesRegex(prepare.InputError, "APT versions"):
                health.installed(self.manifest)
            self.manifest["apt_packages"]["synthetic-package"] = "1.0"
            self.manifest["runner_wheels"][0]["version"] = "2.0"
            with self.assertRaisesRegex(prepare.InputError, "wheel versions"):
                health.installed(self.manifest)

    def test_browser_uses_disposable_unprivileged_profile_and_keeps_sandbox(self):
        paths = []

        def command(*args, **kwargs):
            self.assertEqual(args[:5], ("runuser", "-u", "kelpie", "--", "/usr/local/bin/chromium"))
            self.assertNotIn("--no-sandbox", args)
            self.assertTrue(args[-1].startswith("data:text/html,"))
            directory = Path(next(arg.split("=", 1)[1] for arg in args
                                  if arg.startswith("--user-data-dir=")))
            self.assertTrue(directory.is_dir())
            paths.append(directory)
            return '<p id="probe">4</p>'

        with patch.object(health.pwd, "getpwnam",
                          return_value=SimpleNamespace(pw_uid=1234, pw_gid=1234)), \
                patch.object(health.os, "chown") as chown, \
                patch.object(health, "command", side_effect=command):
            health.browser()
        self.assertEqual(chown.call_args.args[1:], (1234, 1234))
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_browser_rejects_unexecuted_javascript(self):
        with patch.object(health.pwd, "getpwnam",
                          return_value=SimpleNamespace(pw_uid=1234, pw_gid=1234)), \
                patch.object(health.os, "chown"), \
                patch.object(health, "command", return_value='<p id="probe">pending</p>'):
            with self.assertRaisesRegex(prepare.InputError, "DOM smoke failed"):
                health.browser()

    def test_desktop_requires_active_session_and_idle_runner(self):
        def command(*args, **kwargs):
            if args[1] == "is-active" or args[0] == "loginctl":
                return "active"
            if args[0] == "pgrep":
                return "1234"
            if args[0] == "runuser":
                return "synthetic X display metadata"
            if "ActiveState" in args:
                return "inactive"
            return "disabled" if "kelpie-runner.service" in args else "masked"

        with patch.object(health.Path, "lstat",
                          return_value=SimpleNamespace(st_mode=stat.S_IFSOCK)), \
                patch.object(health, "command", side_effect=command):
            health.desktop()
        with patch.object(health, "command", return_value="inactive"):
            with self.assertRaisesRegex(prepare.InputError, "not active"):
                health.desktop()

    def test_probe_runs_every_check_and_never_claims_release(self):
        with ExitStack() as stack:
            stack.enter_context(patch.object(health, "guard"))
            stack.enter_context(patch.object(health, "command"))
            stack.enter_context(patch.object(health.prepare, "read_manifest",
                                             return_value=self.manifest))
            checks = [stack.enter_context(patch.object(health, name)) for name in
                      ("access_removed", "installed", "desktop", "browser")]
            result = health.probe()
        self.assertIs(result["release_eligible"], False)
        self.assertEqual(result["checks"], dict.fromkeys(health.CHECKS, True))
        for check in checks:
            self.assertEqual(check.call_count, 1)

    def test_real_cli_refuses_this_host_without_running_browser(self):
        self.assertNotEqual(os.geteuid(), 0, "run image tests as non-root")
        result = subprocess.run([sys.executable, str(Path(health.__file__))], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "image smoke rejected\n")

    def test_failure_does_not_publish_partial_success_or_raw_diagnostics(self):
        for failure in (prepare.InputError("synthetic-sensitive-value"),
                        subprocess.CalledProcessError(1, "synthetic-sensitive-command")):
            out, error = io.StringIO(), io.StringIO()
            with patch.object(health, "probe", side_effect=failure), \
                    redirect_stdout(out), redirect_stderr(error):
                self.assertEqual(health.main(), 1)
            self.assertEqual(out.getvalue(), "")
            self.assertEqual(error.getvalue(), "image smoke rejected\n")


if __name__ == "__main__":
    unittest.main()
