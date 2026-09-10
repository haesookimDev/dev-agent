"""Build an unverified image candidate on an explicitly approved, dedicated KVM host."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
from pathlib import Path

if __package__:
    from . import guest, prepare
else:
    import guest
    import prepare

PACKER_VERSION = "1.16.0"
TOOL_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ROOT = Path(__file__).resolve().parents[2]
MAX_DISK_BYTES = 40 * 1024**3


def require(condition: bool, message: str) -> None:
    prepare.require(condition, message)


def guard_host(approved: bool) -> None:
    require(approved, "explicit dedicated build host acknowledgement is required")
    require(platform.system() == "Linux" and platform.machine() == "x86_64"
            and os.geteuid() != 0, "builder requires a non-root Linux amd64 host")
    require(Path("/dev/kvm").is_char_device() and os.access("/dev/kvm", os.R_OK | os.W_OK),
            "builder requires accessible KVM; emulation fallback is forbidden")
    for tool in ("qemu-system-x86_64", "qemu-img", "ssh-keygen", "xorriso"):
        require(shutil.which(tool, path=TOOL_PATH) is not None, "required host tool is missing")


def read_json(path: Path) -> dict:
    with os.fdopen(prepare.regular_file(path), "rb") as stream:
        data = stream.read(prepare.MAX_MANIFEST_BYTES + 1)
    require(len(data) <= prepare.MAX_MANIFEST_BYTES, "build record size limit exceeded")
    value = json.loads(data.decode("utf-8"), object_pairs_hook=prepare.unique_object)
    require(isinstance(value, dict), "invalid build record")
    return value


def measure(path: Path) -> dict:
    digest = hashlib.sha256()
    with os.fdopen(prepare.regular_file(path), "rb") as stream:
        before = os.fstat(stream.fileno())
        require(0 < before.st_size <= 64 * 1024**3, "invalid build artifact size")
        remaining = before.st_size
        while remaining:
            chunk = stream.read(min(1024**2, remaining))
            require(bool(chunk), "build artifact changed while hashing")
            digest.update(chunk)
            remaining -= len(chunk)
        require(not stream.read(1)
                and prepare.fingerprint(before) == prepare.fingerprint(os.fstat(stream.fileno())),
                "build artifact changed while hashing")
    return {"file": path.name, "size_bytes": before.st_size, "sha256": digest.hexdigest()}


def stage(bundle: Path, output: Path) -> dict:
    # Trusted operator owns all ancestors and excludes concurrent same-user writers.
    require(not bundle.is_symlink(), "bundle root must not be a symlink")
    manifest = prepare.read_manifest(bundle / "manifest.json")
    expected = {
        "schema_version": 1, "status": "inputs_verified", "release_eligible": False,
        "image_version": manifest["image_version"],
        "manifest_sha256": measure(bundle / "manifest.json")["sha256"],
        "artifact_count": len(prepare.artifacts(manifest)),
    }
    record = read_json(bundle / "inputs-verified.json")
    require(record == expected and type(record.get("schema_version")) is int
            and type(record.get("artifact_count")) is int
            and record.get("release_eligible") is False,
            "bundle completion record does not match manifest")
    require(guest.GUEST_PACKAGES <= manifest["apt_packages"].keys(),
            "required guest packages are missing")
    parent = output.parent.resolve(strict=True)
    info = parent.stat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "build parent must be owned and mode 0700")
    # QEMU socket paths and command interpolation must not contain delimiters.
    output = parent / output.name
    require(re.fullmatch(r"/[a-zA-Z0-9/_.-]+", str(output)) is not None
            and output.name not in {"", ".", ".."} and len(str(output)) <= 80,
            "build path must be a short absolute safe path")
    output.mkdir(mode=0o700)  # Exclusive; never retry in a partial or existing run.
    receipt = prepare.prepare(manifest, bundle / "files", output / "inputs")
    tooling = output / "tooling"
    tooling.mkdir(mode=0o700)
    recipes = [ROOT / "infra/images" / name for name in
               ("build.py", "guest.py", "prepare.py", "ubuntu.pkr.hcl")]
    recipes.append(ROOT / "infra/systemd/kelpie-runner.service")
    hashes = {}
    for source in recipes:
        item = measure(source)
        fd = os.open(source.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            prepare.copy_verified(item, fd, tooling / source.name)
        finally:
            os.close(fd)
        hashes[source.name] = item["sha256"]
    prepare.write_json(output / "recipe.json", {
        "schema_version": 1, "packer_version": PACKER_VERSION, "qemu_plugin_version": "1.1.6",
        "tooling_sha256": hashes, "manifest_sha256": receipt["manifest_sha256"],
        "release_eligible": False,
    })
    for name in ("cache", "config", "plugins", "tmp"):
        (output / name).mkdir(mode=0o700)
    prepare.write_json(output / "config/packer.json", {})
    prepare.write_json(output / "variables.pkrvars.json", {"run_dir": str(output)})
    return manifest


def environment(output: Path) -> dict:
    # No ambient SCM, API, cloud, SSH agent or Packer debug credentials/config.
    return {
        "PATH": TOOL_PATH, "LANG": "C.UTF-8", "CHECKPOINT_DISABLE": "1",
        "PACKER_CONFIG": str(output / "config/packer.json"),
        "PACKER_CONFIG_DIR": str(output / "config"),
        "PACKER_CACHE_DIR": str(output / "cache"), "PACKER_PLUGIN_PATH": str(output / "plugins"),
        "TMPDIR": str(output / "tmp"),
    }


def stop_group(process: subprocess.Popen) -> None:
    # Only the fresh process session created below; never kill by name or host-wide PID search.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait(timeout=10)


def command(args: list[str], output: Path, *, timeout: int, capture=False) -> str:
    # Only capture commands that return metadata; never echo raw diagnostics.
    path = output / "command-output"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w+b") as log:
            with subprocess.Popen(args, cwd=output, env=environment(output),
                                  stdin=subprocess.DEVNULL,
                                  stdout=log if capture else subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL,
                                  start_new_session=True) as process:
                try:
                    code = process.wait(timeout=timeout)
                except BaseException:
                    stop_group(process)
                    raise
                stop_group(process)
                require(code == 0, "build tool failed; candidate is not approved")
            if capture:
                log.seek(0)
                data = log.read(prepare.MAX_MANIFEST_BYTES + 1)
                require(len(data) <= prepare.MAX_MANIFEST_BYTES, "tool output limit exceeded")
                return data.decode("utf-8").strip()
        return ""
    finally:
        path.unlink(missing_ok=True)  # Exact, exclusively created scratch file.


def inspect_disk(path: Path, output: Path) -> dict:
    info = json.loads(command(["qemu-img", "info", "--output=json", "-f", "qcow2", str(path)],
                              output, timeout=30, capture=True))
    require(isinstance(info, dict), "invalid disk metadata")
    specific = info.get("format-specific", {})
    require(isinstance(specific, dict) and isinstance(specific.get("data", {}), dict),
            "invalid disk metadata")
    extra = specific.get("data", {})
    require(isinstance(info, dict) and info.get("format") == "qcow2"
            and type(info.get("virtual-size")) is int
            and 0 < info["virtual-size"] <= MAX_DISK_BYTES
            and not info.get("backing-filename") and not info.get("encrypted")
            and "data-file" not in extra and "encrypt" not in extra,
            "image must be a standalone unencrypted qcow2 within disk budget")
    return info


def build(bundle: Path, output: Path, packer: Path, *, approved=False) -> dict:
    guard_host(approved)  # Before staging, subprocesses or VM creation.
    require(packer.is_absolute() and packer.is_file() and os.access(packer, os.X_OK),
            "an operator-verified absolute Packer executable is required")
    output = output.parent.resolve(strict=True) / output.name
    manifest = stage(bundle, output)
    key = output / "build_key"
    try:
        version = command([str(packer), "version"], output, timeout=10, capture=True)
        require(version == f"Packer v{PACKER_VERSION}", "Packer version differs from pin")
        inspect_disk(output / "inputs/files" / manifest["base_image"]["file"], output)
        command(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "kelpie-image-build",
                 "-f", str(key)], output, timeout=30)
        template = str(output / "tooling/ubuntu.pkr.hcl")
        variables = f"-var-file={output / 'variables.pkrvars.json'}"
        command([str(packer), "init", template], output, timeout=180)
        command([str(packer), "validate", variables, template], output, timeout=60)
        command([str(packer), "build", "-on-error=cleanup", variables, template],
                output, timeout=3600)
        candidate = output / "image/kelpie.qcow2"
        inspect_disk(candidate, output)
        result = {
            "schema_version": 1, "status": "image_built_unverified", "release_eligible": False,
            "image_version": manifest["image_version"], "image": measure(candidate),
            "recipe_sha256": measure(output / "recipe.json")["sha256"],
        }
        prepare.write_json(output / "candidate.json", result)
        prepare.sync_directory(output)
        return result
    finally:
        key.unlink(missing_ok=True)
        (output / "build_key.pub").unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--packer", type=Path, required=True)
    parser.add_argument("--execute-on-dedicated-host", action="store_true")
    args = parser.parse_args(argv)
    try:
        # All child-created state remains private, including Packer's temporary CD and disk.
        previous_umask = os.umask(0o077)
        try:
            result = build(args.bundle, args.output, args.packer,
                           approved=args.execute_on_dedicated_host)
        finally:
            os.umask(previous_umask)
    except prepare.InputError as error:
        print(f"image build rejected: {error}", file=sys.stderr)
        return 1
    except (OSError, ValueError, subprocess.SubprocessError, RecursionError):
        print("image build rejected: build operation failed", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
