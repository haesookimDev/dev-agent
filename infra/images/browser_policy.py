"""Inventory Chromium and install its exact AppArmor user-namespace grant."""

import hashlib
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath

if __package__:
    from . import prepare
else:
    import prepare

ROOT = Path("/")
ROOT_UID = 0
ROOT_GID = 0
MAX_ENTRIES = 10_000
MAX_BYTES = 8 * 1024**3
SCHEMA_VERSION = 1
PROFILE_NAME = "kelpie-image-browser"
POLICY_RELATIVE = Path("etc/apparmor.d/kelpie-image-browser")
_READ_LIMIT = 4 * 1024**2
_COMMAND_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
_ARCHITECTURES = {
    "amd64": ("chrome-linux64", 62),
    "arm64": ("chrome-linux-arm64", 183),
}


def require(condition: bool, message: str) -> None:
    prepare.require(condition, message)


def _architecture(architecture: str) -> tuple[str, int]:
    require(type(architecture) is str and architecture in _ARCHITECTURES,
            "unsupported browser architecture")
    return _ARCHITECTURES[architecture]


def _browser_relative(architecture: str) -> str:
    package, _ = _architecture(architecture)
    return f"{package}/chrome"


def _browser_logical_path(architecture: str) -> str:
    return f"/opt/kelpie/browser/{_browser_relative(architecture)}"


def _policy_content(architecture: str) -> bytes:
    return (
        "abi <abi/4.0>,\n"
        f"profile {PROFILE_NAME} {_browser_logical_path(architecture)} "
        "flags=(unconfined) {\n"
        "  userns,\n"
        "}\n"
    ).encode("ascii")


def _fingerprint(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_nlink,
    )


def _same_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, stat.S_IFMT(left.st_mode)) == (
        right.st_dev,
        right.st_ino,
        stat.S_IFMT(right.st_mode),
    )


def _safe_mode(info: os.stat_result) -> int:
    mode = stat.S_IMODE(info.st_mode)
    require(not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022),
            "browser installation has unsafe permissions")
    return mode


def _root_owned(info: os.stat_result, message: str) -> None:
    require(info.st_uid == ROOT_UID and info.st_gid == ROOT_GID, message)


def _directory_record(info: os.stat_result) -> dict:
    require(stat.S_ISDIR(info.st_mode), "browser installation contains a non-directory")
    _root_owned(info, "browser installation must be root owned")
    return {
        "kind": "directory",
        "mode": _safe_mode(info),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


def _validate_fixed_directories(parts: tuple[str, ...], message: str) -> None:
    paths = [ROOT]
    current = ROOT
    for part in parts:
        current /= part
        paths.append(current)
    for path in paths:
        try:
            info = path.lstat()
        except OSError:
            raise prepare.InputError(message) from None
        require(stat.S_ISDIR(info.st_mode), message)
        _root_owned(info, message)
        mode = stat.S_IMODE(info.st_mode)
        require(not mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | 0o022), message)


def _open_directory(name: str | Path, *, dir_fd: int | None = None) -> int:
    try:
        return os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=dir_fd,
        )
    except OSError:
        raise prepare.InputError("browser installation contains an unsafe directory") from None


def _scan_browser(browser_root: Path, executable: str) -> tuple[dict, bytes | None]:
    root_fd = _open_directory(browser_root)
    try:
        root_info = os.fstat(root_fd)
        entries = {".": _directory_record(root_info)}
        state = {"count": 1, "bytes": 0, "executable_header": None}

        def scan(directory_fd: int, prefix: str) -> None:
            directory_before = os.fstat(directory_fd)
            try:
                iterator = os.scandir(directory_fd)
                with iterator:
                    for entry in iterator:
                        state["count"] += 1
                        require(state["count"] <= MAX_ENTRIES,
                                "browser installation entry limit exceeded")
                        name = f"{prefix}/{entry.name}" if prefix else entry.name
                        try:
                            observed = os.stat(entry.name, dir_fd=directory_fd,
                                               follow_symlinks=False)
                        except OSError:
                            raise prepare.InputError(
                                "browser installation changed during inventory"
                            ) from None
                        if stat.S_ISDIR(observed.st_mode):
                            child_fd = _open_directory(entry.name, dir_fd=directory_fd)
                            try:
                                opened = os.fstat(child_fd)
                                require(_same_object(observed, opened),
                                        "browser installation changed during inventory")
                                entries[name] = _directory_record(opened)
                                scan(child_fd, name)
                            finally:
                                os.close(child_fd)
                            continue
                        require(stat.S_ISREG(observed.st_mode),
                                "browser installation contains a special file")
                        try:
                            file_fd = os.open(
                                entry.name,
                                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                dir_fd=directory_fd,
                            )
                        except OSError:
                            raise prepare.InputError(
                                "browser installation contains an unsafe file"
                            ) from None
                        try:
                            opened = os.fstat(file_fd)
                            require(_same_object(observed, opened)
                                    and stat.S_ISREG(opened.st_mode)
                                    and opened.st_nlink == 1,
                                    "browser files must be single-link regular files")
                            _root_owned(opened, "browser installation must be root owned")
                            mode = _safe_mode(opened)
                            require(opened.st_size >= 0, "invalid browser file size")
                            state["bytes"] += opened.st_size
                            require(state["bytes"] <= MAX_BYTES,
                                    "browser installation size limit exceeded")
                            digest = hashlib.sha256()
                            header = bytearray()
                            remaining = opened.st_size
                            while remaining:
                                chunk = os.read(file_fd, min(1024**2, remaining))
                                require(bool(chunk),
                                        "browser file changed during inventory")
                                remaining -= len(chunk)
                                digest.update(chunk)
                                if name == executable and len(header) < 20:
                                    header.extend(chunk[:20 - len(header)])
                            require(not os.read(file_fd, 1)
                                    and _fingerprint(os.fstat(file_fd)) == _fingerprint(opened),
                                    "browser file changed during inventory")
                            entries[name] = {
                                "kind": "file",
                                "sha256": digest.hexdigest(),
                                "size_bytes": opened.st_size,
                                "mode": mode,
                                "uid": opened.st_uid,
                                "gid": opened.st_gid,
                            }
                            if name == executable:
                                state["executable_header"] = bytes(header)
                        finally:
                            os.close(file_fd)
            except OSError:
                raise prepare.InputError(
                    "browser installation changed during inventory"
                ) from None
            require(_fingerprint(os.fstat(directory_fd)) == _fingerprint(directory_before),
                    "browser installation changed during inventory")

        scan(root_fd, "")
        require(_fingerprint(os.fstat(root_fd)) == _fingerprint(root_info),
                "browser installation changed during inventory")
        return entries, state["executable_header"]
    except OSError:
        raise prepare.InputError("browser installation changed during inventory") from None
    except RecursionError:
        raise prepare.InputError("browser installation nesting limit exceeded") from None
    finally:
        try:
            os.close(root_fd)
        except OSError:
            pass


def inventory(architecture: str) -> dict:
    """Return the complete, root-owned inventory for the fixed native browser tree."""
    _, elf_machine = _architecture(architecture)
    _validate_fixed_directories(("opt", "kelpie", "browser"),
                                "browser directory is not root-owned and immutable")
    executable = _browser_relative(architecture)
    entries, header = _scan_browser(ROOT / "opt/kelpie/browser", executable)
    executable_record = entries.get(executable)
    require(isinstance(executable_record, dict)
            and executable_record.get("kind") == "file"
            and executable_record.get("mode", 0) & 0o111,
            "browser executable is missing or not executable")
    require(header is not None and header[:6] == b"\x7fELF\x02\x01"
            and header[18:20] == elf_machine.to_bytes(2, "little"),
            f"browser executable must be {architecture} ELF")
    return {
        "schema_version": SCHEMA_VERSION,
        "architecture": architecture,
        "browser_path": _browser_logical_path(architecture),
        "entries": entries,
    }


def _validate_inventory_schema(architecture: str, records: dict) -> None:
    require(isinstance(records, dict)
            and set(records) == {"schema_version", "architecture", "browser_path", "entries"},
            "invalid browser inventory fields")
    require(type(records["schema_version"]) is int
            and records["schema_version"] == SCHEMA_VERSION,
            "invalid browser inventory version")
    require(type(records["architecture"]) is str
            and records["architecture"] == architecture,
            "invalid browser inventory architecture")
    require(type(records["browser_path"]) is str
            and records["browser_path"] == _browser_logical_path(architecture),
            "invalid browser inventory executable")
    entries = records["entries"]
    require(isinstance(entries, dict) and 1 <= len(entries) <= MAX_ENTRIES,
            "invalid browser inventory entries")
    total = 0
    for name, record in entries.items():
        path = PurePosixPath(name) if isinstance(name, str) else None
        require(name == "." or (path is not None and not path.is_absolute()
                                and path.as_posix() == name and ".." not in path.parts
                                and "." not in path.parts),
                "invalid browser inventory path")
        require(isinstance(record, dict) and type(record.get("kind")) is str,
                "invalid browser inventory record")
        common = {"kind", "mode", "uid", "gid"}
        kind = record["kind"]
        require(kind in {"directory", "file"}, "invalid browser inventory record")
        expected = common if kind == "directory" else common | {"sha256", "size_bytes"}
        require(set(record) == expected
                and type(record["mode"]) is int and 0 <= record["mode"] <= 0o7777
                and type(record["uid"]) is int and record["uid"] >= 0
                and type(record["gid"]) is int and record["gid"] >= 0,
                "invalid browser inventory record")
        if kind == "file":
            require(type(record["size_bytes"]) is int and record["size_bytes"] >= 0
                    and type(record["sha256"]) is str
                    and re.fullmatch(r"[a-f0-9]{64}", record["sha256"]) is not None,
                    "invalid browser inventory file record")
            total += record["size_bytes"]
            require(total <= MAX_BYTES, "invalid browser inventory size")


def verify_inventory(architecture: str, records: dict) -> None:
    """Reject any schema or installed-tree difference from a sealed inventory."""
    _architecture(architecture)
    _validate_inventory_schema(architecture, records)
    require(inventory(architecture) == records, "browser installation differs from inventory")


def _read_fixed(path: Path, limit: int, message: str) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode), message)
            value = stream.read(limit + 1)
            require(len(value) <= limit, message)
            return value
    except OSError:
        raise prepare.InputError(message) from None


def _command(args: list[str]) -> int:
    try:
        result = subprocess.run(
            args,
            timeout=10,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_COMMAND_ENV,
        )
    except (OSError, subprocess.SubprocessError):
        raise prepare.InputError("AppArmor command failed") from None
    return result.returncode


def _loaded_profiles() -> list[str]:
    value = _read_fixed(
        ROOT / "sys/kernel/security/apparmor/profiles",
        _READ_LIMIT,
        "AppArmor loaded profiles are unavailable",
    )
    try:
        return value.decode("utf-8").splitlines()
    except UnicodeError:
        raise prepare.InputError("AppArmor loaded profiles are malformed") from None


def _profile_lines(lines: list[str]) -> list[str]:
    prefix = f"{PROFILE_NAME} ("
    return [line for line in lines if line == PROFILE_NAME or line.startswith(prefix)]


def _verify_runtime() -> None:
    require(_read_fixed(
        ROOT / "sys/module/apparmor/parameters/enabled",
        16,
        "AppArmor module state is unavailable",
    ) == b"Y\n", "AppArmor module is not enabled")
    require(_read_fixed(
        ROOT / "proc/sys/kernel/apparmor_restrict_unprivileged_userns",
        16,
        "AppArmor user namespace restriction is unavailable",
    ) == b"1\n", "AppArmor user namespace restriction is not enabled")
    require(_command([
        "/usr/bin/systemctl", "is-active", "--quiet", "apparmor.service",
    ]) == 0, "AppArmor service is not active")


def _verify_policy_ancestors() -> None:
    _validate_fixed_directories(("etc", "apparmor.d"),
                                "AppArmor policy directory is unsafe")


def _override_paths() -> tuple[Path, Path]:
    base = ROOT / "etc/apparmor.d"
    return (
        base / "disable" / PROFILE_NAME,
        base / "force-complain" / PROFILE_NAME,
    )


def _reject_overrides() -> None:
    require(all(not os.path.lexists(path) for path in _override_paths()),
            "AppArmor policy override exists")


def install_policy(architecture: str) -> None:
    """Exclusively create and add the exact native-browser userns policy."""
    _architecture(architecture)
    inventory(architecture)
    require(os.geteuid() == ROOT_UID, "AppArmor policy installation requires root")
    _verify_policy_ancestors()
    policy_path = ROOT / POLICY_RELATIVE
    require(not os.path.lexists(policy_path), "AppArmor browser policy already exists")
    _reject_overrides()
    _verify_runtime()
    require(not _profile_lines(_loaded_profiles()),
            "AppArmor browser profile name is already loaded")
    content = _policy_content(architecture)
    try:
        fd = os.open(
            policy_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
        )
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            info = os.fstat(stream.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1
                    and info.st_uid == ROOT_UID and info.st_gid == ROOT_GID,
                    "AppArmor policy destination is unsafe")
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        raise prepare.InputError("AppArmor browser policy already exists") from None
    except OSError:
        raise prepare.InputError("AppArmor policy creation failed") from None
    require(_command([
        "/usr/sbin/apparmor_parser", "--add", str(policy_path),
    ]) == 0, "AppArmor parser rejected browser policy")
    require(_profile_lines(_loaded_profiles()) == [f"{PROFILE_NAME} (unconfined)"],
            "AppArmor browser profile was not loaded as unconfined")


def verify_policy(architecture: str) -> None:
    """Verify the fixed policy file, override absence, and effective runtime state."""
    _architecture(architecture)
    _verify_policy_ancestors()
    _reject_overrides()
    policy_path = ROOT / POLICY_RELATIVE
    try:
        fd = os.open(policy_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                    and before.st_uid == ROOT_UID and before.st_gid == ROOT_GID
                    and stat.S_IMODE(before.st_mode) == 0o644,
                    "AppArmor browser policy file is unsafe")
            content = stream.read(len(_policy_content(architecture)) + 1)
            require(content == _policy_content(architecture)
                    and _fingerprint(os.fstat(stream.fileno())) == _fingerprint(before),
                    "AppArmor browser policy differs from the exact policy")
    except OSError:
        raise prepare.InputError("AppArmor browser policy file is unavailable") from None
    _verify_runtime()
    require(_profile_lines(_loaded_profiles()) == [f"{PROFILE_NAME} (unconfined)"],
            "AppArmor browser profile is not loaded as unconfined")
