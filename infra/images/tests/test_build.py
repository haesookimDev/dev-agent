import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infra.images import build, guest, prepare


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kelpie-builder-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()

        def artifact(name, package=None):
            data = f"synthetic, not a real image or package: {name}".encode()
            (self.assets / name).write_bytes(data)
            item = {"file": name, "version": "1.0-fixture", "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest()}
            if package:
                item["name"] = package
            return item

        self.manifest = {
            "schema_version": 1, "architecture": "amd64", "image_version": "build-fixture.1",
            "ubuntu_snapshot": "20260901T000000Z", "runner_source_commit": "a" * 40,
            "apt_packages": dict.fromkeys(guest.GUEST_PACKAGES, "1.0-fixture"),
            "base_image": artifact("base.qcow2"), "codex": artifact("codex"),
            "browser": artifact("browser.zip"),
            "runner_wheels": [artifact("runner.whl", "kelpie-vm-runner")],
        }
        self.bundle = self.root / "bundle"
        prepare.prepare(self.manifest, self.assets, self.bundle)
        self.output = self.root / "run"
        self.disk = {"format": "qcow2", "virtual-size": 40 * 1024**3}

    def test_stages_independent_verified_inputs_and_exact_recipe(self):
        self.assertEqual(build.stage(self.bundle, self.output), self.manifest)
        for item in prepare.artifacts(self.manifest):
            source = self.bundle / "files" / item["file"]
            target = self.output / "inputs/files" / item["file"]
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
            self.assertEqual(target.read_bytes(), source.read_bytes())
        recipe = build.read_json(self.output / "recipe.json")
        self.assertIs(recipe["release_eligible"], False)
        for name, digest in recipe["tooling_sha256"].items():
            self.assertEqual(build.measure(self.output / "tooling" / name)["sha256"], digest)
        config = Path(build.environment(self.output)["PACKER_CONFIG"])
        # /dev/null fails real Packer configuration loading with EOF.
        self.assertEqual(json.loads(config.read_text()), {})
        for path in self.output.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertFalse((self.output / "candidate.json").exists())

    def test_rejects_incomplete_or_changed_receipts_before_staging(self):
        record = self.bundle / "inputs-verified.json"
        original = record.read_bytes()
        for change in ({"release_eligible": True}, {"status": "partial"},
                       {"manifest_sha256": "b" * 64}, {"unexpected": True},
                       {"schema_version": True}, {"release_eligible": 0},
                       {"artifact_count": 4.0}):
            with self.subTest(change=change):
                altered = json.loads(original) | change
                record.write_text(json.dumps(altered))
                with self.assertRaises(prepare.InputError):
                    build.stage(self.bundle, self.output)
                self.assertFalse(self.output.exists())
        record.unlink()
        with self.assertRaises(FileNotFoundError):
            build.stage(self.bundle, self.output)
        self.assertFalse(self.output.exists())

    def test_rechecks_copied_payload_bytes(self):
        payload = self.bundle / "files/codex"
        payload.write_bytes(b"x" * payload.stat().st_size)
        with self.assertRaisesRegex(prepare.InputError, "digest mismatch"):
            build.stage(self.bundle, self.output)
        self.assertFalse((self.output / "inputs/inputs-verified.json").exists())
        self.assertFalse((self.output / "candidate.json").exists())

    def test_missing_guest_packages_fail_before_output_creation(self):
        del self.manifest["apt_packages"]["lightdm"]
        bundle = self.root / "minimal"
        prepare.prepare(self.manifest, self.assets, bundle)
        with self.assertRaisesRegex(prepare.InputError, "guest packages"):
            build.stage(bundle, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_output_and_unsafe_paths_are_not_reused(self):
        self.output.mkdir()
        sentinel = self.output / "keep"
        sentinel.write_text("preserve")
        with self.assertRaises(FileExistsError):
            build.stage(self.bundle, self.output)
        self.assertEqual(sentinel.read_text(), "preserve")
        for name in ("space path", "comma,path", "x" * 100):
            with self.subTest(name=name), self.assertRaises(prepare.InputError):
                build.stage(self.bundle, self.root / name)
        os.chmod(self.root, 0o755)
        with self.assertRaisesRegex(prepare.InputError, "mode 0700"):
            build.stage(self.bundle, self.root / "public")

    def fake_command(self, args, output, **kwargs):
        if args[0] == "qemu-img":
            return json.dumps(self.disk)
        if args[0] == "ssh-keygen":
            (output / "build_key").write_text("synthetic disposable key, not cryptographic")
            (output / "build_key.pub").write_text("synthetic public key")
        elif args[1] == "version":
            return f"Packer v{build.PACKER_VERSION}"
        elif args[1] == "build":
            (output / "image").mkdir()
            (output / "image/kelpie.qcow2").write_bytes(b"synthetic unbootable candidate")
        return ""

    def test_simulated_build_orders_commands_and_never_promotes(self):
        with patch.object(build, "guard_host"), \
                patch.object(build, "command", side_effect=self.fake_command) as command:
            result = build.build(self.bundle, self.output, Path(sys.executable), approved=True)
        calls = [call.args[0] for call in command.call_args_list]
        self.assertEqual([args[1] for args in calls],
                         ["version", "info", "-q", "init", "validate", "build", "info"])
        self.assertIn("-on-error=cleanup", calls[5])
        self.assertNotIn("-force", calls[5])
        self.assertEqual(result["status"], "image_built_unverified")
        self.assertIs(result["release_eligible"], False)
        self.assertEqual(build.read_json(self.output / "candidate.json"), result)
        self.assertFalse((self.output / "build_key").exists())
        self.assertFalse((self.output / "build_key.pub").exists())

    def test_failures_at_each_tool_stage_remove_keys_without_success_marker(self):
        for failure in range(7):
            counter = 0
            output = self.root / f"fail-{failure}"

            def fail(args, run, *, failure=failure, **kwargs):
                nonlocal counter
                current = counter
                counter += 1
                if current == failure:
                    raise prepare.InputError("synthetic tool failure")
                return self.fake_command(args, run, **kwargs)

            with self.subTest(failure=failure), patch.object(build, "guard_host"), \
                    patch.object(build, "command", side_effect=fail):
                with self.assertRaises(prepare.InputError):
                    build.build(self.bundle, output, Path(sys.executable), approved=True)
            self.assertFalse((output / "candidate.json").exists())
            self.assertFalse((output / "build_key").exists())
            self.assertFalse((output / "build_key.pub").exists())

    def test_packer_version_mismatch_stops_before_key_or_vm_creation(self):
        with patch.object(build, "guard_host"), \
                patch.object(build, "command", return_value="Packer v0.0.0") as command:
            with self.assertRaisesRegex(prepare.InputError, "version differs"):
                build.build(self.bundle, self.output, Path(sys.executable), approved=True)
        self.assertEqual(command.call_count, 1)
        self.assertFalse((self.output / "candidate.json").exists())

    def test_rejects_external_disk_dependencies_encryption_and_invalid_size(self):
        for change in ({"format": "raw"}, {"backing-filename": "/unapproved/file"},
                       {"encrypted": True}, {"virtual-size": 0}, {"virtual-size": "40G"},
                       {"virtual-size": build.MAX_DISK_BYTES + 1},
                       {"format-specific": None}, {"format-specific": {"data": None}},
                       {"format-specific": {"data": {"encrypt": {}}}},
                       {"format-specific": {"data": {"data-file": "/unapproved/file"}}}):
            info = copy.deepcopy(self.disk) | change
            with self.subTest(change=change), \
                    patch.object(build, "command", return_value=json.dumps(info)):
                with self.assertRaises(prepare.InputError):
                    build.inspect_disk(self.root / "synthetic.qcow2", self.root)

    def test_real_cli_without_acknowledgement_never_creates_output(self):
        result = subprocess.run(
            [sys.executable, str(build.ROOT / "infra/images/build.py"), "--bundle",
             str(self.bundle), "--output", str(self.output), "--packer", sys.executable],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("acknowledgement is required", result.stderr)
        self.assertFalse(self.output.exists())
        self.assertEqual(result.stdout, "")

    def test_host_guard_runs_before_staging_or_commands(self):
        with patch.object(build, "guard_host", side_effect=prepare.InputError("guard")), \
                patch.object(build, "stage") as stage, patch.object(build, "command") as command:
            with self.assertRaises(prepare.InputError):
                build.build(self.bundle, self.output, Path(sys.executable), approved=True)
        stage.assert_not_called()
        command.assert_not_called()

    def test_real_subprocess_has_private_environment_and_discards_diagnostics(self):
        build.stage(self.bundle, self.output)
        script = ("import json,os,sys; print(json.dumps(dict(os.environ))); "
                  "print('noise',file=sys.stderr)")
        with patch.dict(os.environ, {"OPENAI_API_KEY": "synthetic-not-a-real-key",
                                     "PACKER_LOG": "1", "SSH_AUTH_SOCK": "/synthetic"}):
            result = build.command([sys.executable, "-c", script], self.output,
                                   timeout=10, capture=True)
        env = json.loads(result)
        for key in ("OPENAI_API_KEY", "PACKER_LOG", "SSH_AUTH_SOCK"):
            self.assertNotIn(key, env)
        self.assertEqual(env["PACKER_PLUGIN_PATH"], str(self.output / "plugins"))
        self.assertFalse((self.output / "command-output").exists())

    def test_real_command_failure_is_closed_and_scratch_is_removed(self):
        build.stage(self.bundle, self.output)
        with self.assertRaisesRegex(prepare.InputError, "build tool failed"):
            build.command([sys.executable, "-c", "import sys; print('synthetic'); sys.exit(7)"],
                          self.output, timeout=10)
        self.assertFalse((self.output / "command-output").exists())

    def test_timeout_stops_owned_process_group(self):
        build.stage(self.bundle, self.output)
        marker = self.output / "should-not-be-written"
        child = ("import time; from pathlib import Path; time.sleep(1); "
                 f"Path({str(marker)!r}).touch()")
        parent = (f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{child!r}]); "
                  "from pathlib import Path; Path('started').touch(); time.sleep(10)")
        with self.assertRaises(subprocess.TimeoutExpired):
            build.command([sys.executable, "-c", parent], self.output, timeout=0.4)
        self.assertTrue((self.output / "started").exists())
        time.sleep(0.8)
        self.assertFalse(marker.exists())
        self.assertFalse((self.output / "command-output").exists())


if __name__ == "__main__":
    unittest.main()
