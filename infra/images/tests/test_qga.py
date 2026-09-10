import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from infra.images import prepare, qga


class GuestAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="kelpie-qga-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "agent.sock"
        self.errors = []

    def server(self, handler):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.path))
        server.listen(1)
        server.settimeout(3)

        def serve():
            try:
                with server, server.accept()[0] as connection:
                    connection.settimeout(3)
                    handler(connection)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected for refused peer/oversized response tests.
            except BaseException as error:
                self.errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        def finish():
            thread.join(4)
            self.assertFalse(thread.is_alive(), "synthetic server did not exit")
            self.assertEqual(self.errors, [])

        self.addCleanup(finish)
        return thread

    def read(self, connection, *, sync=False):
        value = b""
        while not value.endswith(b"\n"):
            chunk = connection.recv(1)
            if not chunk:
                raise EOFError("client disconnected")
            value += chunk
        if sync:
            self.assertEqual(value[:1], b"\xff")
            value = value[1:]
        return json.loads(value)

    def synchronize(self, connection):
        request = self.read(connection, sync=True)
        self.assertEqual(request["execute"], "guest-sync-delimited")
        nonce = request["arguments"]["id"]
        connection.sendall(b"\xff" + json.dumps({"return": nonce}).encode() + b"\n")

    def test_real_socket_discards_partial_stale_data_and_matches_nonce(self):
        def handler(connection):
            request = self.read(connection, sync=True)
            nonce = request["arguments"]["id"]
            connection.sendall(b'partial-stale-json\xff{"return":-1}\n')
            payload = b"\xff" + json.dumps({"return": nonce}).encode() + b"\n"
            for byte in payload:
                connection.sendall(bytes([byte]))
            self.assertEqual(self.read(connection), {"execute": "guest-ping"})
            connection.sendall(b'{"return":{}}\n')

        thread = self.server(handler)
        with qga.GuestAgent(self.path, time.monotonic() + 3) as client:
            self.assertEqual(client.call("guest-ping"), {})
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])

    def test_real_socket_rejects_duplicate_json_keys(self):
        def handler(connection):
            self.synchronize(connection)
            self.read(connection)
            connection.sendall(b'{"return":{},"return":{"synthetic":"private"}}\n')

        self.server(handler)
        with qga.GuestAgent(self.path, time.monotonic() + 3) as client:
            with self.assertRaises(prepare.InputError):
                client.call("guest-ping")

    def test_guest_errors_are_not_returned_to_caller(self):
        def handler(connection):
            self.synchronize(connection)
            self.read(connection)
            connection.sendall(b'{"error":{"desc":"synthetic-sensitive-value"}}\n')

        self.server(handler)
        with qga.GuestAgent(self.path, time.monotonic() + 3) as client:
            with self.assertRaisesRegex(prepare.InputError, "^guest agent command rejected$"):
                client.call("guest-exec", {"path": "/synthetic"})

    def test_real_socket_bounds_unterminated_response(self):
        def handler(connection):
            self.synchronize(connection)
            self.read(connection)
            connection.sendall(b"x" * (qga.MAX_MESSAGE + 1))

        self.server(handler)
        with qga.GuestAgent(self.path, time.monotonic() + 3) as client:
            with self.assertRaisesRegex(prepare.InputError, "too large"):
                client.call("guest-ping")

    def test_actual_socket_timeout_does_not_hang(self):
        def handler(connection):
            self.synchronize(connection)
            self.read(connection)
            time.sleep(0.3)

        thread = self.server(handler)
        with qga.GuestAgent(self.path, time.monotonic() + 0.15) as client:
            with self.assertRaises(TimeoutError):
                client.call("guest-ping")
        thread.join(1)

    def test_symlink_socket_is_not_followed(self):
        target = Path(self.temporary.name) / "target"
        target.write_text("preserve")
        self.path.symlink_to(target)
        with self.assertRaisesRegex(prepare.InputError, "socket must be owned"):
            qga.GuestAgent(self.path, time.monotonic() + 1)
        self.assertEqual(target.read_text(), "preserve")

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux peer credentials only")
    def test_linux_peer_pid_must_match_owned_qemu_process(self):
        self.server(lambda connection: self.synchronize(connection))
        with qga.GuestAgent(self.path, time.monotonic() + 3, peer_pid=os.getpid()):
            pass

    def test_mismatched_peer_is_rejected_before_sync(self):
        with patch.object(qga.socket, "socket") as factory, \
                patch.object(qga.Path, "lstat") as lstat, \
                patch.object(qga.socket, "SO_PEERCRED", 17, create=True), \
                patch.object(qga, "struct") as struct:
            lstat.return_value.st_mode = stat.S_IFSOCK
            lstat.return_value.st_uid = os.getuid()
            struct.unpack.return_value = (999, os.getuid(), 1)
            with self.assertRaisesRegex(prepare.InputError, "peer identity mismatch"):
                qga.GuestAgent(self.path, time.monotonic() + 1, peer_pid=123)
        factory.return_value.sendall.assert_not_called()
        factory.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
