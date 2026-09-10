import copy
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infra.images import codex_package as package
from infra.images import prepare


class CodexPackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="kelpie-codex-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = self.root / "out"
        self.version = "0.154.0"
        elf = b"\x7fELF\x02\x01" + b"\x00" * 12 + b"\x3e\x00" + b"synthetic, never executable"
        self.files = dict.fromkeys(package.BINARIES, elf)
        self.files["package.json"] = json.dumps({
            "name": "@openai/codex", "version": self.version + "-linux-x64",
            "os": ["linux"], "cpu": ["x64"], "license": "Apache-2.0",
        }).encode()
        self.files[package.PREFIX + "codex-package.json"] = json.dumps({
            "layoutVersion": 1, "version": self.version, "target": package.TARGET,
            "variant": "codex", "entrypoint": "bin/codex",
            "resourcesDir": "codex-resources", "pathDir": "codex-path",
        }).encode()

    def archive(self, changes=None, extra=(), mode=None):
        path = self.root / "package.tgz"
        files = self.files | (changes or {})
        with tarfile.open(path, "w:gz") as archive:
            for name, data in files.items():
                if data is None:
                    continue
                info = tarfile.TarInfo("package/" + name)
                info.size = len(data)
                info.mode = (mode if mode is not None
                             else (0o755 if name in package.BINARIES else 0o644))
                archive.addfile(info, io.BytesIO(data))
            for info, data in extra:
                archive.addfile(info, io.BytesIO(data))
        return path

    def test_keeps_every_runtime_resource_and_verifies_installed_inventory(self):
        records = package.extract(self.archive(mode=0o6755), self.destination, self.version)
        self.assertEqual(set(records), set(self.files))
        for name, data in self.files.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
            self.assertEqual(stat.S_IMODE((self.destination / name).stat().st_mode), 0o755)
        package.verify_installed(self.destination, self.version, records)

    def test_missing_helper_wrong_version_architecture_and_non_elf_fail_before_writes(self):
        name = package.PREFIX + "codex-package.json"
        metadata = json.loads(self.files[name])
        for change in (
            {package.PREFIX + "bin/codex-code-mode-host": None},
            {package.PREFIX + "codex-resources/bwrap": b"not ELF"},
            {name: json.dumps(metadata | {"version": "0"}).encode()},
            {name: json.dumps(metadata | {"target": "arm64"}).encode()},
            {name: json.dumps(metadata | {"layoutVersion": True}).encode()},
            {"package.json": self.files["package.json"].replace(b'"x64"', b'"arm64"')},
        ):
            with self.subTest(change=list(change)), self.assertRaises(prepare.InputError):
                package.extract(self.archive(change), self.destination, self.version)
            self.assertFalse(self.destination.exists())

    def test_links_special_files_escapes_aliases_and_ancestor_conflicts_are_rejected(self):
        for name, kind in (
            ("/outside", tarfile.REGTYPE), ("package/../outside", tarfile.REGTYPE),
            ("package/vendor/other/bin/codex", tarfile.REGTYPE),
            ("package/" + package.PREFIX + "bin/codex", tarfile.REGTYPE),
            ("package/" + package.PREFIX + "bin/Codex", tarfile.REGTYPE),
            ("package/" + package.PREFIX + "bin", tarfile.REGTYPE),
            *[("package/" + package.PREFIX + "link", kind) for kind in
              (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE)],
        ):
            entry = tarfile.TarInfo(name)
            entry.type, entry.linkname = kind, "/outside"
            entry.size = 1 if kind == tarfile.REGTYPE else 0
            with self.subTest(name=name, kind=kind), self.assertRaises(prepare.InputError):
                package.extract(self.archive(extra=[(entry, b"x")]), self.destination, self.version)
            self.assertFalse(self.destination.exists())

    def test_limits_expansion_count_and_metadata_before_extraction(self):
        source = self.archive()
        for constant, limit in (("MAX_BYTES", 10), ("MAX_FILES", 2)):
            with patch.object(package, constant, limit), self.assertRaises(prepare.InputError):
                package.extract(source, self.destination, self.version)
            self.assertFalse(self.destination.exists())
        source = self.archive({"package.json": b" " * (64 * 1024 + 1)})
        with self.assertRaises(prepare.InputError):
            package.extract(source, self.destination, self.version)
        self.assertFalse(self.destination.exists())

    def test_duplicate_metadata_keys_and_truncated_tar_are_rejected(self):
        source = self.archive({"package.json": b'{"name":"a","name":"b"}'})
        with self.assertRaises(prepare.InputError):
            package.extract(source, self.destination, self.version)
        source = self.archive()
        source.write_bytes(source.read_bytes()[:50])
        with self.assertRaises(prepare.InputError):
            package.extract(source, self.destination, self.version)
        self.assertFalse(self.destination.exists())

    def test_existing_output_is_never_reused(self):
        self.destination.mkdir()
        marker = self.destination / "keep"
        marker.write_text("preserve")
        with self.assertRaises(FileExistsError):
            package.extract(self.archive(), self.destination, self.version)
        self.assertEqual(marker.read_text(), "preserve")

    def test_installed_mutation_missing_extra_symlink_and_mode_are_detected(self):
        source = self.archive()
        for index, change in enumerate(("content", "missing", "extra", "symlink", "mode")):
            destination = self.root / f"out-{index}"
            records = package.extract(source, destination, self.version)
            target = destination / package.PREFIX / "bin/codex-code-mode-host"
            if change == "content":
                target.write_bytes(b"x" * target.stat().st_size)
            elif change == "missing":
                target.unlink()
            elif change == "extra":
                (destination / "extra").write_text("unregistered")
            elif change == "symlink":
                target.unlink()
                target.symlink_to("/outside")
            else:
                target.chmod(0o777)
            with self.subTest(change=change), self.assertRaises(prepare.InputError):
                package.verify_installed(destination, self.version, records)

    def test_inventory_cannot_redirect_reads_to_an_outside_file(self):
        records = package.extract(self.archive(), self.destination, self.version)
        record = next(iter(records.values()))
        altered = copy.deepcopy(records) | {"../../outside": record}
        with self.assertRaises(prepare.InputError):
            package.verify_installed(self.destination, self.version, altered)
        link = self.root / "link"
        link.symlink_to(self.destination, target_is_directory=True)
        with self.assertRaises(prepare.InputError):
            package.verify_installed(link, self.version, records)
        binary = self.destination / package.PREFIX / "bin/codex"
        os.link(binary, self.root / "hardlink")
        with self.assertRaises(prepare.InputError):
            package.verify_installed(self.destination, self.version, records)


if __name__ == "__main__":
    unittest.main()
