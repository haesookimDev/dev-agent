"""Synthetic candidates and mocked QEMU; these tests are not physical KVM evidence."""

import base64
import contextlib
import copy
import io
import json
import os
import stat
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from infra.images import boot, build, health, prepare, qga
from infra.images.tests import test_build as fixtures


class BootTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.BuildTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.root, self.source = fixture.root, fixture.output
        self.manifest = fixture.manifest
        with patch.object(build, "guard_host"), \
                patch.object(build, "command", side_effect=fixture.fake_command):
            self.receipt = build.build(fixture.bundle, self.source,
                                       Path(sys.executable), approved=True)
        self.output = self.root / "probe"
        self.digest = build.measure(self.source / "inputs/manifest.json")["sha256"]
        self.report = {
            "schema_version": 1, "status": "guest_smoke_passed", "release_eligible": False,
            "image_version": self.manifest["image_version"], "manifest_sha256": self.digest,
            "machine_id_sha256": "a" * 64, "boot_id_sha256": "b" * 64,
            "checks": dict.fromkeys(health.CHECKS, True),
        }
        self.process = Mock(pid=os.getpid())
        self.process.poll.return_value = None

    def encoded_status(self, report=None):
        data = json.dumps(self.report if report is None else report).encode()
        return {"exited": True, "exitcode": 0, "out-data": base64.b64encode(data).decode()}

    @staticmethod
    def capabilities():
        return {"supported_commands": [{"name": name, "enabled": True} for name in
                                       ("guest-exec", "guest-exec-status", "guest-shutdown")]}

    def test_stage_rechecks_and_copies_without_changing_original_candidate(self):
        receipt, manifest = boot.stage(self.source, self.output)
        self.assertEqual((receipt, manifest), (self.receipt, self.manifest))
        original = self.source / "image/kelpie.qcow2"
        candidate = self.output / "candidate.qcow2"
        self.assertEqual(original.read_bytes(), candidate.read_bytes())
        self.assertNotEqual(original.stat().st_ino, candidate.stat().st_ino)
        for path in self.output.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertFalse((self.output / "boot-smoke.json").exists())

    def test_invalid_receipts_fail_before_staging(self):
        path = self.source / "candidate.json"
        for change in ({"status": "partial"}, {"release_eligible": True},
                       {"release_eligible": 0}, {"schema_version": True}, {"extra": 1},
                       {"recipe_sha256": "c" * 64}, {"image_version": "wrong.1"},
                       {"image": self.receipt["image"] | {"file": "../kelpie.qcow2"}},
                       {"image": self.receipt["image"] | {"size_bytes": True}}):
            with self.subTest(change=change):
                path.write_text(json.dumps(self.receipt | change))
                with self.assertRaises(prepare.InputError):
                    boot.stage(self.source, self.output)
                self.assertFalse(self.output.exists())

    def test_changed_payload_or_recipe_never_creates_success_receipt(self):
        (self.source / "image/kelpie.qcow2").write_bytes(b"changed candidate")
        with self.assertRaises(prepare.InputError):
            boot.stage(self.source, self.output)
        self.assertFalse((self.output / "boot-smoke.json").exists())
        (self.source / "recipe.json").write_text("{}")
        with self.assertRaisesRegex(prepare.InputError, "recipe hash"):
            boot.stage(self.source, self.root / "other")
        self.assertFalse((self.root / "other").exists())

    def test_existing_output_unsafe_path_and_public_or_symlink_source_are_rejected(self):
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("preserve")
        with self.assertRaises(FileExistsError):
            boot.stage(self.source, self.output)
        self.assertEqual(marker.read_text(), "preserve")
        for name in ("unsafe,path", "space path", "x" * 100):
            with self.subTest(name=name), self.assertRaises(prepare.InputError):
                boot.stage(self.source, self.root / name)
        link = self.root / "link"
        link.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(prepare.InputError):
            boot.stage(link, self.root / "other")
        self.source.chmod(0o755)
        with self.assertRaises(prepare.InputError):
            boot.stage(self.source, self.root / "other")

    def test_probe_has_private_overlay_no_nic_or_public_management_and_no_tcg(self):
        boot.stage(self.source, self.output)
        with patch.object(build, "inspect_disk") as inspect, \
                patch.object(build, "command") as command:
            boot.prepare_vm(self.output)
        inspect.assert_called_once_with(self.output / "candidate.qcow2", self.output)
        create, iso = [call.args[0] for call in command.call_args_list]
        self.assertEqual(create[:7], ["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b"])
        self.assertEqual(create[-2:], [str(self.output / "candidate.qcow2"),
                                      str(self.output / "probe.qcow2")])
        self.assertEqual(iso[0], "xorriso")
        user = json.loads((self.output / "seed/user-data").read_text().split("\n", 1)[1])
        self.assertEqual(user, {"users": [], "disable_root": True, "ssh_pwauth": False,
                                "package_update": False, "package_upgrade": False})
        self.assertEqual(build.read_json(self.output / "seed/network-config"),
                         {"version": 2, "ethernets": {}})
        args = boot.qemu_arguments(self.output, self.output / "qga.sock")
        self.assertEqual(args[args.index("-nic") + 1], "none")
        self.assertIn("q35,accel=kvm", args)
        self.assertIn(f"type=1,manufacturer=KVM,product={health.PROBE_PRODUCT}", args)
        for forbidden in ("-vnc", "-netdev", "-qmp", "-daemonize"):
            self.assertNotIn(forbidden, args)
        self.assertNotIn("tcg", " ".join(args))
        self.assertNotIn("tcp:", " ".join(args))
        self.assertNotIn(str(self.source / "image/kelpie.qcow2"), " ".join(args))

    def test_reports_require_exact_identity_and_all_boolean_checks(self):
        self.assertEqual(boot.validate_report(self.report, self.manifest, self.digest), self.report)
        for change in ({"schema_version": True}, {"status": "ok"}, {"release_eligible": 0},
                       {"image_version": "wrong.1"}, {"manifest_sha256": "c" * 64},
                       {"machine_id_sha256": "raw-id"}, {"boot_id_sha256": None}, {"extra": 1},
                       {"checks": {}}, {"checks": self.report["checks"] | {"browser_dom": 1}},
                       {"checks": self.report["checks"] | {"browser_dom": False}}):
            with self.subTest(change=change), self.assertRaises(prepare.InputError):
                boot.validate_report(self.report | change, self.manifest, self.digest)

    def test_arm_probe_uses_private_uefi_virtio_gpu_scsi_and_only_kvm(self):
        args = boot.qemu_arguments(self.output, self.output / "qga.sock", "arm64")
        self.assertEqual(args[0], "qemu-system-aarch64")
        self.assertIn("virt,accel=kvm,gic-version=host", args)
        self.assertIn("virtio-gpu-pci", args)
        self.assertIn("scsi-cd,drive=seed,bus=scsi0.0", args)
        self.assertIn(f"file={self.output / 'firmware/AAVMF_CODE.no-secboot.fd'},"
                      "format=raw,if=pflash,unit=0,readonly=on", args)
        self.assertIn(f"file={self.output / 'firmware/AAVMF_VARS.fd'},"
                      "format=raw,if=pflash,unit=1", args)
        self.assertEqual(args[args.index("-nic") + 1], "none")
        self.assertIn(f"type=1,manufacturer=KVM,product={health.PROBE_PRODUCT}", args)
        for value in ("-vga", "-parallel", "-netdev", "-vnc", "-daemonize"):
            self.assertNotIn(value, args)
        self.assertNotIn("tcg", " ".join(args))
        self.assertNotIn("/usr/share/AAVMF", " ".join(args))
        with self.assertRaises(prepare.InputError):
            boot.qemu_arguments(self.output, self.output / "qga.sock", "riscv64")

    def arm_source(self):
        manifest_path = self.source / "inputs/manifest.json"
        manifest = build.read_json(manifest_path) | {"architecture": "arm64"}
        manifest_path.write_text(json.dumps(manifest))
        directory = self.source / "firmware"
        directory.mkdir()
        records = {}
        for name in build.ARM_FIRMWARE:
            path = directory / name
            path.write_bytes(f"synthetic firmware: {name}".encode())
            records[name] = build.measure(path)
        recipe = build.read_json(self.source / "recipe.json")
        recipe.update(firmware=records, manifest_sha256=build.measure(manifest_path)["sha256"])
        (self.source / "recipe.json").write_text(json.dumps(recipe))
        self.receipt["recipe_sha256"] = build.measure(self.source / "recipe.json")["sha256"]
        (self.source / "candidate.json").write_text(json.dumps(self.receipt))
        return records

    def test_arm_stage_copies_verified_blank_firmware_without_reusing_builder_nvram(self):
        records = self.arm_source()
        with patch.object(build, "ARM_FIRMWARE", records):
            _, manifest = boot.stage(self.source, self.output)
        self.assertEqual(manifest["architecture"], "arm64")
        for name, record in records.items():
            source, target = self.source / "firmware" / name, self.output / "firmware" / name
            self.assertEqual(build.measure(target), record)
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
        (self.output / "firmware/AAVMF_VARS.fd").write_bytes(b"first boot private state")
        self.assertEqual(build.measure(self.source / "firmware/AAVMF_VARS.fd"),
                         records["AAVMF_VARS.fd"])

    def test_arm_stage_rejects_tampered_extra_or_unpinned_firmware_before_output(self):
        records = self.arm_source()
        directory = self.source / "firmware"
        with self.assertRaisesRegex(prepare.InputError, "firmware differs from pin"):
            boot.stage(self.source, self.output)
        self.assertFalse(self.output.exists())
        (directory / "unexpected").write_bytes(b"extra firmware")
        with patch.object(build, "ARM_FIRMWARE", records), \
                self.assertRaisesRegex(prepare.InputError, "unexpected firmware"):
            boot.stage(self.source, self.output)
        self.assertFalse(self.output.exists())
        (directory / "unexpected").unlink()
        (directory / "AAVMF_VARS.fd").write_bytes(b"changed vars")
        with patch.object(build, "ARM_FIRMWARE", records), \
                self.assertRaisesRegex(prepare.InputError, "firmware differs from pin"):
            boot.stage(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_mismatched_probe_architecture_never_stages_or_runs_vm(self):
        with patch.object(build, "guard_host"), \
                patch.object(build.platform, "machine", return_value="aarch64"), \
                patch.object(boot, "stage") as stage, patch.object(boot, "boot_once") as run:
            with self.assertRaisesRegex(prepare.InputError, "native KVM host"):
                boot.probe(self.source, self.output, approved=True)
        stage.assert_not_called()
        run.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_qga_exec_waits_for_readiness_without_resetting_deadline(self):
        agent = Mock()
        agent.call.side_effect = [self.capabilities(), {"pid": 7},
                                  {"exited": True, "exitcode": 1}, {"pid": 8},
                                  {"exited": False}, self.encoded_status()]
        with patch.object(boot.time, "sleep"):
            result = boot.check_guest(agent, self.process, self.manifest, self.digest,
                                      time.monotonic() + 5)
        self.assertEqual(result, self.report)
        calls = agent.call.call_args_list
        self.assertEqual([call.args[0] for call in calls],
                         ["guest-info", "guest-exec", "guest-exec-status", "guest-exec",
                          "guest-exec-status", "guest-exec-status"])
        execution = calls[1].args[1]
        self.assertEqual(execution["path"], "/usr/bin/env")
        self.assertEqual(execution["arg"], [
            "-i", f"PATH={build.TOOL_PATH}", "LANG=C.UTF-8",
            "/bin/sh", "-c", 'exec 2>/dev/null; exec "$@"', "kelpie-image-smoke",
            "/usr/bin/python3", "/opt/kelpie/image-health/health.py",
        ])
        self.assertIs(execution["capture-output"], True)

    def test_capture_compatibility_wrapper_discards_stderr_and_preserves_exit_status(self):
        agent = Mock()
        agent.call.side_effect = [self.capabilities(), {"pid": 7}, self.encoded_status()]
        boot.check_guest(agent, self.process, self.manifest, self.digest, time.monotonic() + 5)
        execution = agent.call.call_args_list[1].args[1]
        self.assertIs(execution["capture-output"], True)
        # Use the actual fixed shell boundary with a synthetic probe, not the guest installer.
        literal = "literal;$(printf synthetic-command-substitution)"
        for code in (0, 7):
            script = ("import os,sys; assert 'KELPIE_TEST_SECRET' not in os.environ; "
                      f"assert sys.argv[1] == {literal!r}; "
                      "print('synthetic-report'); "
                      "print('synthetic-sensitive-diagnostic',file=sys.stderr); "
                      f"sys.exit({code})")
            args = [execution["path"], *execution["arg"][:-2],
                    sys.executable, "-c", script, literal]
            with self.subTest(code=code):
                result = subprocess.run(args, capture_output=True, text=True, timeout=5,
                                        env={"KELPIE_TEST_SECRET": "synthetic-value"})
                self.assertEqual(result.returncode, code)
                self.assertEqual(result.stdout, "synthetic-report\n")
                self.assertEqual(result.stderr, "")

    def test_disabled_or_malformed_capabilities_do_not_enable_guest_rpcs(self):
        for info in (None, {}, {"supported_commands": [{"name": [], "enabled": True}]},
                     {"supported_commands": [{"name": "guest-exec", "enabled": False}]}):
            agent = Mock()
            agent.call.return_value = info
            with self.subTest(info=info), self.assertRaises(prepare.InputError):
                boot.check_guest(agent, self.process, self.manifest, self.digest,
                                 time.monotonic() + 5)
            agent.call.assert_called_once_with("guest-info")

    def test_invalid_pid_status_or_output_cannot_pass(self):
        duplicate = base64.b64encode(b'{"schema_version":1,"schema_version":1}').decode()
        sequences = [
            [{"pid": True}], [{"pid": 0}], [{"pid": 7}, {"exited": 1}],
            [{"pid": 7}, {"exited": True, "exitcode": 1, "err-data": "eA=="}],
            *[[{"pid": 7}, self.encoded_status() | change] for change in (
                {"out-truncated": True}, {"out-data": "not base64!"}, {"out-data": duplicate},
                {"out-data": "x" * (16 * 1024 + 1)}, {"signal": 9},
                {"err-data": base64.b64encode(b"synthetic-sensitive-diagnostic").decode()},
                {"err-truncated": True},
            )],
        ]
        for sequence in sequences:
            agent = Mock()
            agent.call.side_effect = [self.capabilities(), *sequence]
            with self.subTest(sequence=sequence), \
                    self.assertRaises((prepare.InputError, ValueError)):
                boot.check_guest(agent, self.process, self.manifest, self.digest,
                                 time.monotonic() + 5)

    def test_dead_vm_and_expired_deadline_stop_before_exec(self):
        agent = Mock()
        agent.call.return_value = self.capabilities()
        for alive, deadline in ((True, time.monotonic() - 1), (False, time.monotonic() + 5)):
            self.process.poll.return_value = None if alive else 1
            agent.call.reset_mock()
            with self.subTest(alive=alive), self.assertRaises(prepare.InputError):
                boot.check_guest(agent, self.process, self.manifest, self.digest, deadline)
            agent.call.assert_called_once_with("guest-info")

    def test_wait_agent_retries_startup_with_same_deadline_and_expected_peer(self):
        deadline = time.monotonic() + 5
        responses = [FileNotFoundError(), self.process]
        with patch.object(qga, "GuestAgent", side_effect=responses) as ctor, \
                patch.object(boot.time, "sleep"):
            self.assertIs(boot.wait_agent(self.output / "qga.sock", self.process, deadline),
                          self.process)
        self.assertEqual(ctor.call_count, 2)
        for call in ctor.call_args_list:
            self.assertEqual(call.args, (self.output / "qga.sock", deadline))
            self.assertEqual(call.kwargs, {"peer_pid": self.process.pid})
        with patch.object(qga, "GuestAgent") as ctor:
            with self.assertRaises(prepare.InputError):
                boot.wait_agent(self.output / "qga.sock", self.process, time.monotonic() - 1)
        ctor.assert_not_called()

    def test_shutdown_requires_clean_exit_and_always_stops_owned_process_group(self):
        boot.stage(self.source, self.output)
        for index, outcome in enumerate((0, 1, subprocess.TimeoutExpired("qemu", 60))):
            manager, agent = MagicMock(), MagicMock()
            manager.__enter__.return_value = self.process
            self.process.wait.side_effect = outcome if isinstance(outcome, Exception) else None
            self.process.wait.return_value = outcome
            with patch.object(boot.subprocess, "Popen", return_value=manager) as popen, \
                    patch.object(boot, "wait_agent", return_value=agent), \
                    patch.object(boot, "check_guest", return_value=self.report), \
                    patch.object(build, "stop_group") as stop:
                if outcome == 0:
                    self.assertEqual(boot.boot_once(self.output, index, self.manifest, self.digest),
                                     self.report)
                else:
                    with self.assertRaises((prepare.InputError, subprocess.TimeoutExpired)):
                        boot.boot_once(self.output, index, self.manifest, self.digest)
                stop.assert_called_once_with(self.process)
                agent.__enter__.return_value.send.assert_called_once_with(
                    "guest-shutdown", {"mode": "powerdown"})
                self.assertIs(popen.call_args.kwargs["start_new_session"], True)
                for stream in ("stdin", "stdout", "stderr"):
                    self.assertEqual(popen.call_args.kwargs[stream], subprocess.DEVNULL)

    def test_smoke_failure_stops_owned_vm_without_claiming_clean_shutdown(self):
        boot.stage(self.source, self.output)
        manager = MagicMock()
        manager.__enter__.return_value = self.process
        with patch.object(boot.subprocess, "Popen", return_value=manager), \
                patch.object(boot, "wait_agent", side_effect=prepare.InputError("not ready")), \
                patch.object(build, "stop_group") as stop:
            with self.assertRaises(prepare.InputError):
                boot.boot_once(self.output, 0, self.manifest, self.digest)
        stop.assert_called_once_with(self.process)
        self.process.wait.assert_not_called()

    def test_two_distinct_boots_keep_machine_identity_and_never_release(self):
        reports = [self.report, self.report | {"boot_id_sha256": "c" * 64}]
        with patch.object(build, "guard_host") as guard, patch.object(boot, "prepare_vm"), \
                patch.object(boot, "boot_once", side_effect=reports) as run:
            result = boot.probe(self.source, self.output, approved=True)
        guard.assert_called_once_with(True)
        self.assertEqual([call.args[1] for call in run.call_args_list], [0, 1])
        self.assertEqual(result["boots"], reports)
        self.assertEqual(result["status"], "boot_smoke_passed_unreleased")
        self.assertIs(result["release_eligible"], False)
        self.assertEqual(build.read_json(self.output / "boot-smoke.json"), result)
        self.assertEqual(result["image"], build.measure(self.source / "image/kelpie.qcow2"))
        self.assertIn("browser_probe.py", result["probe_tooling_sha256"])
        for name, digest in result["probe_tooling_sha256"].items():
            self.assertEqual(build.measure(build.ROOT / "infra/images" / name)["sha256"], digest)

    def test_reused_boot_id_changed_machine_or_failed_boot_has_no_success_receipt(self):
        for index, reports in enumerate((
            [self.report, self.report],
            [self.report, self.report | {
                "machine_id_sha256": "d" * 64, "boot_id_sha256": "c" * 64}],
            [self.report, prepare.InputError("second boot failed")],
        )):
            output = self.root / f"fail-{index}"
            with patch.object(build, "guard_host"), patch.object(boot, "prepare_vm"), \
                    patch.object(boot, "boot_once", side_effect=copy.deepcopy(reports)):
                with self.assertRaises(prepare.InputError):
                    boot.probe(self.source, output, approved=True)
            self.assertFalse((output / "boot-smoke.json").exists())

    def test_modified_candidate_is_rejected_after_boot_without_touching_original(self):
        original = (self.source / "image/kelpie.qcow2").read_bytes()

        def run(output, index, manifest, digest):
            (output / "candidate.qcow2").write_bytes(b"unexpected change")
            return self.report | {"boot_id_sha256": ("b" if index == 0 else "c") * 64}

        with patch.object(build, "guard_host"), patch.object(boot, "prepare_vm"), \
                patch.object(boot, "boot_once", side_effect=run):
            with self.assertRaisesRegex(prepare.InputError, "candidate changed"):
                boot.probe(self.source, self.output, approved=True)
        self.assertFalse((self.output / "boot-smoke.json").exists())
        self.assertEqual((self.source / "image/kelpie.qcow2").read_bytes(), original)

    def test_real_cli_without_host_acknowledgement_never_stages_or_runs_vm(self):
        result = subprocess.run(
            [sys.executable, str(build.ROOT / "infra/images/boot.py"), "--build-dir",
             str(self.source), "--output", str(self.output)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "image boot rejected; candidate remains unreleased\n")
        self.assertFalse(self.output.exists())

    def test_build_reserves_socket_path_budget_before_staging(self):
        parent = self.root.resolve()
        output = parent / ("x" * (70 - len(str(parent)) - 1))
        with patch.object(build, "guard_host"), patch.object(build, "stage") as stage:
            with self.assertRaisesRegex(prepare.InputError, "automatic boot probe"):
                build.build(self.root / "bundle", output, Path(sys.executable), approved=True)
        stage.assert_not_called()
        self.assertFalse(output.exists())

    def test_build_cli_automatically_probes_and_propagates_failure(self):
        args = ["--bundle", str(self.root / "bundle"), "--output", str(self.output),
                "--packer", sys.executable, "--execute-on-dedicated-host"]
        for success in (True, False):
            stdout, stderr = io.StringIO(), io.StringIO()
            order = Mock()
            with patch.object(build, "build", return_value=self.receipt) as builder, \
                    patch.object(boot, "probe") as probe, \
                    contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                order.attach_mock(builder, "build")
                order.attach_mock(probe, "probe")
                probe.return_value = {"status": "synthetic"}
                if not success:
                    probe.side_effect = subprocess.TimeoutExpired("synthetic private diagnostic", 1)
                self.assertEqual(build.main(args), 0 if success else 1)
            self.assertEqual([call[0] for call in order.mock_calls], ["build", "probe"])
            probe.assert_called_once_with(self.output, self.output / "boot-check", approved=True)
            if success:
                self.assertIs(json.loads(stdout.getvalue())["release_eligible"], False)
            else:
                self.assertEqual(stdout.getvalue(), "")
                self.assertNotIn("private diagnostic", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
