"""Verify/extract a pinned Linux Codex platform package without executing its contents."""

import gzip
import hashlib
import json
import os
import re
import stat
import tarfile
from pathlib import Path, PurePosixPath

if __package__:
    from . import prepare
else:
    import prepare

_ARCHITECTURES = {
    "amd64": ("x86_64-unknown-linux-musl", "x64", 62),
    "arm64": ("aarch64-unknown-linux-musl", "arm64", 183),
}
_RUNTIME_FILES = {
    "bin/codex", "bin/codex-code-mode-host", "codex-path/rg",
    "codex-resources/bwrap", "codex-resources/zsh/bin/zsh",
}
MAX_BYTES = 512 * 1024**2
MAX_FILES = 100


def require(condition: bool, message: str) -> None:
    prepare.require(condition, message)


def _architecture_values(architecture: str) -> tuple[str, str, int]:
    require(isinstance(architecture, str) and architecture in _ARCHITECTURES,
            "unsupported Codex architecture")
    return _ARCHITECTURES[architecture]


def runtime_prefix(architecture: str = "amd64") -> str:
    target, _, _ = _architecture_values(architecture)
    return f"vendor/{target}/"


TARGET = _architecture_values("amd64")[0]
PREFIX = runtime_prefix()
BINARIES = {PREFIX + name for name in _RUNTIME_FILES}
METADATA = {"package.json", PREFIX + "codex-package.json"}


def valid_path(name: str, architecture: str = "amd64") -> bool:
    prefix = runtime_prefix(architecture)
    path = PurePosixPath(name)
    return (re.fullmatch(r"[a-zA-Z0-9_./+-]{1,240}", name) is not None
            and not path.is_absolute() and ".." not in path.parts
            and path.as_posix() == name
            and (name.startswith(prefix) or name in {"package.json", "README.md", "LICENSE"}))


class BoundedReader:
    def __init__(self, stream):
        self.stream = stream
        self.remaining = MAX_BYTES + 1024**2  # Include tar headers/padding in the bound.

    def read(self, size: int) -> bytes:
        require(0 <= size <= 1024**2, "invalid Codex archive read")
        value = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(value)
        require(self.remaining >= 0, "Codex archive expansion limit exceeded")
        return value


def identity(metadata: dict[str, bytes], version: str, architecture: str = "amd64") -> None:
    target, npm_architecture, _ = _architecture_values(architecture)
    prefix = runtime_prefix(architecture)
    metadata_names = {"package.json", prefix + "codex-package.json"}
    values = {}
    for name in metadata_names:
        value = json.loads(metadata[name].decode("utf-8"), object_pairs_hook=prepare.unique_object)
        require(isinstance(value, dict), "invalid Codex package metadata")
        values[name] = value
    platform = values[prefix + "codex-package.json"]
    require(platform == {
        "layoutVersion": 1, "version": version, "target": target, "variant": "codex",
        "entrypoint": "bin/codex", "resourcesDir": "codex-resources", "pathDir": "codex-path",
    } and type(platform.get("layoutVersion")) is int, "Codex runtime layout differs from lock")
    package = values["package.json"]
    require(package.get("name") == "@openai/codex"
            and package.get("version") == f"{version}-linux-{npm_architecture}"
            and package.get("os") == ["linux"] and package.get("cpu") == [npm_architecture],
            "Codex platform package differs from lock")


def scan(stream, version: str, destination: Path | None = None, expected=None,
         architecture: str = "amd64") -> dict:
    _, _, elf_machine = _architecture_values(architecture)
    prefix = runtime_prefix(architecture)
    binaries = {prefix + name for name in _RUNTIME_FILES}
    metadata_names = {"package.json", prefix + "codex-package.json"}
    records, metadata, seen = {}, {}, set()
    total = 0
    with gzip.GzipFile(fileobj=stream) as compressed, \
            tarfile.open(fileobj=BoundedReader(compressed), mode="r|") as archive:
        for member in archive:
            require(member.name.startswith("package/"), "invalid Codex package root")
            name = member.name.removeprefix("package/")
            require(valid_path(name, architecture) and member.isfile() and not member.issparse(),
                    "unsafe Codex archive member")
            require(name.casefold() not in seen, "duplicate Codex archive path")
            seen.add(name.casefold())
            require(len(seen) <= MAX_FILES, "too many Codex package files")
            total += member.size
            require(0 < member.size <= MAX_BYTES and total <= MAX_BYTES,
                    "Codex package size limit exceeded")
            require(name not in metadata_names or member.size <= 64 * 1024,
                    "Codex metadata size limit exceeded")
            mode = 0o755 if member.mode & 0o111 else 0o644
            require(name not in binaries or mode == 0o755, "Codex runtime is not executable")
            digest = hashlib.sha256()
            data = bytearray()
            target = None
            try:
                if destination is not None:
                    require(name in expected, "Codex archive changed during extraction")
                    path = destination / name
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                    target = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                              | os.O_NOFOLLOW, 0o600), "wb")
                with archive.extractfile(member) as source:
                    remaining = member.size
                    while remaining:
                        chunk = source.read(min(1024**2, remaining))
                        require(bool(chunk), "truncated Codex package file")
                        if remaining == member.size and name in binaries:
                            require(chunk[:6] == b"\x7fELF\x02\x01"
                                    and chunk[18:20] == elf_machine.to_bytes(2, "little"),
                                    f"Codex runtime must be {architecture} ELF")
                        remaining -= len(chunk)
                        digest.update(chunk)
                        if name in metadata_names:
                            data.extend(chunk)
                        if target is not None:
                            target.write(chunk)
                record = {"size_bytes": member.size, "sha256": digest.hexdigest(), "mode": mode}
                if target is not None:
                    require(record == expected[name], "Codex archive changed during extraction")
                    target.flush()
                    os.fsync(target.fileno())
                    os.fchmod(target.fileno(), mode)
                records[name] = record
                if name in metadata_names:
                    metadata[name] = bytes(data)
            finally:
                if target is not None:
                    target.close()
    require(binaries | metadata_names <= records.keys(), "Codex runtime package is incomplete")
    require(all(parent.as_posix() not in records for name in records
                for parent in PurePosixPath(name).parents), "conflicting Codex archive paths")
    identity(metadata, version, architecture)
    return records


def extract(source: Path, destination: Path, version: str, architecture: str = "amd64") -> dict:
    _architecture_values(architecture)
    try:
        with os.fdopen(prepare.regular_file(source), "rb") as stream:
            before = prepare.fingerprint(os.fstat(stream.fileno()))
            records = scan(stream, version, architecture=architecture)
            destination.mkdir(mode=0o755)  # Exclusive; no reuse of partial or existing output.
            stream.seek(0)
            require(scan(stream, version, destination, records, architecture) == records
                    and prepare.fingerprint(os.fstat(stream.fileno())) == before,
                    "Codex archive changed during extraction")
        return records
    except (tarfile.TarError, EOFError, ValueError, KeyError, RecursionError):
        raise prepare.InputError("invalid Codex platform archive") from None


def verify_installed(root: Path, version: str, records: dict,
                     architecture: str = "amd64") -> None:
    _architecture_values(architecture)
    prefix = runtime_prefix(architecture)
    binaries = {prefix + name for name in _RUNTIME_FILES}
    metadata_names = {"package.json", prefix + "codex-package.json"}
    require(isinstance(records, dict) and 1 <= len(records) <= MAX_FILES
            and binaries | metadata_names <= records.keys(), "invalid Codex installed inventory")
    require(stat.S_ISDIR(root.lstat().st_mode), "invalid Codex installation root")
    files, metadata = set(), {}
    for path in root.rglob("*"):
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode),
                "invalid Codex installed file type")
        if stat.S_ISREG(info.st_mode):
            files.add(path.relative_to(root).as_posix())
    require(files == set(records), "Codex installed file set changed")
    total = 0
    for name, record in records.items():
        require(valid_path(name, architecture), "invalid Codex installed path")
        prepare.fields(record, {"size_bytes", "sha256", "mode"})
        require(type(record["size_bytes"]) is int and 0 < record["size_bytes"] <= MAX_BYTES
                and prepare.matches(prepare.SHA256, record["sha256"])
                and type(record["mode"]) is int and record["mode"] in {0o644, 0o755},
                "invalid Codex installed record")
        total += record["size_bytes"]
        require(total <= MAX_BYTES, "Codex installed size limit exceeded")
        digest = hashlib.sha256()
        data = bytearray()
        with os.fdopen(prepare.regular_file(root / name), "rb") as stream:
            info = os.fstat(stream.fileno())
            require(info.st_size == record["size_bytes"]
                    and stat.S_IMODE(info.st_mode) == record["mode"],
                    "Codex installed file changed")
            if name in metadata_names:
                require(info.st_size <= 64 * 1024, "Codex metadata size limit exceeded")
            remaining = info.st_size
            while remaining:
                chunk = stream.read(min(1024**2, remaining))
                require(bool(chunk), "Codex installed file changed")
                remaining -= len(chunk)
                digest.update(chunk)
                if name in metadata_names:
                    data.extend(chunk)
            require(not stream.read(1) and digest.hexdigest() == record["sha256"]
                    and prepare.fingerprint(os.fstat(stream.fileno())) == prepare.fingerprint(info),
                    "Codex installed file changed")
            if name in metadata_names:
                metadata[name] = bytes(data)
    identity(metadata, version, architecture)
