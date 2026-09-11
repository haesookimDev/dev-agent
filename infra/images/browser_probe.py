"""Bounded offline Chromium DOM probe over private CDP file descriptors."""

import fcntl
import json
import os
import platform
import pwd
import re
import select
import stat
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

PROBE_PRODUCT = "KelpieGoldenImageProbe"
DMI_PRODUCT_NAME = Path("/sys/class/dmi/id/product_name")
BROWSER = Path("/usr/local/bin/chromium")
CHILD_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "C.UTF-8",
}
FAILURE_MESSAGE = "browser probe rejected"
TOTAL_TIMEOUT_SECONDS = 24.0
MAX_FRAME_BYTES = 1024 * 1024
MAX_EVENTS_PER_PROBE = 256
READ_CHUNK_BYTES = 64 * 1024
LAUNCHER = """
import os
import sys

read_fd, write_fd = map(int, sys.argv[1:3])
os.dup2(read_fd, 3, inheritable=True)
os.dup2(write_fd, 4, inheritable=True)
os.close(read_fd)
os.close(write_fd)
os.execv(sys.argv[3], sys.argv[3:])
"""
HTML = ('<!doctype html><meta charset="utf-8"><p id="probe">pending</p>'
        '<script>document.getElementById("probe").textContent=2+2</script>')


class ProbeError(Exception):
    """A deliberately detail-free probe failure."""


def _require(condition: bool) -> None:
    if not condition:
        raise ProbeError


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ProbeError
        value[key] = item
    return value


def _reject_constant(_value):
    raise ProbeError


def _close(descriptor: int | None) -> None:
    if descriptor is None:
        return
    try:
        os.close(descriptor)
    except OSError:
        pass


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ProbeError
    return remaining


def _validate(profile: Path, expected_version: str) -> None:
    _require(platform.system() == "Linux" and platform.machine() in {"x86_64", "aarch64"})
    _require(re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", expected_version) is not None
             and len(expected_version) <= 64)
    try:
        with DMI_PRODUCT_NAME.open("rb") as stream:
            product = stream.read(129)
    except OSError as error:
        raise ProbeError from error
    _require(len(product) <= 128 and product.rstrip(b"\n") == PROBE_PRODUCT.encode())

    try:
        user = pwd.getpwnam("kelpie")
    except KeyError as error:
        raise ProbeError from error
    _require(user.pw_name == "kelpie" and type(user.pw_uid) is int
             and type(user.pw_gid) is int and user.pw_uid > 0 and user.pw_gid > 0)
    _require(os.getuid() == os.geteuid() == user.pw_uid
             and os.getgid() == os.getegid() == user.pw_gid)

    _require(profile.parent == Path("/tmp")
             and str(profile) == "/tmp/" + profile.name
             and re.fullmatch(r"kelpie-image-smoke-[A-Za-z0-9_-]{1,80}", profile.name)
             is not None)
    descriptor = None
    try:
        before = profile.lstat()
        _require(stat.S_ISDIR(before.st_mode) and stat.S_IMODE(before.st_mode) == 0o700
                 and before.st_uid == user.pw_uid and before.st_gid == user.pw_gid)
        descriptor = os.open(profile, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(descriptor)
        _require((opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino)
                 and stat.S_IMODE(opened.st_mode) == 0o700
                 and opened.st_uid == user.pw_uid and opened.st_gid == user.pw_gid)
    except (OSError, ValueError) as error:
        raise ProbeError from error
    finally:
        _close(descriptor)


def _duplicate_for_child(descriptor: int) -> int:
    duplicate = fcntl.fcntl(descriptor, fcntl.F_DUPFD_CLOEXEC, 10)
    _require(duplicate >= 10)
    return duplicate


def _launch(profile: Path):
    command_read = command_write = response_read = response_write = None
    child_read = child_write = None
    process = None
    succeeded = False
    try:
        command_read, command_write = os.pipe()
        response_read, response_write = os.pipe()
        child_read = _duplicate_for_child(command_read)
        child_write = _duplicate_for_child(response_write)
        args = [
            sys.executable, "-I", "-S", "-c", LAUNCHER, str(child_read), str(child_write),
            str(BROWSER), "--headless=new", "--disable-background-networking",
            "--disable-component-update", "--disable-default-apps", "--disable-sync",
            "--metrics-recording-only", "--no-first-run", "--no-default-browser-check",
            "--user-data-dir=" + str(profile), "--remote-debugging-pipe", "about:blank",
        ]
        process = subprocess.Popen(
            args, close_fds=True, pass_fds=(child_read, child_write),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=CHILD_ENVIRONMENT,
        )
        _close(command_read)
        command_read = None
        _close(response_write)
        response_write = None
        _close(child_read)
        child_read = None
        _close(child_write)
        child_write = None
        os.set_blocking(command_write, False)
        os.set_blocking(response_read, False)
        succeeded = True
        return process, command_write, response_read
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise ProbeError from error
    finally:
        _close(command_read)
        _close(response_write)
        _close(child_read)
        _close(child_write)
        if not succeeded:
            _close(command_write)
            _close(response_read)
            if process is not None:
                _stop_and_reap(process)


def _stop_and_reap(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        process.wait()
        return
    try:
        process.terminate()
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired as error:
            raise ProbeError from error
    except OSError as error:
        raise ProbeError from error


class _Connection:
    def __init__(self, write_descriptor: int, read_descriptor: int, deadline: float):
        self.write_descriptor = write_descriptor
        self.read_descriptor = read_descriptor
        self.deadline = deadline
        self.buffer = bytearray()
        self.next_id = 0
        self.events = 0

    def _wait(self, *, writing: bool) -> None:
        while True:
            try:
                readable = [] if writing else [self.read_descriptor]
                writable = [self.write_descriptor] if writing else []
                ready_read, ready_write, _ = select.select(
                    readable, writable, [], _remaining(self.deadline))
            except InterruptedError:
                continue
            except (OSError, ValueError) as error:
                raise ProbeError from error
            _require(bool(ready_write if writing else ready_read))
            return

    def _write(self, content: bytes) -> None:
        offset = 0
        while offset < len(content):
            self._wait(writing=True)
            try:
                written = os.write(self.write_descriptor, content[offset:])
            except BlockingIOError:
                continue
            except OSError as error:
                raise ProbeError from error
            _require(written > 0)
            offset += written

    def _frame(self) -> dict:
        while True:
            separator = self.buffer.find(0)
            if separator >= 0:
                _require(0 < separator <= MAX_FRAME_BYTES)
                frame = bytes(self.buffer[:separator])
                del self.buffer[:separator + 1]
                try:
                    value = json.loads(
                        frame.decode("utf-8", errors="strict"),
                        object_pairs_hook=_unique_object,
                        parse_constant=_reject_constant,
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError,
                        ProbeError) as error:
                    raise ProbeError from error
                _require(isinstance(value, dict))
                return value
            _require(len(self.buffer) <= MAX_FRAME_BYTES)
            self._wait(writing=False)
            try:
                part = os.read(self.read_descriptor, READ_CHUNK_BYTES)
            except BlockingIOError:
                continue
            except OSError as error:
                raise ProbeError from error
            _require(bool(part))
            self.buffer.extend(part)

    def call(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        self.next_id += 1
        identifier = self.next_id
        request = {"id": identifier, "method": method, "params": params or {}}
        if session is not None:
            request["sessionId"] = session
        self._write(json.dumps(request, separators=(",", ":")).encode() + b"\0")
        while True:
            response = self._frame()
            if "id" not in response:
                _require(set(response) <= {"method", "params", "sessionId"}
                         and isinstance(response.get("method"), str)
                         and 0 < len(response["method"]) <= 256
                         and isinstance(response.get("params", {}), dict)
                         and ("sessionId" not in response
                              or (isinstance(response["sessionId"], str)
                                  and 0 < len(response["sessionId"]) <= 256)))
                self.events += 1
                _require(self.events <= MAX_EVENTS_PER_PROBE)
                continue
            _require(type(response["id"]) is int and response["id"] == identifier)
            _require("error" not in response and set(response) <= {"id", "result", "sessionId"}
                     and isinstance(response.get("result"), dict))
            if session is None:
                _require("sessionId" not in response)
            else:
                _require(response.get("sessionId") == session)
            return response["result"]


def _protocol(profile: Path, expected_version: str) -> None:
    deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
    process = None
    write_descriptor = read_descriptor = None
    completed = False
    cleanup_error = None
    try:
        process, write_descriptor, read_descriptor = _launch(profile)
        connection = _Connection(write_descriptor, read_descriptor, deadline)
        version = connection.call("Browser.getVersion")
        _require(version.get("product") == "Chrome/" + expected_version)

        target = connection.call("Target.createTarget", {
            "url": "data:text/html," + quote(HTML, safe=""),
        }).get("targetId")
        _require(isinstance(target, str) and 0 < len(target) <= 256)
        session = connection.call("Target.attachToTarget", {
            "targetId": target,
            "flatten": True,
        }).get("sessionId")
        _require(isinstance(session, str) and 0 < len(session) <= 256)

        while True:
            evaluation = connection.call("Runtime.evaluate", {
                "expression": ("({calculation:2+2,text:document.getElementById('probe')"
                               "?.textContent})"),
                "returnByValue": True,
            }, session)
            _require("exceptionDetails" not in evaluation)
            remote = evaluation.get("result")
            _require(isinstance(remote, dict) and remote.get("type") == "object")
            value = remote.get("value")
            _require(isinstance(value, dict) and type(value.get("calculation")) is int
                     and value["calculation"] == 4)
            if value.get("text") == "4":
                break
            time.sleep(min(0.05, _remaining(deadline)))

        connection.call("Browser.close")
        # The close acknowledgement ends command traffic. Deliver EOF before
        # joining Chromium so its pipe-reader thread cannot wait on our writer.
        _close(write_descriptor)
        write_descriptor = None
        try:
            return_code = process.wait(timeout=_remaining(deadline))
        except subprocess.TimeoutExpired as error:
            raise ProbeError from error
        _require(return_code == 0)
        completed = True
    except ProbeError:
        raise
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        raise ProbeError from error
    finally:
        _close(write_descriptor)
        _close(read_descriptor)
        if process is not None and (not completed or process.poll() is None):
            try:
                _stop_and_reap(process)
            except ProbeError as error:
                cleanup_error = error
        if cleanup_error is not None and sys.exc_info()[0] is None:
            raise cleanup_error


def probe(profile: Path, expected_version: str) -> dict:
    try:
        _validate(profile, expected_version)
        _protocol(profile, expected_version)
    except ProbeError:
        raise
    except Exception as error:
        raise ProbeError from error
    return {
        "schema_version": 1,
        "status": "browser_dom_passed",
        "browser_version": expected_version,
    }


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        _require(len(arguments) == 2)
        result = probe(Path(arguments[0]), arguments[1])
    except BaseException:
        print(FAILURE_MESSAGE, file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
