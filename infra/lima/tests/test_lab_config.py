"""Fast template policy checks; these are not nested-VM runtime acceptance."""

import re
import subprocess
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parents[1] / "ubuntu-arm64.yaml"


class LabConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = yaml.safe_load(TEMPLATE.read_text(encoding="utf-8"))

    def test_requires_native_arm_virtualization_with_nested_kvm(self):
        for name, expected in {
            "minimumLimaVersion": "2.2.0", "vmType": "vz", "arch": "aarch64",
            "nestedVirtualization": True,
        }.items():
            self.assertEqual(self.config[name], expected)
        self.assertEqual(self.config["vmOpts"], {
            "vz": {"rosetta": {"enabled": False, "binfmt": False}},
        })

    def test_uses_one_dated_digest_locked_ubuntu_image_without_floating_fallback(self):
        self.assertEqual(self.config["base"], [])
        self.assertEqual(len(self.config["images"]), 1)
        image = self.config["images"][0]
        self.assertEqual(set(image), {"location", "arch", "digest"})
        self.assertEqual(image["arch"], "aarch64")
        self.assertRegex(image["location"], re.compile(
            r"^https://cloud-images\.ubuntu\.com/releases/noble/release-\d{8}/"
            r"ubuntu-24\.04-server-cloudimg-arm64\.img$"))
        self.assertRegex(image["digest"], r"^sha256:[a-f0-9]{64}$")

    def test_does_not_mount_host_files_or_attach_other_disks(self):
        for name in ("mounts", "additionalDisks", "copyToHost"):
            self.assertEqual(self.config[name], [])

    def test_disables_automatic_forwarding_for_all_guest_addresses_and_protocols(self):
        self.assertEqual(self.config["networks"], [])
        self.assertEqual(self.config["portForwards"], [{
            "guestIP": "0.0.0.0", "guestIPMustBeZero": False,
            "guestPortRange": [1, 65535], "proto": "any", "ignore": True,
        }])

    def test_does_not_import_host_ssh_keys_agent_or_display(self):
        self.assertEqual(self.config["ssh"], {
            "localPort": 0, "overVsock": True, "loadDotSSHPubKeys": False,
            "forwardAgent": False, "forwardX11": False, "forwardX11Trusted": False,
        })

    def test_no_ambient_proxy_environment_or_automatic_container_install(self):
        self.assertIs(self.config["propagateProxyEnv"], False)
        self.assertEqual(self.config["env"], {})
        self.assertEqual(self.config["containerd"], {"system": False, "user": False})
        self.assertEqual(self.config["provision"], [])
        self.assertIs(self.config["upgradePackages"], False)

    def test_bounds_initial_resources_and_uses_a_guest_only_account(self):
        self.assertEqual((self.config["cpus"], self.config["memory"], self.config["disk"]),
                         (4, "8GiB", "40GiB"))
        self.assertEqual(self.config["user"]["name"], "kelpie")
        self.assertEqual(self.config["user"]["uid"], 1000)
        self.assertEqual(self.config["user"]["home"], "/home/kelpie")
        # This is the trusted lab host administrator, never an untrusted work VM user.
        self.assertIs(self.config["user"]["passwordlessSudo"], True)

    def test_readiness_requires_linux_arm_and_kvm_without_installing_packages(self):
        self.assertEqual(len(self.config["probes"]), 1)
        probe = self.config["probes"][0]
        self.assertEqual(probe["mode"], "readiness")
        self.assertEqual(probe["script"], '#!/bin/sh\nset -eu\n'
                         'test "$(uname -s)" = Linux\n'
                         'test "$(uname -m)" = aarch64\ntest -c /dev/kvm\n')
        result = subprocess.run(["/bin/sh", "-n"], input=probe["script"], text=True,
                                capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
