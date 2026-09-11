"""Bounded QEMU guest-agent transport for a privately owned image-probe VM."""

import json
import os
import secrets
import socket
import stat
import struct
import time
from pathlib import Path

if __package__:
    from . import prepare
else:
    import prepare

MAX_MESSAGE = 128 * 1024


class GuestAgent:
    def __init__(self, path: Path, deadline: float, *, peer_pid: int | None = None):
        self.deadline = deadline
        self.buffer = b""
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            info = path.lstat()
            prepare.require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid(),
                            "guest agent socket must be owned")
            self.socket.settimeout(self.remaining())
            self.socket.connect(str(path))
            if peer_pid is not None:
                prepare.require(hasattr(socket, "SO_PEERCRED"), "peer identity is unavailable")
                peer = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
                pid, uid, _ = struct.unpack("3i", peer)
                prepare.require(pid == peer_pid and uid == os.getuid(),
                                "guest agent peer identity mismatch")
            self.synchronize()
        except BaseException:
            self.socket.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.socket.close()

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("guest agent deadline exceeded")
        return min(5.0, remaining)

    def send(self, name: str, arguments: dict | None = None, *, prefix=b"") -> None:
        self.socket.settimeout(self.remaining())
        payload = {"execute": name}
        if arguments is not None:
            payload["arguments"] = arguments
        self.socket.sendall(prefix + json.dumps(payload).encode("ascii") + b"\n")

    def receive(self, *, delimited=False) -> dict:
        received = len(self.buffer)
        synchronized = not delimited
        while True:
            sentinel = self.buffer.rfind(b"\xff")
            if sentinel >= 0:
                self.buffer = self.buffer[sentinel + 1:]
                synchronized = True
            if synchronized and b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                prepare.require(len(line) <= MAX_MESSAGE, "guest agent response too large")
                try:
                    value = json.loads(line.decode("utf-8"),
                                       object_pairs_hook=prepare.unique_object)
                except (ValueError, UnicodeError, RecursionError):
                    raise prepare.InputError("invalid guest agent JSON") from None
                prepare.require(isinstance(value, dict), "invalid guest agent response")
                return value
            self.socket.settimeout(self.remaining())
            chunk = self.socket.recv(4096)
            prepare.require(bool(chunk), "guest agent disconnected")
            received += len(chunk)
            prepare.require(received <= MAX_MESSAGE, "guest agent response too large")
            self.buffer += chunk

    def synchronize(self) -> None:
        nonce = secrets.randbits(63)
        self.send("guest-sync-delimited", {"id": nonce}, prefix=b"\xff")
        # A new connection may contain stale complete/partial responses. Only the
        # sentinel-framed, unique echoed nonce permits subsequent commands.
        for index in range(16):
            response = self.receive(delimited=index == 0)
            if set(response) == {"return"} and type(response["return"]) is int \
                    and response["return"] == nonce:
                return
        raise prepare.InputError("guest agent synchronization failed")

    def call(self, name: str, arguments: dict | None = None):
        self.send(name, arguments)
        response = self.receive()
        prepare.require(set(response) == {"return"}, "guest agent command rejected")
        return response["return"]
