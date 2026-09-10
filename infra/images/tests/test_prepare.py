import copy
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from infra.images import prepare

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/images/prepare.py"


class PrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kelpie-image-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.output = self.root / "bundle"
        self.manifest_path = self.root / "lock.json"
        # Deliberately synthetic bytes, NOT bootable images or installable packages.
        def item(name, version, package=None):
            content = (f"synthetic test artifact: {name}\n").encode()
            (self.assets / name).write_bytes(content)
            result = {"file": name, "version": version, "size_bytes": len(content),
                      "sha256": hashlib.sha256(content).hexdigest()}
            if package:
                result["name"] = package
            return result

        self.manifest = {
            "schema_version": 1,
            "image_version": "ubuntu-24.04-amd64-fixture.1",
            "architecture": "amd64",
            "ubuntu_snapshot": "20260901T000000Z",
            "runner_source_commit": "a" * 40,
            "apt_packages": dict.fromkeys(prepare.REQUIRED_PACKAGES, "1.0-fixture"),
            "base_image": item("ubuntu.qcow2", "24.04-fixture"),
            "codex": item("codex", "0.0.0-fixture"),
            "browser": item("chrome-linux64.zip", "0.0.0-fixture"),
            "runner_wheels": [
                item("kelpie_vm_runner-0.1.0-py3-none-any.whl", "0.1.0", "kelpie-vm-runner"),
                item("httpx-0.28.1-py3-none-any.whl", "0.28.1", "httpx"),
            ],
        }

    def invoke(self, **kwargs):
        self.manifest_path.write_text(json.dumps(self.manifest))
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--manifest", str(self.manifest_path),
             "--assets", str(self.assets), "--output", str(self.output)],
            cwd=ROOT, capture_output=True, text=True, timeout=10, **kwargs,
        )

    def assert_incomplete(self):
        self.assertFalse((self.output / "inputs-verified.json").exists())

    def test_actual_cli_copies_digest_locked_inputs_without_promoting(self):
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        record = json.loads(result.stdout)
        self.assertEqual(record["status"], "inputs_verified")
        self.assertIs(record["release_eligible"], False)
        self.assertEqual(record["artifact_count"], 5)
        self.assertEqual(json.loads((self.output / "inputs-verified.json").read_text()), record)
        saved = (self.output / "manifest.json").read_bytes()
        self.assertEqual(json.loads(saved), self.manifest)
        self.assertEqual(hashlib.sha256(saved).hexdigest(), record["manifest_sha256"])
        for item in prepare.artifacts(self.manifest):
            source = self.assets / item["file"]
            target = self.output / "files" / item["file"]
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertNotEqual(source.stat().st_ino, target.stat().st_ino)
            source.write_bytes(b"changed after successful preparation")
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), item["sha256"])
        for path in self.output.rglob("*"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)

    def test_preparation_is_deterministic(self):
        first = prepare.prepare(self.manifest, self.assets, self.output)
        second = self.root / "second"
        self.assertEqual(prepare.prepare(self.manifest, self.assets, second), first)
        for path in self.output.rglob("*"):
            if path.is_file():
                other = second / path.relative_to(self.output)
                self.assertEqual(path.read_bytes(), other.read_bytes())

    def test_arm64_manifest_uses_the_same_strict_nonrelease_input_contract(self):
        self.manifest["architecture"] = "arm64"
        result = self.invoke()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)["release_eligible"], False)
        self.assertEqual(prepare.read_manifest(self.output / "manifest.json")["architecture"],
                         "arm64")

    def test_native_machine_has_only_explicit_supported_architectures(self):
        self.assertEqual(prepare.native_machine("amd64"), "x86_64")
        self.assertEqual(prepare.native_machine("arm64"), "aarch64")
        for value in ("x86_64", "aarch64", "riscv64", "ARM64", None, [], {}, True):
            with self.subTest(value=value), self.assertRaises(prepare.InputError):
                prepare.native_machine(value)

    def test_rejects_invalid_contract_before_creating_output(self):
        mutations = [
            lambda m: m.update(schema_version=True),
            lambda m: m.update(schema_version=2),
            lambda m: m.update(architecture="riscv64"),
            lambda m: m.update(image_version="latest"),
            lambda m: m.update(image_version="$(touch escaped)"),
            lambda m: m.update(ubuntu_snapshot="20260230T000000Z"),
            lambda m: m.update(ubuntu_snapshot="20260901"),
            lambda m: m.update(runner_source_commit="main"),
            lambda m: m.update(token="not-allowed"),
            lambda m: m["base_image"].update(sha256="0"),
            lambda m: m["base_image"].update(size_bytes=True),
            lambda m: m["base_image"].update(size_bytes=0),
            lambda m: m["base_image"].update(size_bytes=65 * 1024**3),
            lambda m: m["codex"].update(version="stable"),
            lambda m: m["codex"].update(file="../codex"),
            lambda m: m["codex"].update(file="/codex"),
            lambda m: m["codex"].update(file="co\\dex"),
            lambda m: m["codex"].update(file="CHROME-linux64.zip"),
            lambda m: m["browser"].update(file="chrome.tar.gz"),
            lambda m: m["apt_packages"].pop("qemu-guest-agent"),
            lambda m: m["apt_packages"].update(git="*"),
            lambda m: m.update(runner_wheels=[]),
            lambda m: m["runner_wheels"].pop(0),
            lambda m: m["runner_wheels"].append(copy.deepcopy(m["runner_wheels"][0])),
            lambda m: m["runner_wheels"][1].update(name="kelpie.vm.runner"),
            lambda m: m["runner_wheels"][0].update(file="runner.tar.gz"),
        ]
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index):
                manifest = copy.deepcopy(self.manifest)
                mutation(manifest)
                with self.assertRaises(prepare.InputError):
                    prepare.prepare(manifest, self.assets, self.output)
                self.assertFalse(self.output.exists())

    def test_rejects_duplicate_json_fields(self):
        for body in ('{"schema_version":1,"schema_version":1}',
                     '{"nested":{"sha256":"a","sha256":"b"}}'):
            with self.subTest(body=body):
                self.manifest_path.write_text(body)
                with self.assertRaisesRegex(prepare.InputError, "duplicate manifest field"):
                    prepare.read_manifest(self.manifest_path)

    def test_rejects_malformed_or_oversized_manifest(self):
        for body in (b"not json", b"\xff", b"[" * 2000, b" " * (prepare.MAX_MANIFEST_BYTES + 1)):
            with self.subTest(size=len(body)):
                self.manifest_path.write_bytes(body)
                with self.assertRaises(prepare.InputError):
                    prepare.read_manifest(self.manifest_path)

    def test_manifest_requires_utf8_without_implicit_utf16_or_utf32(self):
        for encoding in ("utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                self.manifest_path.write_bytes(json.dumps(self.manifest).encode(encoding))
                with self.assertRaisesRegex(prepare.InputError, "invalid manifest JSON"):
                    prepare.read_manifest(self.manifest_path)

    def test_rejects_tampering_even_when_size_matches(self):
        self.manifest["browser"]["sha256"] = "f" * 64
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "image inputs rejected: artifact digest mismatch\n")
        self.assert_incomplete()
        self.assertTrue(self.output.is_dir())  # Kept private; not silently erased or reused.

    def test_rejects_size_mismatch(self):
        self.manifest["base_image"]["size_bytes"] += 1
        with self.assertRaisesRegex(prepare.InputError, "artifact size mismatch"):
            prepare.prepare(self.manifest, self.assets, self.output)
        self.assert_incomplete()

    def test_missing_asset_has_no_raw_path_in_diagnostics(self):
        self.manifest["codex"]["file"] = "private-test-marker"
        result = self.invoke()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "image inputs rejected: filesystem operation failed\n")
        self.assert_incomplete()

    def test_rejects_symlinks_hardlinks_and_nonregular_assets(self):
        source = self.assets / "ubuntu.qcow2"
        saved = source.read_bytes()
        target = self.root / "outside"
        target.write_bytes(saved)
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                case_assets = self.root / kind
                case_assets.mkdir()
                linked = case_assets / source.name
                if kind == "symlink":
                    linked.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, linked)
                elif kind == "fifo":
                    os.mkfifo(linked)
                else:
                    linked.mkdir()
                with self.assertRaises((OSError, prepare.InputError)):
                    prepare.prepare(self.manifest, case_assets, self.root / f"out-{kind}")
                self.assertFalse((self.root / f"out-{kind}" / "inputs-verified.json").exists())
                self.assertEqual(target.read_bytes(), saved)

    def test_rejects_symlinked_manifest_and_asset_root(self):
        real = self.root / "real.json"
        real.write_text(json.dumps(self.manifest))
        self.manifest_path.symlink_to(real)
        with self.assertRaises(OSError):
            prepare.read_manifest(self.manifest_path)
        linked = self.root / "linked-assets"
        linked.symlink_to(self.assets, target_is_directory=True)
        with self.assertRaises(OSError):
            prepare.prepare(self.manifest, linked, self.output)
        self.assertFalse(self.output.exists())

    def test_existing_output_is_never_changed(self):
        for kind in ("directory", "file", "symlink"):
            with self.subTest(kind=kind):
                existing = self.root / kind
                sentinel = self.root / f"{kind}-sentinel"
                sentinel.write_bytes(b"preserve user data")
                if kind == "directory":
                    existing.mkdir()
                elif kind == "file":
                    existing.write_bytes(b"preserve user data")
                else:
                    existing.symlink_to(sentinel)
                with self.assertRaises(FileExistsError):
                    prepare.prepare(self.manifest, self.assets, existing)
                self.assertEqual(sentinel.read_bytes(), b"preserve user data")
                if kind == "directory":
                    self.assertEqual(list(existing.iterdir()), [])
                else:
                    self.assertEqual(existing.read_bytes(), b"preserve user data")

    def test_requires_private_owned_parent(self):
        self.root.chmod(0o755)
        self.addCleanup(self.root.chmod, 0o700)
        with self.assertRaisesRegex(prepare.InputError, "output parent must be owned"):
            prepare.prepare(self.manifest, self.assets, self.output)
        self.assertFalse(self.output.exists())

    def test_mutation_during_copy_is_rejected(self):
        original = prepare.fingerprint
        calls = 0

        def altered(info):
            nonlocal calls
            calls += 1
            result = original(info)
            return (*result[:-1], 2) if calls == 2 else result

        with patch.object(prepare, "fingerprint", side_effect=altered):
            with self.assertRaisesRegex(prepare.InputError, "artifact changed during copy"):
                prepare.prepare(self.manifest, self.assets, self.output)
        self.assert_incomplete()

    def test_write_failure_does_not_publish_success_marker(self):
        with patch.object(prepare.os, "fsync", side_effect=OSError("private-test-marker")):
            with self.assertRaises(OSError):
                prepare.prepare(self.manifest, self.assets, self.output)
        self.assert_incomplete()

    def test_concurrent_preparers_do_not_overwrite_each_other(self):
        def run():
            try:
                return prepare.prepare(self.manifest, self.assets, self.output)
            except FileExistsError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(json.loads((self.output / "inputs-verified.json").read_text())
                         ["status"], "inputs_verified")

    def test_real_source_rewrite_during_copy_is_rejected(self):
        source = self.assets / self.manifest["base_image"]["file"]
        content = source.read_bytes()
        digest = hashlib.sha256()

        class MutatingDigest:
            def update(self, chunk):
                digest.update(chunk)
                source.write_bytes(b"x" * len(content))

            def hexdigest(self):
                return digest.hexdigest()

        # The read bytes still match the expected hash. Only checking the digest
        # would miss this real, same-sized source-file rewrite during copying.
        with patch.object(prepare.hashlib, "sha256", return_value=MutatingDigest()):
            with self.assertRaisesRegex(prepare.InputError, "artifact changed during copy"):
                prepare.prepare(self.manifest, self.assets, self.output)
        self.assertEqual(source.read_bytes(), b"x" * len(content))
        self.assert_incomplete()

    def test_actual_cli_refuses_to_reuse_partial_bundle(self):
        expected = self.manifest["browser"]["sha256"]
        self.manifest["browser"]["sha256"] = "f" * 64
        first = self.invoke()
        self.assertEqual(first.returncode, 1)
        files = {path.name: path.read_bytes() for path in (self.output / "files").iterdir()}
        self.manifest["browser"]["sha256"] = expected
        second = self.invoke()
        self.assertEqual(second.returncode, 1)
        self.assertEqual(second.stdout, "")
        self.assert_incomplete()
        self.assertEqual(files, {
            path.name: path.read_bytes() for path in (self.output / "files").iterdir()
        })

    def test_large_input_streams_across_multiple_chunks(self):
        content = b"synthetic-image-chunk" * 100_000
        base = self.manifest["base_image"]
        (self.assets / base["file"]).write_bytes(content)
        base.update(size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest())
        prepare.prepare(self.manifest, self.assets, self.output)
        copied = (self.output / "files" / base["file"]).read_bytes()
        self.assertEqual(copied, content)

    def test_extra_assets_are_not_copied_or_listed(self):
        marker = "synthetic-private-unlisted-file"
        (self.assets / marker).write_text("not part of the approved manifest")
        result = self.invoke()
        self.assertEqual(result.returncode, 0)
        self.assertNotIn(marker, result.stdout + result.stderr)
        self.assertFalse((self.output / "files" / marker).exists())


if __name__ == "__main__":
    unittest.main()
