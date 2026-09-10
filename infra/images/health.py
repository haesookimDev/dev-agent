"""Offline smoke checks inside a dedicated image-probe VM, never release approval."""

import hashlib
import json
import os
import platform
import pwd
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

if __package__:
    from . import codex_package, guest, prepare
else:
    import codex_package
    import guest
    import prepare

PROBE_PRODUCT = "KelpieGoldenImageProbe"
ROOT = Path("/")
CHECKS = ("sealed_identity", "builder_access_removed", "credential_caches_absent",
          "locked_packages", "runner_import", "idle_runner", "desktop_session", "browser_dom")


def require(condition: bool, message: str) -> None:
    prepare.require(condition, message)


def command(*args: str, **kwargs) -> str:
    return guest.command(*args, timeout=kwargs.pop("timeout", 30), capture=True, **kwargs)


def guard() -> None:
    require(platform.system() == "Linux" and os.geteuid() == 0
            and platform.machine() in {"x86_64", "aarch64"},
            "probe requires a Linux amd64 or arm64 guest")
    require((ROOT / "sys/class/dmi/id/product_name").read_text().strip() == PROBE_PRODUCT,
            "probe guest identity mismatch")
    release = platform.freedesktop_os_release()
    require(release.get("ID") == "ubuntu" and release.get("VERSION_ID") == "24.04"
            and command("systemd-detect-virt", "--vm") == "kvm", "unsupported probe guest")


def read_json(path: Path) -> dict:
    with os.fdopen(prepare.regular_file(path), "rb") as stream:
        content = stream.read(prepare.MAX_MANIFEST_BYTES + 1)
    require(len(content) <= prepare.MAX_MANIFEST_BYTES, "probe record too large")
    value = json.loads(content.decode("utf-8"), object_pairs_hook=prepare.unique_object)
    require(isinstance(value, dict), "invalid probe record")
    return value


def identities(manifest: dict) -> dict:
    path = ROOT / "opt/kelpie/image-manifest.json"
    with os.fdopen(prepare.regular_file(path), "rb") as stream:
        digest = hashlib.sha256(stream.read(prepare.MAX_MANIFEST_BYTES + 1)).hexdigest()
    sealed = read_json(ROOT / "opt/kelpie/candidate.json")
    require(sealed == {
        "schema_version": 1, "status": "sealed_candidate", "release_eligible": False,
        "image_version": manifest["image_version"], "manifest_sha256": digest,
    } and type(sealed.get("schema_version")) is int and sealed.get("release_eligible") is False,
        "image sealing record mismatch")
    machine = (ROOT / "etc/machine-id").read_text().strip()
    boot = (ROOT / "proc/sys/kernel/random/boot_id").read_text().strip()
    require(re.fullmatch(r"[a-f0-9]{32}", machine) is not None and machine != "0" * 32,
            "machine identity was not initialized")
    require(re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot) is not None,
            "boot identity is invalid")
    return {"manifest_sha256": digest,
            "machine_id_sha256": hashlib.sha256(machine.encode()).hexdigest(),
            "boot_id_sha256": hashlib.sha256(boot.encode()).hexdigest()}


def access_removed() -> None:
    for name in ("etc/sudoers.d/kelpie-image-builder",
                 "home/kelpie-builder/.ssh/authorized_keys", "run/kelpie/assignment.env"):
        require(not os.path.lexists(ROOT / name), "build access or assignment remains")
    account = pwd.getpwnam("kelpie-builder")
    require(account.pw_shell == "/usr/sbin/nologin", "builder shell is not locked")
    status = command("passwd", "--status", "kelpie-builder").split()
    require(status[:2] == ["kelpie-builder", "L"], "builder password is not locked")
    guest.reject_credentials()  # Known cache paths only; not a general secret scanner.


def installed(manifest: dict) -> None:
    packages = dict(line.split("\t", 1) for line in command(
        "dpkg-query", "-W", "-f=${Package}\t${Version}\n",
    ).splitlines())
    require(all(packages.get(name) == version
                for name, version in manifest["apt_packages"].items()),
            "installed APT versions differ from lock")
    require(packages == read_json(ROOT / "opt/kelpie/apt-inventory.json"),
            "APT inventory changed after sealing")
    versions = json.loads(command(
        guest.RUNNER_PYTHON, "-c", "import importlib.metadata as m,json; "
        "import kelpie_runner.main; "
        "print(json.dumps({d.metadata['Name']:d.version for d in m.distributions()}))",
    ))
    require(isinstance(versions, dict)
            and versions == read_json(ROOT / "opt/kelpie/python-inventory.json"),
            "Runner inventory changed after sealing")
    normalized = {re.sub(r"[-_.]+", "-", name).lower(): version
                  for name, version in versions.items()}
    require(all(normalized.get(re.sub(r"[-_.]+", "-", item["name"])) == item["version"]
                for item in manifest["runner_wheels"]), "installed wheel versions differ from lock")
    command(guest.RUNNER_PYTHON, "-m", "pip", "--isolated", "check")
    if manifest["codex"]["file"].endswith((".tgz", ".tar.gz")):
        codex_root = ROOT / "opt/kelpie/codex"
        codex_package.verify_installed(codex_root, manifest["codex"]["version"],
                                       read_json(ROOT / "opt/kelpie/codex-inventory.json"),
                                       architecture=manifest["architecture"])
        require(os.readlink(ROOT / "usr/local/bin/codex")
                == f"/opt/kelpie/codex/{codex_package.runtime_prefix(manifest['architecture'])}"
                   "bin/codex",
                "Codex entrypoint differs from installed package")
    require(command("runuser", "-u", "kelpie", "--", "/usr/local/bin/codex", "--version")
            == f"codex-cli {manifest['codex']['version']}", "Codex version differs from lock")
    require(command("runuser", "-u", "kelpie", "--", "/usr/local/bin/chromium", "--version")
            .split()[-1:] == [manifest["browser"]["version"]], "browser version differs from lock")


def desktop() -> None:
    for unit in ("qemu-guest-agent.service", "lightdm.service"):
        require(command("systemctl", "is-active", unit) == "active",
                "desktop service is not active")
    require(command("systemctl", "show", "kelpie-runner.service", "-p", "ActiveState", "--value")
            == "inactive", "Runner must remain idle without assignment")
    for unit, state in (("kelpie-runner.service", "disabled"), ("apt-daily.timer", "masked"),
                        ("apt-daily-upgrade.timer", "masked")):
        require(command("systemctl", "show", unit, "-p", "UnitFileState", "--value") == state,
                "image unit policy mismatch")
    require(command("loginctl", "show-user", "kelpie", "-p", "State", "--value") == "active",
            "desktop user session is not active")
    require(command("pgrep", "-u", "kelpie", "-x", "xfce4-session").isdigit(),
            "desktop session is not running")
    display = ROOT / "tmp/.X11-unix/X0"
    require(stat.S_ISSOCK(display.lstat().st_mode), "X display socket is missing")
    command("runuser", "-u", "kelpie", "--", "env", "DISPLAY=:0",
            "XAUTHORITY=/home/kelpie/.Xauthority", "xdpyinfo", "-display", ":0")


def browser() -> None:
    user = pwd.getpwnam("kelpie")
    require(user.pw_uid > 0 and user.pw_gid > 0, "browser user must be unprivileged")
    html = ('<p id="probe">pending</p>'
            '<script>document.getElementById("probe").textContent=2+2</script>')
    with tempfile.TemporaryDirectory(prefix="kelpie-image-smoke-", dir="/tmp") as directory:
        os.chown(directory, user.pw_uid, user.pw_gid)
        output = command(
            "runuser", "-u", "kelpie", "--", "/usr/local/bin/chromium", "--headless=new",
            "--disable-background-networking", "--no-first-run", "--no-default-browser-check",
            f"--user-data-dir={directory}", "--timeout=10000", "--dump-dom",
            "data:text/html," + quote(html),
        )
        require('<p id="probe">4</p>' in output, "browser DOM smoke failed")


def probe() -> dict:
    guard()  # No commands or temporary browser profile on another host/guest.
    command("cloud-init", "status", "--wait", timeout=120)
    manifest = prepare.read_manifest(ROOT / "opt/kelpie/image-manifest.json")
    require(platform.machine() == prepare.native_machine(manifest["architecture"]),
            "image architecture differs from probe guest")
    identity = identities(manifest)
    access_removed()
    installed(manifest)
    desktop()
    browser()
    return {"schema_version": 1, "status": "guest_smoke_passed", "release_eligible": False,
            "image_version": manifest["image_version"], **identity,
            "checks": dict.fromkeys(CHECKS, True)}


def main() -> int:
    try:
        result = probe()
    except (prepare.InputError, OSError, ValueError, KeyError, RecursionError,
            subprocess.SubprocessError):
        # Never return package output, environment values or raw exceptions through QGA.
        print("image smoke rejected", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
