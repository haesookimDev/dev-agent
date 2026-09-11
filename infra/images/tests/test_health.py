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

from infra.images import codex_package, health, prepare
from infra.images.tests import test_codex_package


class HealthTests(unittest.TestCase):
    def setUp(self):
        machine = patch.object(health.platform, "machine", return_value="x86_64")
        machine.start()
        self.addCleanup(machine.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix="kelpie-health-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manifest = {
            "architecture": "amd64",
            "image_version": "synthetic.1", "apt_packages": {"synthetic-package": "1.0"},
            "runner_wheels": [{"name": "kelpie-vm-runner", "version": "1.0"}],
            "codex": {"file": "codex", "version": "0.0-fixture"},
            "browser": {"version": "0.0-fixture"},
        }
        self.put("opt/kelpie/image-manifest.json", json.dumps(self.manifest))
        self.digest = hashlib.sha256(
            (self.root / "opt/kelpie/image-manifest.json").read_bytes()).hexdigest()
        self.seal = {"schema_version": 1, "status": "sealed_candidate", "release_eligible": False,
                     "image_version": "synthetic.1", "manifest_sha256": self.digest}
        self.put("opt/kelpie/candidate.json", json.dumps(self.seal))
        self.put("etc/machine-id", "a" * 32)
        self.put("proc/sys/kernel/random/boot_id", "11111111-1111-4111-8111-111111111111")
        self.put("sys/fs/cgroup/cgroup.controllers", "cpu memory pids\n")
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

    def test_boot_checks_complete_codex_package_and_canonical_entrypoint(self):
        case = test_codex_package.CodexPackageTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        destination = self.root / "opt/kelpie/codex"
        records = codex_package.extract(case.archive(), destination, case.version)
        self.put("opt/kelpie/codex-inventory.json", json.dumps(records))
        entrypoint = self.root / "usr/local/bin/codex"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.symlink_to(f"/opt/kelpie/codex/{codex_package.PREFIX}bin/codex")
        self.manifest["codex"] = {"file": "codex.tgz", "version": case.version}
        self.put("opt/kelpie/apt-inventory.json", json.dumps(self.manifest["apt_packages"]))
        self.put("opt/kelpie/python-inventory.json", '{"kelpie-vm-runner":"1.0"}')

        def command(*args, **kwargs):
            if args[0] == "dpkg-query":
                return "synthetic-package\t1.0"
            if args[1] == "-c":
                return '{"kelpie-vm-runner":"1.0"}'
            if args[-1] == "--version":
                return (f"codex-cli {case.version}" if args[-2].endswith("codex")
                        else "Chrome 0.0-fixture")
            return ""

        with patch.object(health, "command", side_effect=command):
            health.installed(self.manifest)
            entrypoint.unlink()
            entrypoint.symlink_to("/different/codex")
            with self.assertRaisesRegex(prepare.InputError, "entrypoint differs"):
                health.installed(self.manifest)
            entrypoint.unlink()
            entrypoint.symlink_to(f"/opt/kelpie/codex/{codex_package.PREFIX}bin/codex")
            helper = destination / codex_package.PREFIX / "bin/codex-code-mode-host"
            helper.write_bytes(b"x" * helper.stat().st_size)
            with self.assertRaisesRegex(prepare.InputError, "installed file changed"):
                health.installed(self.manifest)

    def test_browser_uses_disposable_unprivileged_profile_and_keeps_sandbox(self):
        paths = []

        def command(*args, **kwargs):
            self.assertEqual(args[1:3], ("/usr/bin/python3",
                                        "/opt/kelpie/image-health/browser_probe.py"))
            self.assertEqual(args[4], "1.2.3.4")
            directory = Path(args[3])
            self.assertTrue(directory.is_dir())
            paths.append(directory)
            return json.dumps({"schema_version": 1, "status": "browser_dom_passed",
                               "browser_version": "1.2.3.4"})

        with patch.object(health.pwd, "getpwnam",
                          return_value=SimpleNamespace(pw_uid=1234, pw_gid=1234)), \
                patch.object(health.os, "chown") as chown, \
                patch.object(health, "smoke_service", side_effect=command):
            health.browser("1.2.3.4")
        self.assertEqual(chown.call_args.args[1:], (1234, 1234))
        self.assertTrue(paths)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_arm_installed_package_checks_architecture_specific_inventory_and_link(self):
        case = test_codex_package.CodexPackageTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        destination = self.root / "opt/kelpie/codex"
        records = codex_package.extract(case.archive(architecture="arm64"), destination,
                                        case.version, architecture="arm64")
        self.put("opt/kelpie/codex-inventory.json", json.dumps(records))
        self.put("opt/kelpie/apt-inventory.json", json.dumps(self.manifest["apt_packages"]))
        self.put("opt/kelpie/python-inventory.json", '{"kelpie-vm-runner":"1.0"}')
        entrypoint = self.root / "usr/local/bin/codex"
        entrypoint.parent.mkdir(parents=True)
        entrypoint.symlink_to("/opt/kelpie/codex/vendor/aarch64-unknown-linux-musl/bin/codex")
        self.manifest.update(architecture="arm64", codex={"file": "codex.tgz",
                                                        "version": case.version})

        def command(*args, **kwargs):
            if args[0] == "dpkg-query":
                return "synthetic-package\t1.0"
            if args[1] == "-c":
                return '{"kelpie-vm-runner":"1.0"}'
            if args[-1] == "--version":
                return (f"codex-cli {case.version}" if args[-2].endswith("codex")
                        else "Chrome 0.0-fixture")
            return ""

        with patch.object(health, "command", side_effect=command):
            health.installed(self.manifest)
            entrypoint.unlink()
            entrypoint.symlink_to("/opt/kelpie/codex/vendor/x86_64-unknown-linux-musl/bin/codex")
            with self.assertRaisesRegex(prepare.InputError, "entrypoint differs"):
                health.installed(self.manifest)
            self.manifest["architecture"] = "amd64"
            with self.assertRaisesRegex(prepare.InputError, "invalid Codex installed inventory"):
                health.installed(self.manifest)

    def test_browser_rejects_missing_malformed_or_mismatched_completion(self):
        record = {"schema_version": 1, "status": "browser_dom_passed",
                  "browser_version": "1.2.3.4"}
        invalid = ["", '<p id="probe">4</p>', "[]", "null", "x" * 4097,
                   '{"schema_version":1,"schema_version":1}',
                   *[json.dumps(record | change) for change in (
                       {"schema_version": True}, {"status": "browser_started"},
                       {"browser_version": "9.9.9.9"}, {"extra": True})]]
        for output in invalid:
            with self.subTest(output=output[:20]), \
                    patch.object(health.pwd, "getpwnam",
                                 return_value=SimpleNamespace(pw_uid=1234, pw_gid=1234)), \
                    patch.object(health.os, "chown"), \
                    patch.object(health, "smoke_service", return_value=output):
                with self.assertRaises(prepare.InputError):
                    health.browser("1.2.3.4")

    def test_smoke_service_owns_a_bounded_cgroup_with_literal_clean_unprivileged_command(self):
        user = SimpleNamespace(pw_uid=1234, pw_gid=1234)
        with patch.object(health, "service_state", return_value={"LoadState": "not-found"}), \
                patch.object(health, "clean_smoke_service") as cleanup, \
                patch.object(health, "command", return_value="synthetic DOM") as command:
            self.assertEqual(health.smoke_service(user, "/bin/echo", "$SYNTHETIC;$(false)"),
                             "synthetic DOM")
        args = command.call_args.args
        for option in ("--wait", "--pipe", "--collect", "--quiet", "--no-ask-password",
                       "--service-type=exec", "--expand-environment=no", "--uid=1234",
                       "--gid=1234", "--property=KillMode=control-group",
                       "--property=SendSIGKILL=yes", "--property=FinalKillSignal=SIGKILL",
                       "--property=RuntimeMaxSec=30s", "--property=TimeoutStartSec=5s",
                       "--property=TimeoutStopSec=5s", "--property=Restart=no",
                       "--property=Delegate=no", "--slice=system.slice"):
            self.assertIn(option, args)
        self.assertEqual(args[-6:], ("/usr/bin/env", "-i",
                                    "PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                    "LANG=C.UTF-8", "/bin/echo", "$SYNTHETIC;$(false)"))
        self.assertEqual(command.call_args.kwargs, {"timeout": 45})
        unit = next(arg.removeprefix("--unit=") for arg in args if arg.startswith("--unit="))
        owner = next(arg.removeprefix("--description=") for arg in args
                     if arg.startswith("--description="))
        self.assertRegex(unit, r"^kelpie-image-smoke-[0-9a-f]{32}\.service$")
        cleanup.assert_called_once_with(unit, owner)

    def test_smoke_service_cleans_up_on_command_error_timeout_and_interruption(self):
        user = SimpleNamespace(pw_uid=1234, pw_gid=1234)
        failures = (subprocess.CalledProcessError(7, "synthetic"),
                    subprocess.TimeoutExpired("synthetic", 45), KeyboardInterrupt())
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), \
                    patch.object(health, "service_state",
                                 return_value={"LoadState": "not-found"}), \
                    patch.object(health, "clean_smoke_service") as cleanup, \
                    patch.object(health, "command", side_effect=failure):
                with self.assertRaises(type(failure)):
                    health.smoke_service(user, "/bin/true")
                cleanup.assert_called_once()

    def test_smoke_service_rejects_existing_unit_before_launch_or_cleanup(self):
        with patch.object(health, "service_state", return_value={"LoadState": "loaded"}), \
                patch.object(health, "command") as command, \
                patch.object(health, "clean_smoke_service") as cleanup:
            with self.assertRaisesRegex(prepare.InputError, "already exists"):
                health.smoke_service(SimpleNamespace(pw_uid=1234, pw_gid=1234), "/bin/true")
        command.assert_not_called()
        cleanup.assert_not_called()

    def test_smoke_service_requires_inspectable_cgroup_v2_before_launch(self):
        (self.root / "sys/fs/cgroup/cgroup.controllers").unlink()
        with patch.object(health, "service_state") as state, \
                patch.object(health, "command") as command:
            with self.assertRaisesRegex(prepare.InputError, "cgroup v2"):
                health.smoke_service(SimpleNamespace(pw_uid=1234, pw_gid=1234), "/bin/true")
        state.assert_not_called()
        command.assert_not_called()

    def test_cleanup_stops_only_owned_transient_unit_and_requires_empty_cgroup(self):
        unit, owner = "kelpie-image-smoke-" + "a" * 32 + ".service", "synthetic-owner"
        group = "/system.slice/" + unit
        owned = {"LoadState": "loaded", "Description": owner, "Transient": "yes",
                 "ActiveState": "active", "ControlGroup": group}
        with patch.object(health, "service_state",
                          side_effect=[owned, {"LoadState": "not-found"}]), \
                patch.object(health, "command") as command:
            health.clean_smoke_service(unit, owner)
        command.assert_called_once_with("systemctl", "--no-ask-password", "stop", unit, timeout=10)
        self.put("sys/fs/cgroup" + group + "/cgroup.events", "populated 1\nfrozen 0\n")
        with patch.object(health, "service_state", return_value={"LoadState": "not-found"}):
            with self.assertRaisesRegex(prepare.InputError, "processes remain"):
                health.clean_smoke_service(unit, owner)
            self.put("sys/fs/cgroup" + group + "/cgroup.events", "populated 0\nfrozen 0\n")
            health.clean_smoke_service(unit, owner)

    def test_cleanup_rejects_foreign_ownership_without_stopping_anything(self):
        unit, owner = "kelpie-image-smoke-" + "b" * 32 + ".service", "synthetic-owner"
        owned = {"LoadState": "loaded", "Description": owner, "Transient": "yes",
                 "ActiveState": "active", "ControlGroup": "/system.slice/" + unit}
        for change in ({"Description": "another-owner"}, {"Transient": "no"},
                       {"ControlGroup": "/system.slice/unrelated.service"}):
            with self.subTest(change=change), \
                    patch.object(health, "service_state", return_value=owned | change), \
                    patch.object(health, "command") as command:
                with self.assertRaisesRegex(prepare.InputError, "ownership"):
                    health.clean_smoke_service(unit, owner)
                command.assert_not_called()

    def test_cleanup_accepts_gc_race_only_after_confirming_absence_and_no_processes(self):
        unit, owner = "kelpie-image-smoke-" + "c" * 32 + ".service", "synthetic-owner"
        owned = {"LoadState": "loaded", "Description": owner, "Transient": "yes",
                 "ActiveState": "inactive", "ControlGroup": ""}
        for state, populated, accepted in (({"LoadState": "not-found"}, False, True),
                                          ({"LoadState": "not-found"}, True, False),
                                          (owned, False, False)):
            with self.subTest(state=state, populated=populated), \
                    patch.object(health, "service_state", side_effect=[owned, state]), \
                    patch.object(health, "command", side_effect=subprocess.CalledProcessError(
                        5, "synthetic missing service")):
                self.put("sys/fs/cgroup/system.slice/" + unit + "/cgroup.events",
                         "populated " + str(int(populated)) + "\n")
                if accepted:
                    health.clean_smoke_service(unit, owner)
                else:
                    with self.assertRaises((prepare.InputError, subprocess.CalledProcessError)):
                        health.clean_smoke_service(unit, owner)

    def test_cleanup_failure_cannot_turn_browser_output_into_success(self):
        with patch.object(health, "service_state", return_value={"LoadState": "not-found"}), \
                patch.object(health, "command", return_value="synthetic DOM"), \
                patch.object(health, "clean_smoke_service",
                             side_effect=prepare.InputError("cleanup failed")):
            with self.assertRaisesRegex(prepare.InputError, "cleanup failed"):
                health.smoke_service(SimpleNamespace(pw_uid=1234, pw_gid=1234), "/bin/true")

    def test_service_state_uses_only_fixed_metadata_and_rejects_ambiguous_results(self):
        state = {"LoadState": "not-found", "Description": "", "Transient": "no",
                 "ActiveState": "inactive", "ControlGroup": ""}
        valid = "\n".join(name + "=" + value for name, value in state.items()) + "\n"
        with patch.object(health.subprocess, "run", return_value=SimpleNamespace(
                returncode=1, stdout=valid)) as run:
            self.assertEqual(health.service_state("synthetic.service"), state)
        self.assertEqual(run.call_args.kwargs["env"],
                         {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)
        for code, output in ((2, valid), (0, valid + "LoadState=loaded\n"),
                             (0, "LoadState=not-found\n"), (0, "x" * 4096),
                             (1, valid.replace("not-found", "loaded")), (0, "invalid line")):
            with self.subTest(code=code, output=output[:20]), \
                    patch.object(health.subprocess, "run", return_value=SimpleNamespace(
                        returncode=code, stdout=output)):
                with self.assertRaises(prepare.InputError):
                    health.service_state("synthetic.service")

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
        checks[-1].assert_called_once_with(self.manifest["browser"]["version"])

    def test_probe_rejects_manifest_architecture_before_package_or_browser_checks(self):
        with patch.object(health, "guard"), patch.object(health, "command"), \
                patch.object(health.platform, "machine", return_value="aarch64"), \
                patch.object(health.prepare, "read_manifest", return_value=self.manifest), \
                patch.object(health, "identities") as identities, \
                patch.object(health, "installed") as installed, \
                patch.object(health, "browser") as browser:
            with self.assertRaisesRegex(prepare.InputError, "architecture differs"):
                health.probe()
        identities.assert_not_called()
        installed.assert_not_called()
        browser.assert_not_called()

    def test_arm_guard_preserves_root_dmi_ubuntu_and_kvm_requirements(self):
        self.put("sys/class/dmi/id/product_name", health.PROBE_PRODUCT)
        with patch.object(health.platform, "system", return_value="Linux"), \
                patch.object(health.platform, "machine", return_value="aarch64"), \
                patch.object(health.os, "geteuid", return_value=0), \
                patch.object(health.platform, "freedesktop_os_release", return_value={
                    "ID": "ubuntu", "VERSION_ID": "24.04"}), \
                patch.object(health, "command", return_value="kvm") as command:
            health.guard()
            command.return_value = "qemu"
            with self.assertRaisesRegex(prepare.InputError, "unsupported probe guest"):
                health.guard()
            command.return_value = "kvm"
            self.put("sys/class/dmi/id/product_name", "another guest")
            with self.assertRaisesRegex(prepare.InputError, "identity mismatch"):
                health.guard()

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
