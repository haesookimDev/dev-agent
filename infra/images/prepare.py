"""Prepare private, digest-locked inputs; never build or promote a VM image."""

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 128 * 1024
MAX_WHEELS = 64
REQUIRED_PACKAGES = frozenset({
    "build-essential", "ca-certificates", "dbus-x11", "git", "nodejs", "npm",
    "python3.12-venv", "qemu-guest-agent", "xfce4", "xorg",
})
FILENAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._+-]{0,199}")
VERSION = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.+:~_-]{0,127}")
PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]{0,99}")
SHA256 = re.compile(r"[a-f0-9]{64}")


class InputError(Exception):
    """Only fixed, non-sensitive messages may cross the CLI boundary."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise InputError(message)


def fields(value: object, expected: set[str]) -> dict:
    require(isinstance(value, dict) and set(value) == expected, "invalid manifest fields")
    return value


def matches(pattern: re.Pattern, value: object) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def pinned_version(value: object) -> bool:
    return matches(VERSION, value) and value.lower() not in {
        "latest", "current", "main", "master", "head", "stable", "development",
    }


def native_machine(architecture: object) -> str:
    require(architecture in ("amd64", "arm64"), "unsupported image architecture")
    return "x86_64" if architecture == "amd64" else "aarch64"


def artifact(value: object, *, suffixes: tuple[str, ...], limit: int, wheel=False) -> dict:
    expected = {"file", "version", "sha256", "size_bytes"}
    if wheel:
        expected.add("name")
    item = fields(value, expected)
    require(matches(FILENAME, item["file"]), "invalid artifact filename")
    require(item["file"].endswith(suffixes), "invalid artifact format")
    require(pinned_version(item["version"]), "artifact version must be pinned")
    require(matches(SHA256, item["sha256"]), "invalid artifact digest")
    size = item["size_bytes"]
    require(type(size) is int and 0 < size <= limit, "invalid artifact size")
    if wheel:
        require(matches(PACKAGE, item["name"]), "invalid wheel package name")
    return item


def validate_manifest(value: object) -> dict:
    manifest = fields(value, {
        "schema_version", "image_version", "architecture", "ubuntu_snapshot",
        "runner_source_commit", "apt_packages", "base_image", "codex", "browser", "runner_wheels",
    })
    require(type(manifest["schema_version"]) is int
            and manifest["schema_version"] == SCHEMA_VERSION, "unsupported manifest version")
    require(pinned_version(manifest["image_version"]), "image version must be pinned")
    native_machine(manifest["architecture"])
    snapshot = manifest["ubuntu_snapshot"]
    require(isinstance(snapshot, str) and re.fullmatch(r"\d{8}T\d{6}Z", snapshot) is not None,
            "invalid Ubuntu snapshot")
    try:
        datetime.strptime(snapshot, "%Y%m%dT%H%M%SZ")
    except ValueError:
        raise InputError("invalid Ubuntu snapshot") from None
    require(matches(re.compile(r"[a-f0-9]{40}"), manifest["runner_source_commit"]),
            "Runner source commit must be pinned")
    packages = manifest["apt_packages"]
    require(isinstance(packages, dict) and len(packages) <= 256, "invalid apt package lock")
    require(REQUIRED_PACKAGES <= packages.keys(), "required image packages are missing")
    for name, version in packages.items():
        require(matches(PACKAGE, name) and pinned_version(version), "invalid apt package lock")
    artifact(manifest["base_image"], suffixes=(".qcow2", ".img"), limit=64 * 1024**3)
    artifact(manifest["codex"], suffixes=("",), limit=1024**3)
    artifact(manifest["browser"], suffixes=(".zip",), limit=2 * 1024**3)
    wheels = manifest["runner_wheels"]
    require(isinstance(wheels, list) and 1 <= len(wheels) <= MAX_WHEELS,
            "invalid Runner wheel lock")
    names = set()
    for wheel in wheels:
        artifact(wheel, suffixes=(".whl",), limit=256 * 1024**2, wheel=True)
        name = re.sub(r"[-_.]+", "-", wheel["name"])
        require(name not in names, "duplicate wheel package")
        names.add(name)
    require("kelpie-vm-runner" in names, "Runner wheel is missing")
    files = [item["file"] for item in artifacts(manifest)]
    # Reject case aliases too: the same bundle may be prepared on macOS and consumed on Linux.
    require(len(files) == len({name.casefold() for name in files}), "duplicate artifact filename")
    return manifest


def artifacts(manifest: dict) -> list[dict]:
    return [manifest["base_image"], manifest["codex"], manifest["browser"],
            *manifest["runner_wheels"]]


def unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate manifest field")
        result[key] = value
    return result


def regular_file(name: str | Path, *, dir_fd: int | None = None) -> int:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise InputError("inputs must be single-link regular files")
    return fd


def read_manifest(path: Path) -> dict:
    with os.fdopen(regular_file(path), "rb") as stream:
        data = stream.read(MAX_MANIFEST_BYTES + 1)
    require(len(data) <= MAX_MANIFEST_BYTES, "manifest size limit exceeded")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise InputError("invalid manifest JSON") from None
    return validate_manifest(value)


def fingerprint(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def copy_verified(item: dict, source_fd: int, destination: Path) -> None:
    digest = hashlib.sha256()
    with os.fdopen(regular_file(item["file"], dir_fd=source_fd), "rb") as source:
        before = os.fstat(source.fileno())
        require(before.st_size == item["size_bytes"], "artifact size mismatch")
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as target:
            remaining = item["size_bytes"]
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                require(bool(chunk), "artifact changed during copy")
                target.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            require(not source.read(1), "artifact changed during copy")
            require(fingerprint(before) == fingerprint(os.fstat(source.fileno())),
                    "artifact changed during copy")
            require(digest.hexdigest() == item["sha256"], "artifact digest mismatch")
            target.flush()
            os.fsync(target.fileno())


def write_json(path: Path, value: dict) -> str:
    data = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(data).hexdigest()


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def prepare(manifest: dict, source: Path, output: Path) -> dict:
    validate_manifest(manifest)
    # A private, operator-selected parent is mandatory; never write into a shared
    # directory or reuse/erase a previous bundle, including an incomplete one.
    parent = output.parent.resolve(strict=True)
    info = parent.stat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "output parent must be owned and mode 0700")
    require(output.name not in {"", ".", ".."}, "invalid output directory")
    output = parent / output.name
    source_fd = os.open(source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        output.mkdir(mode=0o700)  # Exclusive, even against concurrent preparers.
        files = output / "files"
        files.mkdir(mode=0o700)
        for item in artifacts(manifest):
            copy_verified(item, source_fd, files / item["file"])
        digest = write_json(output / "manifest.json", manifest)
        sync_directory(files)
        sync_directory(output)
        result = {
            "schema_version": SCHEMA_VERSION,
            "status": "inputs_verified",
            "release_eligible": False,
            "image_version": manifest["image_version"],
            "manifest_sha256": digest,
            "artifact_count": len(artifacts(manifest)),
        }
        # A consumer must require this complete record and recheck copied digests.
        # Its absence means an incomplete bundle, never a usable/released image.
        write_json(output / "inputs-verified.json", result)
        sync_directory(output)
        sync_directory(parent)
        return result
    finally:
        os.close(source_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(read_manifest(args.manifest), args.assets, args.output)
    except InputError as error:
        print(f"image inputs rejected: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("image inputs rejected: filesystem operation failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
