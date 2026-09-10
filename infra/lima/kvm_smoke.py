"""Boot a diskless, offline ARM64 Linux guest with KVM; not product acceptance.

Run as a non-root trusted lab user with KVM access, a readable ARM64 kernel,
and Ubuntu's qemu-system-arm/busybox-static packages. No sudo or downloads here.
"""

import argparse
import gzip
import hashlib
import json
import os
import platform
import secrets
import socket
import stat
import subprocess
import tempfile
import time
from pathlib import Path


def newc_entry(name, mode, data=b"", device=(0, 0)):
    """Encode one fixed-name initramfs entry without creating privileged nodes."""
    encoded = name.encode("ascii") + b"\0"
    fields = (0, mode, 0, 0, 1, 0, len(data), 0, 0, *device, len(encoded), 0)
    header = b"070701" + "".join(f"{value:08x}" for value in fields).encode("ascii")
    prefix = header + encoded
    return prefix + b"\0" * (-len(prefix) % 4) + data + b"\0" * (-len(data) % 4)


def initramfs(busybox, nonce):
    init = (
        '#!/bin/busybox sh\nset -eu\n'
        '/bin/busybox mount -t devtmpfs devtmpfs /dev\n'
        'test "$(/bin/busybox uname -m)" = aarch64\n'
        f'printf "KELPIE_KVM_BOOT_OK:{nonce}:aarch64\\n"\n'
        '/bin/busybox poweroff -f\n'
    ).encode("ascii")
    entries = [newc_entry(name, stat.S_IFDIR | 0o755) for name in ("bin", "dev")]
    entries.extend((
        newc_entry("dev/console", stat.S_IFCHR | 0o600, device=(5, 1)),
        newc_entry("bin/busybox", stat.S_IFREG | 0o755, busybox),
        newc_entry("init", stat.S_IFREG | 0o755, init),
        newc_entry("TRAILER!!!", 0),
    ))
    return gzip.compress(b"".join(entries), mtime=0)


def qemu_command(kernel, ramdisk, serial, qmp):
    return [
        "/usr/bin/qemu-system-aarch64", "-machine", "virt,accel=kvm,gic-version=host",
        "-cpu", "host", "-smp", "1", "-m", "512M", "-nodefaults",
        "-nic", "none", "-display", "none", "-monitor", "none", "-no-reboot",
        "-kernel", str(kernel), "-initrd", str(ramdisk),
        "-append", "console=ttyAMA0 rdinit=/init panic=-1",
        "-serial", f"file:{serial}", "-qmp", f"unix:{qmp},server=on,wait=off", "-S",
    ]


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError("KVM boot deadline exceeded")
    return value


def qmp_read(stream, connection, deadline):
    connection.settimeout(remaining(deadline))
    line = stream.readline(65537)
    if not line or len(line) > 65536:
        raise ValueError("missing or oversized QMP response")
    message = json.loads(line)
    if not isinstance(message, dict):
        raise TypeError("QMP response must be an object")
    return message


def qmp_execute(stream, connection, deadline, command):
    connection.settimeout(remaining(deadline))
    stream.write(json.dumps({"execute": command, "id": command}).encode() + b"\n")
    stream.flush()
    while True:
        message = qmp_read(stream, connection, deadline)
        if message.get("id") == command:
            if "error" in message or "return" not in message:
                raise ValueError(f"QMP command failed: {command}")
            return message["return"]


def require_kvm(result):
    if result.get("enabled") is not True or result.get("present") is not True:
        raise ValueError("QEMU did not enable KVM; emulation is not acceptance")


def require_boot(returncode, serial, nonce):
    marker = f"KELPIE_KVM_BOOT_OK:{nonce}:aarch64"
    if returncode != 0 or marker not in serial.splitlines() or "Kernel panic" in serial:
        raise ValueError("guest did not boot and power off cleanly; inspect retained logs")


def preflight():
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        raise ValueError("run inside the ARM64 Linux lab, not directly on macOS")
    if os.geteuid() == 0:
        raise ValueError("run QEMU as the non-root trusted lab user")
    if not stat.S_ISCHR(Path("/dev/kvm").stat().st_mode):
        raise ValueError("/dev/kvm is not a character device")
    # Opening, not chmod, confirms this session has permission to use KVM.
    descriptor = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
    os.close(descriptor)


def run(kernel, output, timeout):
    if not 1 <= timeout <= 120:
        raise ValueError("timeout must be between 1 and 120 seconds")
    if "," in str(output):
        raise ValueError("output path must not contain QEMU option separators")
    preflight()
    kernel_bytes = kernel.read_bytes()
    busybox = Path("/usr/bin/busybox").read_bytes()
    output.mkdir(mode=0o700)  # Never overwrite or clean an existing output directory.
    kernel_copy = output / "kernel"
    kernel_copy.write_bytes(kernel_bytes)
    kernel_copy.chmod(0o400)
    nonce = secrets.token_hex(16)
    ramdisk = output / "initramfs.cpio.gz"
    ramdisk.write_bytes(initramfs(busybox, nonce))
    serial = output / "serial.log"
    deadline = time.monotonic() + timeout
    start = time.monotonic()
    # Keep the management socket short and private, regardless of output path length.
    with tempfile.TemporaryDirectory(prefix="kelpie-kvm-") as sockets:
        qmp = Path(sockets) / "qmp.sock"
        command = qemu_command(kernel_copy, ramdisk, serial, qmp)
        with (output / "qemu.log").open("xb") as log:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log,
                                       stderr=subprocess.STDOUT,
                                       env={"PATH": "/usr/bin:/bin", "LANG": "C"})
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    while True:
                        remaining(deadline)
                        if process.poll() is not None:
                            raise ValueError("QEMU exited before QMP; inspect qemu.log")
                        try:
                            connection.connect(str(qmp))
                            break
                        except (FileNotFoundError, ConnectionRefusedError):
                            time.sleep(min(0.02, remaining(deadline)))
                    with connection.makefile("rwb") as stream:
                        if "QMP" not in qmp_read(stream, connection, deadline):
                            raise ValueError("invalid QMP greeting")
                        qmp_execute(stream, connection, deadline, "qmp_capabilities")
                        kvm = qmp_execute(stream, connection, deadline, "query-kvm")
                        require_kvm(kvm)
                        if qmp_execute(stream, connection, deadline, "query-block") != []:
                            raise ValueError("diskless probe unexpectedly has block devices")
                        qmp_execute(stream, connection, deadline, "cont")
                returncode = process.wait(timeout=remaining(deadline))
                require_boot(returncode, serial.read_text(errors="replace"), nonce)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
    receipt = {
        "schema_version": 1, "scope": "diskless-offline-arm64-kvm-smoke",
        "product_acceptance": False, "kvm": kvm, "qemu_exit_code": returncode,
        "host_kernel": platform.release(), "host_arch": platform.machine(),
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "qemu_version": subprocess.run([command[0], "--version"], check=True,
                                       capture_output=True, text=True, timeout=5).stdout.splitlines()[0],
        "sha256": {
            "kernel": hashlib.sha256(kernel_bytes).hexdigest(),
            "busybox": hashlib.sha256(busybox).hexdigest(),
            "probe": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "initramfs": hashlib.sha256(ramdisk.read_bytes()).hexdigest(),
            "serial": hashlib.sha256(serial.read_bytes()).hexdigest(),
        },
    }
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    try:
        run(args.kernel.resolve(), args.output.absolute(), args.timeout)
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        parser.exit(1, f"KVM smoke failed: {error}\n")


if __name__ == "__main__":
    main()
