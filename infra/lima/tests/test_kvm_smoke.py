"""Host-independent policy and protocol tests, never a simulated KVM pass."""

import gzip
import importlib.util
import io
import json
import stat
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location(
    "kvm_smoke", Path(__file__).resolve().parents[1] / "kvm_smoke.py")
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class SmokeTests(unittest.TestCase):
    def test_qemu_requires_kvm_and_has_no_disks_nics_or_display(self):
        command = smoke.qemu_command("kernel", "initramfs", "serial", "socket")
        self.assertEqual(command, [
            "/usr/bin/qemu-system-aarch64", "-machine", "virt,accel=kvm,gic-version=host",
            "-cpu", "host", "-smp", "1", "-m", "512M", "-nodefaults",
            "-nic", "none", "-display", "none", "-monitor", "none", "-no-reboot",
            "-kernel", "kernel", "-initrd", "initramfs",
            "-append", "console=ttyAMA0 rdinit=/init panic=-1",
            "-serial", "file:serial", "-qmp", "unix:socket,server=on,wait=off", "-S",
        ])

    def test_kvm_must_be_present_and_enabled_not_merely_truthy(self):
        smoke.require_kvm({"present": True, "enabled": True})
        for result in ({}, {"present": True}, {"present": True, "enabled": False},
                       {"present": False, "enabled": True}, {"present": 1, "enabled": 1}):
            with self.subTest(result=result), self.assertRaises(ValueError):
                smoke.require_kvm(result)

    def test_boot_requires_exact_current_nonce_arm_and_clean_exit(self):
        marker = "KELPIE_KVM_BOOT_OK:nonce:aarch64"
        smoke.require_boot(0, "kernel log\r\n" + marker + "\r\n", "nonce")
        cases = [(1, marker), (0, marker.replace("nonce", "stale")),
                 (0, marker.replace("aarch64", "x86_64")), (0, "prefix " + marker),
                 (0, marker + "\nKernel panic"), (0, "")]
        for returncode, serial in cases:
            with self.subTest(serial=serial), self.assertRaises(ValueError):
                smoke.require_boot(returncode, serial, "nonce")

    def test_archive_has_only_fixed_files_and_console_without_host_mounts(self):
        archive = gzip.decompress(smoke.initramfs(b"static-busybox", "nonce"))
        entries = {}
        offset = 0
        while offset < len(archive):
            self.assertEqual(archive[offset:offset + 6], b"070701")
            fields = [int(archive[offset + 6 + index * 8:offset + 14 + index * 8], 16)
                      for index in range(13)]
            size, name_size = fields[6], fields[11]
            name = archive[offset + 110:offset + 110 + name_size - 1].decode()
            data_offset = (offset + 110 + name_size + 3) & ~3
            entries[name] = (fields, archive[data_offset:data_offset + size])
            offset = (data_offset + size + 3) & ~3
        self.assertEqual(set(entries), {"bin", "dev", "dev/console", "bin/busybox",
                                       "init", "TRAILER!!!"})
        console = entries["dev/console"][0]
        self.assertEqual((console[1], console[9:11]), (stat.S_IFCHR | 0o600, [5, 1]))
        self.assertEqual(entries["bin/busybox"][1], b"static-busybox")
        self.assertIn(b"KELPIE_KVM_BOOT_OK:nonce:aarch64", entries["init"][1])
        self.assertTrue(entries["init"][1].endswith(b"/bin/busybox poweroff -f\n"))

    def test_deadline_expiration_is_failure(self):
        with patch.object(smoke.time, "monotonic", return_value=10):
            self.assertEqual(smoke.remaining(11), 1)
            with self.assertRaises(TimeoutError):
                smoke.remaining(10)

    def test_rejects_malformed_empty_or_oversized_qmp(self):
        for message in (b"", b"[]\n", b"not json\n", b" " * 65537):
            with self.subTest(message=message[:10]), self.assertRaises((ValueError, TypeError)):
                smoke.qmp_read(io.BytesIO(message), Mock(), smoke.time.monotonic() + 1)

    def test_qmp_ignores_events_and_matches_response_id(self):
        response = {"return": {"present": True, "enabled": True}, "id": "query-kvm"}
        stream = Mock()
        stream.readline.side_effect = [b'{"event":"STOP"}\n',
                                       json.dumps(response).encode() + b"\n"]
        result = smoke.qmp_execute(stream, Mock(), smoke.time.monotonic() + 1, "query-kvm")
        self.assertEqual(result, response["return"])
        stream.write.assert_called_once_with(b'{"execute": "query-kvm", "id": "query-kvm"}\n')

    def test_qmp_command_errors_fail_closed(self):
        stream = Mock()
        stream.readline.return_value = b'{"id":"query-kvm","error":{"class":"Error"}}\n'
        with self.assertRaisesRegex(ValueError, "QMP command failed"):
            smoke.qmp_execute(stream, Mock(), smoke.time.monotonic() + 1, "query-kvm")

    def test_preflight_rejects_mac_x86_and_root_before_opening_kvm(self):
        for system, machine, uid in (("Darwin", "arm64", 1000), ("Linux", "x86_64", 1000),
                                     ("Linux", "aarch64", 0)):
            with (self.subTest(system=system, machine=machine, uid=uid),
                  patch.object(smoke.platform, "system", return_value=system),
                  patch.object(smoke.platform, "machine", return_value=machine),
                  patch.object(smoke.os, "geteuid", return_value=uid),
                  patch.object(smoke.os, "open") as opened,
                  self.assertRaises(ValueError)):
                smoke.preflight()
            opened.assert_not_called()

    def test_invalid_timeout_or_output_fails_before_accessing_host(self):
        with patch.object(smoke, "preflight") as preflight:
            for timeout in (0, 121):
                with self.assertRaises(ValueError):
                    smoke.run(Path("kernel"), Path("new-output"), timeout)
            with self.assertRaises(ValueError):
                smoke.run(Path("kernel"), Path("output,server=on"), 60)
            preflight.assert_not_called()


if __name__ == "__main__":
    unittest.main()
