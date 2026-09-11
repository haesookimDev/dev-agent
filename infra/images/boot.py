"""Boot a verified candidate twice on dedicated KVM; never approve or roll out images."""

import argparse
import base64
import json
import os
import re
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

if __package__:
    from . import build, health, prepare, qga
else:
    import build
    import health
    import prepare
    import qga

BOOT_TIMEOUT = 300
SHUTDOWN_TIMEOUT = 60


def require(condition: bool, message: str) -> None:
    prepare.require(condition, message)


def private_directory(path: Path) -> None:
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "probe directory must be owned and mode 0700")


def stage(source: Path, output: Path) -> tuple[dict, dict]:
    private_directory(source)
    receipt = build.read_json(source / "candidate.json")
    prepare.fields(receipt, {"schema_version", "status", "release_eligible", "image_version",
                             "image", "recipe_sha256"})
    require(type(receipt["schema_version"]) is int and receipt["schema_version"] == 1
            and receipt["status"] == "image_built_unverified"
            and receipt["release_eligible"] is False,
            "candidate is not a completed build")
    image = prepare.fields(receipt["image"], {"file", "size_bytes", "sha256"})
    require(image["file"] == "kelpie.qcow2" and type(image["size_bytes"]) is int
            and 0 < image["size_bytes"] <= 64 * 1024**3
            and prepare.matches(prepare.SHA256, image["sha256"]), "invalid candidate image record")
    require(build.measure(source / "recipe.json")["sha256"] == receipt["recipe_sha256"],
            "candidate recipe hash mismatch")
    recipe = build.read_json(source / "recipe.json")
    manifest = prepare.read_manifest(source / "inputs/manifest.json")
    manifest_digest = build.measure(source / "inputs/manifest.json")["sha256"]
    require(recipe.get("manifest_sha256") == manifest_digest
            and receipt["image_version"] == manifest["image_version"],
            "candidate manifest mismatch")
    firmware = build.firmware_records(manifest["architecture"])
    require(recipe.get("firmware", {}) == firmware, "candidate firmware differs from pin")
    build.verify_firmware(source / "firmware", firmware)
    private_directory(output.parent)
    require(re.fullmatch(r"/[a-zA-Z0-9/_.-]+", str(output)) is not None
            and output.name not in {"", ".", ".."} and len(str(output)) <= 80,
            "probe path must be a short absolute safe path")
    output.mkdir(mode=0o700)
    (output / "tmp").mkdir(mode=0o700)
    build.copy_firmware(firmware, source / "firmware", output / "firmware")
    source_fd = os.open(source / "image", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        prepare.copy_verified(image, source_fd, output / "candidate.qcow2")
    finally:
        os.close(source_fd)
    prepare.write_json(output / "source-candidate.json", receipt)
    prepare.write_json(output / "source-recipe.json", recipe)
    prepare.write_json(output / "manifest.json", manifest)
    return receipt, manifest


def write_text(path: Path, content: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def prepare_vm(output: Path) -> None:
    build.inspect_disk(output / "candidate.qcow2", output)
    build.command(["qemu-img", "create", "-f", "qcow2", "-F", "qcow2", "-b",
                   str(output / "candidate.qcow2"), str(output / "probe.qcow2")],
                  output, timeout=30)
    seed = output / "seed"
    seed.mkdir(mode=0o700)
    write_text(seed / "user-data", "#cloud-config\n" + json.dumps({
        "users": [], "disable_root": True, "ssh_pwauth": False,
        "package_update": False, "package_upgrade": False,
    }))
    prepare.write_json(seed / "meta-data", {
        "instance-id": "kelpie-probe-" + str(uuid.uuid4()), "local-hostname": "kelpie-image-probe",
    })
    prepare.write_json(seed / "network-config", {"version": 2, "ethernets": {}})
    build.command(["xorriso", "-as", "mkisofs", "-quiet", "-V", "cidata", "-J", "-r",
                   "-o", str(output / "seed.iso"), str(seed / "user-data"),
                   str(seed / "meta-data"), str(seed / "network-config")], output, timeout=30)


def qemu_arguments(output: Path, socket_path: Path, architecture: str = "amd64") -> list[str]:
    machine = prepare.native_machine(architecture)
    arm = architecture == "arm64"
    args = [
        f"qemu-system-{machine}", "-machine",
        "virt,accel=kvm,gic-version=host" if arm else "q35,accel=kvm", "-cpu", "host",
        "-m", "4096", "-smp", "2", "-display", "none",
        "-serial", "none", "-monitor", "none", "-nic", "none",
        # ARM systemd uses DMI to identify KVM; accel=kvm above enforces the real backend.
        "-no-reboot", "-smbios", f"type=1,manufacturer=KVM,product={health.PROBE_PRODUCT}",
        "-drive", f"file={output / 'probe.qcow2'},format=qcow2,if=virtio,cache=none",
        "-device", "virtio-serial", "-chardev",
        f"socket,path={socket_path},server=on,wait=off,id=qga0",
        "-device", "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0",
    ]
    if arm:
        args += [
            "-device", "virtio-gpu-pci", "-device", "virtio-scsi-pci,id=scsi0",
            "-drive", f"file={output / 'seed.iso'},format=raw,if=none,id=seed,readonly=on",
            "-device", "scsi-cd,drive=seed,bus=scsi0.0",
            "-drive", f"file={output / 'firmware/AAVMF_CODE.no-secboot.fd'},"
                      "format=raw,if=pflash,unit=0,readonly=on",
            "-drive", f"file={output / 'firmware/AAVMF_VARS.fd'},format=raw,if=pflash,unit=1",
        ]
    else:
        args += ["-vga", "std", "-parallel", "none", "-drive",
                 f"file={output / 'seed.iso'},format=raw,media=cdrom,readonly=on"]
    return args


def wait_agent(path: Path, process, deadline: float) -> qga.GuestAgent:
    while time.monotonic() < deadline:
        require(process.poll() is None, "probe VM exited before guest readiness")
        try:
            return qga.GuestAgent(path, deadline, peer_pid=process.pid)
        except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
            time.sleep(0.25)
    raise prepare.InputError("probe VM boot timed out")


def validate_report(value: object, manifest: dict, manifest_sha256: str) -> dict:
    report = prepare.fields(value, {
        "schema_version", "status", "release_eligible", "image_version",
        "manifest_sha256", "machine_id_sha256", "boot_id_sha256", "checks",
    })
    require(type(report["schema_version"]) is int and report["schema_version"] == 1
            and report["status"] == "guest_smoke_passed" and report["release_eligible"] is False
            and report["image_version"] == manifest["image_version"]
            and report["manifest_sha256"] == manifest_sha256, "guest smoke identity mismatch")
    require(prepare.matches(prepare.SHA256, report["machine_id_sha256"])
            and prepare.matches(prepare.SHA256, report["boot_id_sha256"]), "invalid boot identity")
    checks = prepare.fields(report["checks"], set(health.CHECKS))
    require(all(value is True for value in checks.values()), "guest smoke checks are incomplete")
    return report


def check_guest(agent: qga.GuestAgent, process, manifest: dict,
                digest: str, deadline: float) -> dict:
    # Do not enable disabled RPCs or change guest policy to make a check pass.
    info = agent.call("guest-info")
    require(isinstance(info, dict) and isinstance(info.get("supported_commands"), list),
            "invalid guest agent capabilities")
    enabled = {item.get("name") for item in info["supported_commands"]
               if isinstance(item, dict) and isinstance(item.get("name"), str)
               and item.get("enabled") is True}
    require({"guest-exec", "guest-exec-status", "guest-shutdown"} <= enabled,
            "required guest agent commands are disabled")
    while time.monotonic() < deadline:
        require(process.poll() is None, "probe VM exited during smoke checks")
        started = agent.call("guest-exec", {
            "path": "/usr/bin/env", "arg": ["-i", f"PATH={build.TOOL_PATH}", "LANG=C.UTF-8",
                "/bin/sh", "-c", 'exec 2>/dev/null; exec "$@"', "kelpie-image-smoke",
                "/usr/bin/python3", "/opt/kelpie/image-health/health.py"],
            # QGA 8.2 stdout-only capture can report exited=false forever. The
            # fixed wrapper closes stderr before the probe; no data enters that pipe.
            "capture-output": True,
        })
        require(isinstance(started, dict) and type(started.get("pid")) is int
                and started["pid"] > 0, "invalid guest probe process")
        while time.monotonic() < deadline:
            require(process.poll() is None, "probe VM exited during smoke checks")
            status = agent.call("guest-exec-status", {"pid": started["pid"]})
            require(isinstance(status, dict) and type(status.get("exited")) is bool,
                    "invalid guest probe status")
            if status["exited"]:
                require(status.get("err-data", "") == ""
                        and status.get("err-truncated", False) is False,
                        "unexpected guest probe diagnostic output")
                if type(status.get("exitcode")) is int and status["exitcode"] == 0:
                    require("signal" not in status and status.get("out-truncated", False) is False
                            and isinstance(status.get("out-data"), str)
                            and len(status["out-data"]) <= 16 * 1024, "invalid guest probe output")
                    decoded = base64.b64decode(status["out-data"], validate=True)
                    value = json.loads(decoded.decode("utf-8"),
                                       object_pairs_hook=prepare.unique_object)
                    return validate_report(value, manifest, digest)
                break  # Readiness can lag QGA startup; retry within the original deadline only.
            time.sleep(0.25)
        time.sleep(1)
    raise prepare.InputError("guest smoke checks timed out")


def boot_once(output: Path, number: int, manifest: dict, digest: str) -> dict:
    run = output / f"boot-{number}"
    run.mkdir(mode=0o700)
    socket_path = run / "qga.sock"
    with subprocess.Popen(qemu_arguments(output, socket_path, manifest["architecture"]), cwd=output,
                          env=build.environment(output), stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=True) as process:
        try:
            deadline = time.monotonic() + BOOT_TIMEOUT
            with wait_agent(socket_path, process, deadline) as agent:
                report = check_guest(agent, process, manifest, digest, deadline)
                agent.send("guest-shutdown", {"mode": "powerdown"})
            # QGA acknowledges no successful shutdown response. Only a clean VM
            # process exit proves this smoke run stopped; a timeout is failure.
            require(process.wait(timeout=SHUTDOWN_TIMEOUT) == 0, "probe VM shutdown failed")
            return report
        finally:
            build.stop_group(process)


def probe(source: Path, output: Path, *, approved=False) -> dict:
    build.guard_host(approved)
    build.require_native(prepare.read_manifest(source / "inputs/manifest.json")["architecture"])
    output = output.parent.resolve(strict=True) / output.name
    receipt, manifest = stage(source, output)
    build.require_native(manifest["architecture"])
    prepare_vm(output)
    digest = build.measure(output / "manifest.json")["sha256"]
    reports = [boot_once(output, index, manifest, digest) for index in range(2)]
    require(reports[0]["machine_id_sha256"] == reports[1]["machine_id_sha256"]
            and reports[0]["boot_id_sha256"] != reports[1]["boot_id_sha256"],
            "image did not preserve machine identity across distinct boots")
    image = build.measure(output / "candidate.qcow2")
    require(image["sha256"] == receipt["image"]["sha256"]
            and image["size_bytes"] == receipt["image"]["size_bytes"],
            "candidate changed during boot")
    tooling = ("boot.py", "qga.py", "health.py", "build.py", "prepare.py",
               "guest.py", "codex_package.py", "browser_probe.py")
    result = {
        "schema_version": 1, "status": "boot_smoke_passed_unreleased", "release_eligible": False,
        "image_version": manifest["image_version"], "image": receipt["image"], "boots": reports,
        "source_recipe_sha256": receipt["recipe_sha256"],
        "probe_tooling_sha256": {name: build.measure(Path(__file__).parent / name)["sha256"]
                                  for name in tooling},
    }
    prepare.write_json(output / "boot-smoke.json", result)
    prepare.sync_directory(output)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute-on-dedicated-host", action="store_true")
    args = parser.parse_args(argv)
    try:
        previous_umask = os.umask(0o077)
        try:
            result = probe(args.build_dir, args.output, approved=args.execute_on_dedicated_host)
        finally:
            os.umask(previous_umask)
    except (prepare.InputError, OSError, ValueError, KeyError, RecursionError,
            subprocess.SubprocessError):
        print("image boot rejected; candidate remains unreleased", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
