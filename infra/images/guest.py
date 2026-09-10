"""Install/seal only the dedicated, credential-free Golden Image build guest."""

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from datetime import datetime
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

if __package__:
    from . import codex_package, prepare
else:
    import codex_package
    import prepare

BUILD_PRODUCT = "KelpieGoldenImageBuild"
STAGING = Path("/tmp/kelpie-image")
INSTALL_ROOT = Path("/opt/kelpie")
RUNNER_PYTHON = "/opt/kelpie/runner/bin/python"
BROWSER_ROOT = Path("/opt/kelpie/browser")
GUEST_PACKAGES = prepare.REQUIRED_PACKAGES | {
    "lightdm", "libnss3", "libgbm1", "libasound2t64", "fonts-liberation",
    "x11-utils",
}
MAX_EXPANDED_BROWSER_BYTES = 8 * 1024**3
ARCHITECTURES = {
    "amd64": ("x86_64", 62, "chrome-linux64", "https://archive.ubuntu.com/ubuntu/"),
    "arm64": ("aarch64", 183, "chrome-linux-arm64", "https://snapshot.ubuntu.com/ubuntu/"),
}


def require(condition: bool, message: str) -> None:
    prepare.require(condition, message)


def architecture_values(architecture: str) -> tuple[str, int, str, str]:
    require(isinstance(architecture, str) and architecture in ARCHITECTURES,
            "unsupported guest architecture")
    return ARCHITECTURES[architecture]


def command(*args: str, timeout: int = 900, capture: bool = False) -> str:
    result = subprocess.run(
        args, check=True, timeout=timeout,
        stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        text=True,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8",
             "DEBIAN_FRONTEND": "noninteractive", "PIP_CONFIG_FILE": "/dev/null",
             "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    return result.stdout.strip() if capture else ""


def guard_guest() -> None:
    require(platform.system() == "Linux" and os.geteuid() == 0,
            "installer requires the dedicated Linux build guest")
    require(Path("/sys/class/dmi/id/product_name").read_text().strip() == BUILD_PRODUCT,
            "build guest identity mismatch")
    release = platform.freedesktop_os_release()
    require(release.get("ID") == "ubuntu" and release.get("VERSION_ID") == "24.04",
            "installer requires Ubuntu 24.04")
    machine = platform.machine()
    require(machine in {values[0] for values in ARCHITECTURES.values()},
            "installer requires an amd64 or arm64 guest")
    require(command("systemd-detect-virt", "--vm", timeout=10, capture=True) == "kvm",
            "installer requires a KVM guest")


def write_file(path: Path, content: str, mode: int = 0o644) -> None:
    # Only fixed build-owned destinations are used; never follow a pre-existing symlink.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    with os.fdopen(fd, "w") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
                "guest destination must be a single-link regular file")
        stream.truncate(0)
        os.fchmod(stream.fileno(), mode)
        stream.write(content)


def verify_wheel(path: Path, item: dict) -> None:
    with zipfile.ZipFile(path) as archive:
        metadata = [entry for entry in archive.infolist()
                    if entry.filename.endswith(".dist-info/METADATA")]
        require(len(metadata) == 1 and metadata[0].file_size <= 1024**2,
                "invalid wheel metadata")
        parsed = BytesParser().parsebytes(archive.read(metadata[0]))
    def normalize(name):
        return re.sub(r"[-_.]+", "-", name).lower()

    require(len(parsed.get_all("Name", [])) == 1 and len(parsed.get_all("Version", [])) == 1,
            "invalid wheel metadata")
    require(normalize(parsed["Name"]) == normalize(item["name"])
            and parsed["Version"] == item["version"], "wheel identity differs from lock")


def extract_browser(source: Path, destination: Path, architecture: str = "amd64") -> Path:
    _, elf_machine, archive_root, _ = architecture_values(architecture)
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        require(1 <= len(entries) <= 10_000, "invalid browser archive")
        total = 0
        seen = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            require(not path.is_absolute() and ".." not in path.parts
                    and "\\" not in entry.filename and "\x00" not in entry.orig_filename
                    and path.parts and path.parts[0] == archive_root,
                    "unsafe browser archive path")
            require(stat.S_IFMT(mode) in {0, stat.S_IFREG, stat.S_IFDIR},
                    "browser archive contains a special file")
            require(entry.compress_type in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    and not entry.flag_bits & 1, "unsupported browser archive encoding")
            key = path.as_posix().casefold()
            require(key not in seen, "duplicate browser archive path")
            seen.add(key)
            total += entry.file_size
            require(total <= MAX_EXPANDED_BROWSER_BYTES, "browser expansion limit exceeded")
        destination.mkdir(mode=0o755)
        for entry in entries:
            target = destination / entry.filename
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with archive.open(entry) as source_file, os.fdopen(fd, "wb") as target_file:
                shutil.copyfileobj(source_file, target_file, length=1024**2)
                # No setuid/setgid bits, writable shared files or --no-sandbox workaround.
                mode = (entry.external_attr >> 16) & 0o755
                os.fchmod(target_file.fileno(), mode or 0o644)
    binary = destination / archive_root / "chrome"
    require(binary.is_file() and os.access(binary, os.X_OK), "browser executable is missing")
    with os.fdopen(prepare.regular_file(binary), "rb") as stream:
        header = stream.read(20)
    require(header[:6] == b"\x7fELF\x02\x01"
            and header[18:20] == elf_machine.to_bytes(2, "little"),
            f"browser executable must be {architecture} ELF")
    return binary


def configure_apt(snapshot: str, packages: dict, architecture: str = "amd64") -> None:
    _, _, _, mirror = architecture_values(architecture)
    require(isinstance(snapshot, str)
            and re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", snapshot) is not None,
            "invalid Ubuntu snapshot")
    try:
        datetime.strptime(snapshot, "%Y%m%dT%H%M%SZ")
    except ValueError:
        raise prepare.InputError("invalid Ubuntu snapshot") from None
    require(GUEST_PACKAGES <= packages.keys(), "required guest packages are missing")
    sources_dir = Path("/etc/apt/sources.list.d")
    require(set(sources_dir.glob("*.sources")) <= {sources_dir / "ubuntu.sources"}
            and not list(sources_dir.glob("*.list")), "unexpected APT sources")
    legacy = Path("/etc/apt/sources.list")
    if legacy.exists():
        require(all(not line.strip() or line.lstrip().startswith("#")
                    for line in legacy.read_text().splitlines()), "unexpected APT sources")
    uri = mirror if architecture == "amd64" else f"{mirror}{snapshot}/"
    snapshot_field = f"Snapshot: {snapshot}\n" if architecture == "amd64" else ""
    write_file(sources_dir / "ubuntu.sources", (
        f"Types: deb\nURIs: {uri}\n"
        "Suites: noble noble-updates noble-security\n"
        "Components: main restricted universe multiverse\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
        f"{snapshot_field}"
    ))
    # Keep signature, certificate and metadata-expiry validation enabled.
    command("apt-get", "update")
    command("apt-get", "install", "-y", "--no-install-recommends", "--no-remove",
            *[f"{name}={version}" for name, version in sorted(packages.items())], timeout=1800)
    installed = dict(line.split("\t", 1) for line in command(
        "dpkg-query", "-W", "-f=${Package}\t${Version}\n", capture=True,
    ).splitlines())
    require(all(installed.get(name) == version for name, version in packages.items()),
            "installed APT versions differ from lock")
    prepare.write_json(INSTALL_ROOT / "apt-inventory.json", installed)


def install_codex(item: dict, inputs: Path, root: Path, executable: Path,
                  architecture: str = "amd64") -> None:
    _, elf_machine, _, _ = architecture_values(architecture)
    require(not os.path.lexists(executable), "Codex entrypoint already exists")
    source = inputs / item["file"]
    if item["file"].endswith((".tgz", ".tar.gz")):
        destination = root / "codex"
        records = codex_package.extract(source, destination, item["version"], architecture)
        codex_package.verify_installed(destination, item["version"], records, architecture)
        prepare.write_json(root / "codex-inventory.json", records)
        # Preserve the native package layout, without claiming an npm-managed installation.
        executable.symlink_to(
            destination / codex_package.runtime_prefix(architecture) / "bin/codex")
    else:
        with os.fdopen(prepare.regular_file(source), "rb") as stream:
            header = stream.read(20)
        require(header[:6] == b"\x7fELF\x02\x01"
                and header[18:20] == elf_machine.to_bytes(2, "little"),
                f"Codex must be an {architecture} ELF executable")
        source_fd = os.open(inputs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            prepare.copy_verified(item, source_fd, executable)
        finally:
            os.close(source_fd)
        executable.chmod(0o755)


def reject_credentials() -> None:
    for directory in (Path("/root"), Path("/home/kelpie"), Path("/home/kelpie-builder")):
        require(not (directory / ".codex/auth.json").exists()
                and not (directory / ".aws/credentials").exists()
                and not (directory / ".git-credentials").exists(),
                "build guest contains a forbidden credential cache")
    require(not Path("/run/kelpie/assignment.env").exists(),
            "build guest must not have a work assignment")


def install() -> None:
    guard_guest()
    command("cloud-init", "status", "--wait", timeout=600)
    reject_credentials()
    manifest = prepare.read_manifest(STAGING / "manifest.json")
    architecture = manifest["architecture"]
    require(prepare.native_machine(architecture) == platform.machine(),
            "manifest architecture differs from native guest")
    require(GUEST_PACKAGES <= manifest["apt_packages"].keys(),
            "required guest packages are missing")
    for name in ("/usr/local/bin/codex", "/usr/local/bin/chromium",
                 "/usr/local/bin/kelpie-runner", "/etc/systemd/system/kelpie-runner.service"):
        require(not os.path.lexists(name), "guest installation destination already exists")
    INSTALL_ROOT.mkdir(mode=0o755)  # Never reuse an existing Kelpie installation.
    inputs = INSTALL_ROOT / "image-inputs"
    inputs.mkdir(mode=0o700)
    source_fd = os.open(STAGING / "files", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for item in [manifest["codex"], manifest["browser"], *manifest["runner_wheels"]]:
            prepare.copy_verified(item, source_fd, inputs / item["file"])
    finally:
        os.close(source_fd)
    for item in manifest["runner_wheels"]:
        verify_wheel(inputs / item["file"], item)
    install_codex(manifest["codex"], inputs, INSTALL_ROOT, Path("/usr/local/bin/codex"),
                  architecture)
    configure_apt(manifest["ubuntu_snapshot"], manifest["apt_packages"], architecture)
    command("python3.12", "-m", "venv", "/opt/kelpie/runner")
    requirements = "".join(
        f"{item['name']}=={item['version']} --hash=sha256:{item['sha256']}\n"
        for item in manifest["runner_wheels"]
    )
    write_file(inputs / "requirements.lock", requirements, 0o600)
    command(RUNNER_PYTHON, "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
            "--no-cache-dir", "--require-hashes", "--find-links", str(inputs),
            "-r", str(inputs / "requirements.lock"))
    command(RUNNER_PYTHON, "-m", "pip", "--isolated", "check")
    wheel_versions = json.loads(command(
        RUNNER_PYTHON, "-c", "import importlib.metadata as m,json; "
        "print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()}))",
        capture=True,
    ))
    prepare.write_json(INSTALL_ROOT / "python-inventory.json", wheel_versions)
    require(command("runuser", "-u", "kelpie", "--", "/usr/local/bin/codex", "--version",
                    timeout=30, capture=True) == f"codex-cli {manifest['codex']['version']}",
            "installed Codex version differs from lock")
    browser = extract_browser(inputs / manifest["browser"]["file"], BROWSER_ROOT, architecture)
    browser_version = command("runuser", "-u", "kelpie", "--", str(browser), "--version",
                              timeout=30, capture=True)
    require(browser_version.split()[-1:] == [manifest["browser"]["version"]],
            "installed browser version differs from lock")
    write_file(Path("/usr/local/bin/chromium"),
               f'#!/bin/sh\nexec {browser} "$@"\n', 0o755)
    Path("/usr/local/bin/kelpie-runner").symlink_to("/opt/kelpie/runner/bin/kelpie-runner")
    unit = (STAGING / "tooling/kelpie-runner.service").read_text()
    write_file(Path("/etc/systemd/system/kelpie-runner.service"), unit)
    command("install", "-d", "-o", "kelpie", "-g", "kelpie", "-m", "0700", "/workspace")
    lightdm = Path("/etc/lightdm/lightdm.conf.d")
    lightdm.mkdir(parents=True, exist_ok=True)
    write_file(lightdm / "50-kelpie.conf", (
        "[Seat:*]\nautologin-user=kelpie\nautologin-session=xfce\nuser-session=xfce\n"
        "autologin-user-timeout=0\nallow-guest=false\nxserver-command=X -nolisten tcp\n"
    ))
    write_file(Path("/etc/cloud/cloud.cfg.d/99-kelpie-users.cfg"),
               "users: []\ndisable_root: true\nssh_pwauth: false\nssh_deletekeys: true\n")
    command("systemctl", "daemon-reload")
    command("systemctl", "disable", "kelpie-runner.service")
    command("systemctl", "enable", "lightdm.service", "qemu-guest-agent.service")
    command("systemctl", "set-default", "graphical.target")
    command("systemctl", "mask", "apt-daily.service", "apt-daily-upgrade.service",
            "apt-daily.timer", "apt-daily-upgrade.timer")
    prepare.write_json(INSTALL_ROOT / "image-manifest.json", manifest)
    health_tools = INSTALL_ROOT / "image-health"
    health_tools.mkdir(mode=0o755)
    for name in ("health.py", "prepare.py", "guest.py", "codex_package.py"):
        write_file(health_tools / name, (STAGING / "tooling" / name).read_text())
    reject_credentials()
    # This is build-owned package staging, never a workspace or caller-selected path.
    shutil.rmtree(inputs)


def seal() -> None:
    guard_guest()
    reject_credentials()
    manifest = prepare.read_manifest(INSTALL_ROOT / "image-manifest.json")
    require((INSTALL_ROOT / "apt-inventory.json").is_file()
            and (INSTALL_ROOT / "python-inventory.json").is_file(),
            "guest installation is incomplete")
    command("usermod", "--lock", "--shell", "/usr/sbin/nologin", "kelpie-builder")
    Path("/home/kelpie-builder/.ssh/authorized_keys").unlink(missing_ok=True)
    Path("/etc/sudoers.d/kelpie-image-builder").unlink(missing_ok=True)
    for kind in ("rsa", "ecdsa", "ed25519"):
        for suffix in ("", ".pub"):
            Path(f"/etc/ssh/ssh_host_{kind}_key{suffix}").unlink(missing_ok=True)
    command("cloud-init", "clean", "--logs", "--machine-id", "--seed", timeout=60)
    prepare.write_json(INSTALL_ROOT / "candidate.json", {
        "schema_version": 1, "image_version": manifest["image_version"],
        "status": "sealed_candidate", "release_eligible": False,
        "manifest_sha256": hashlib.sha256(
            (INSTALL_ROOT / "image-manifest.json").read_bytes()).hexdigest(),
    })
    shutil.rmtree(STAGING)
    command("sync", timeout=60)
    command("shutdown", "-P", "now", timeout=30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("install", "seal"))
    args = parser.parse_args(argv)
    try:
        {"install": install, "seal": seal}[args.phase]()
    except prepare.InputError as error:
        print(f"image guest rejected: {error}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError, ValueError, zipfile.BadZipFile):
        print("image guest rejected: installation operation failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
