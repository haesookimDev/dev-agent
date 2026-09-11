import fcntl
import io
import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from infra.images import browser_probe

VERSION = "153.0.8010.36"


class BrowserProbeTests(unittest.TestCase):
    def setUp(self):
        self.assertGreater(os.geteuid(), 0, "run image tests as non-root")
        self.profile = tempfile.TemporaryDirectory(prefix="kelpie-image-smoke-", dir="/tmp")
        self.addCleanup(self.profile.cleanup)
        os.chown(self.profile.name, os.geteuid(), os.getegid())
        os.chmod(self.profile.name, 0o700)
        self.scratch = tempfile.TemporaryDirectory(prefix="kelpie-browser-probe-test-")
        self.addCleanup(self.scratch.cleanup)
        self.dmi = Path(self.scratch.name) / "product_name"
        self.dmi.write_text(browser_probe.PROBE_PRODUCT + "\n")
        identity = SimpleNamespace(pw_name="kelpie", pw_uid=os.geteuid(), pw_gid=os.getegid())
        patches = (
            patch.object(browser_probe, "DMI_PRODUCT_NAME", self.dmi),
            patch.object(browser_probe.platform, "system", return_value="Linux"),
            patch.object(browser_probe.platform, "machine", return_value="x86_64"),
            patch.object(browser_probe.pwd, "getpwnam", return_value=identity),
        )
        for active in patches:
            active.start()
            self.addCleanup(active.stop)

    def browser(self, mode="success"):
        path = Path(self.scratch.name) / ("browser-" + mode)
        source = textwrap.dedent(
            f"""\
            #!{sys.executable}
            import fcntl
            import json
            import os
            import sys
            import time

            mode = {mode!r}
            assert "--remote-debugging-pipe" in sys.argv
            assert not any(arg == "--no-sandbox" or arg.startswith("--remote-debugging-port")
                           for arg in sys.argv)
            assert any(arg.startswith("--user-data-dir=/tmp/kelpie-image-smoke-")
                       for arg in sys.argv)
            assert "PROBE_CREDENTIAL" not in os.environ and "HOME" not in os.environ
            assert os.environ["LANG"] == "C.UTF-8"
            assert os.environ["PATH"] == "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            # macOS injects this one locale key while launching a process; the Linux
            # production guest receives exactly PATH and LANG.
            assert set(os.environ) <= {{"LANG", "PATH", "__CF_USER_TEXT_ENCODING"}}
            opened = []
            for descriptor in range(64):
                try:
                    fcntl.fcntl(descriptor, fcntl.F_GETFD)
                except OSError:
                    continue
                opened.append(descriptor)
            assert opened == [0, 1, 2, 3, 4], opened
            profile = next(arg.split("=", 1)[1] for arg in sys.argv
                           if arg.startswith("--user-data-dir="))
            with open(profile + "/stub.pid", "w") as stream:
                stream.write(str(os.getpid()))

            buffered = b""
            def request():
                global buffered
                while b"\\0" not in buffered:
                    part = os.read(3, 7)
                    if not part:
                        raise SystemExit(91)
                    buffered += part
                frame, buffered = buffered.split(b"\\0", 1)
                return json.loads(frame)

            def send(value):
                data = json.dumps(value, separators=(",", ":")).encode() + b"\\0"
                while data:
                    written = os.write(4, data[:3])
                    data = data[written:]
                    if mode == "fragmented":
                        time.sleep(0.001)

            first = request()
            if mode == "timeout":
                time.sleep(5)
                raise SystemExit(92)
            if mode == "eof":
                os.close(4)
                raise SystemExit(0)
            if mode == "oversized":
                oversized = b"x" * ({browser_probe.MAX_FRAME_BYTES} + 1)
                while oversized:
                    written = os.write(4, oversized)
                    oversized = oversized[written:]
                time.sleep(5)
                raise SystemExit(93)
            if mode == "malformed":
                os.write(4, b"{{]\\0")
                raise SystemExit(0)
            if mode == "duplicate":
                os.write(4, b'{{"id":1,"id":1,"result":{{}}}}\\0')
                raise SystemExit(0)
            if mode == "invalid_utf8":
                os.write(4, b"\\xff\\0")
                raise SystemExit(0)
            if mode == "error":
                send({{"id": first["id"], "error": {{"code": -1, "message": "private"}}}})
                raise SystemExit(0)
            if mode == "wrong_id":
                send({{"id": first["id"] + 1, "result": {{}}}})
                raise SystemExit(0)
            if mode == "events":
                for _ in range({browser_probe.MAX_EVENTS_PER_PROBE} + 1):
                    send({{"method": "Target.targetInfoChanged", "params": {{}}}})
            product = "Chrome/{VERSION}" if mode != "wrong_version" else "Chrome/1.2.3.4"
            send({{"id": first["id"], "result": {{"product": product}}}})

            created = request()
            send({{"id": created["id"], "result": {{"targetId": "target-1"}}}})
            attached = request()
            send({{"id": attached["id"], "result": {{"sessionId": "session-1"}}}})
            evaluated = request()
            session = "wrong-session" if mode == "wrong_session" else "session-1"
            result = {{"result": {{"type": "object", "value": {{
                "calculation": 4, "text": "4"
            }}}}}}
            if mode == "exception":
                result["exceptionDetails"] = {{"text": "private"}}
            if mode == "boolean_calculation":
                result["result"]["value"]["calculation"] = True
            if mode == "missing_value":
                del result["result"]["value"]
            if mode == "pending_dom":
                result["result"]["value"]["text"] = "pending"
                while True:
                    assert evaluated["method"] == "Runtime.evaluate"
                    send({{"id": evaluated["id"], "sessionId": session, "result": result}})
                    evaluated = request()
            send({{"id": evaluated["id"], "sessionId": session, "result": result}})
            closed = request()
            send({{"id": closed["id"], "result": {{}}}})
            if mode == "close_waits_for_eof":
                assert os.read(3, 1) == b""
            raise SystemExit(7 if mode == "child_failure" else 0)
            """
        )
        path.write_text(source)
        path.chmod(0o755)
        return path

    def run_probe(self, mode="success", timeout=2.0):
        with patch.object(browser_probe, "BROWSER", self.browser(mode)), \
                patch.object(browser_probe, "TOTAL_TIMEOUT_SECONDS", timeout), \
                patch.dict(os.environ, {"PROBE_CREDENTIAL": "must-not-reach-child"}):
            return browser_probe.probe(Path(self.profile.name), VERSION)

    @staticmethod
    def open_descriptors():
        descriptors = set()
        for descriptor in range(256):
            try:
                fcntl.fcntl(descriptor, fcntl.F_GETFD)
            except OSError:
                continue
            descriptors.add(descriptor)
        return descriptors

    def test_success_uses_pipe_sandbox_clean_environment_and_closes_fds(self):
        before = self.open_descriptors()
        self.assertEqual(self.run_probe(), {
            "schema_version": 1,
            "status": "browser_dom_passed",
            "browser_version": VERSION,
        })
        self.assertEqual(self.open_descriptors(), before)

    def test_writes_requests_completely_when_the_pipe_accepts_partial_writes(self):
        write = browser_probe.os.write

        def partial_write(descriptor, content):
            return write(descriptor, content[:2])

        with patch.object(browser_probe.os, "write", side_effect=partial_write):
            self.run_probe()

    def test_reads_fragmented_response_frames(self):
        self.run_probe("fragmented")

    def test_rejects_timeout_eof_oversized_and_malformed_frames(self):
        for mode in ("timeout", "eof", "oversized", "malformed", "duplicate", "invalid_utf8"):
            with self.subTest(mode=mode):
                started = time.monotonic()
                with self.assertRaises(browser_probe.ProbeError):
                    self.run_probe(mode, timeout=0.5)
                self.assertLess(time.monotonic() - started, 1.5)
                if mode == "timeout":
                    pid = int((Path(self.profile.name) / "stub.pid").read_text())
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)

    def test_rejects_protocol_error_wrong_response_and_event_flood(self):
        for mode in ("error", "wrong_id", "wrong_session", "exception", "events"):
            with self.subTest(mode=mode), self.assertRaises(browser_probe.ProbeError):
                self.run_probe(mode)

    def test_rejects_wrong_version_and_nonzero_browser_exit(self):
        for mode in ("wrong_version", "child_failure"):
            with self.subTest(mode=mode), self.assertRaises(browser_probe.ProbeError):
                self.run_probe(mode)

    def test_rejects_unexecuted_dom_and_invalid_javascript_results(self):
        for mode in ("pending_dom", "boolean_calculation", "missing_value"):
            with self.subTest(mode=mode), self.assertRaises(browser_probe.ProbeError):
                self.run_probe(mode, timeout=0.5)

    def test_blocked_request_pipe_obeys_the_shared_deadline(self):
        read_descriptor, write_descriptor = os.pipe()
        try:
            os.set_blocking(write_descriptor, False)
            while True:
                try:
                    os.write(write_descriptor, b"x" * 65536)
                except BlockingIOError:
                    break
            started = time.monotonic()
            connection = browser_probe._Connection(
                write_descriptor, read_descriptor, started + 0.1)
            with self.assertRaises(browser_probe.ProbeError):
                connection.call("Browser.getVersion")
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            os.close(read_descriptor)
            os.close(write_descriptor)

    def test_failed_launch_closes_every_owned_pipe(self):
        before = self.open_descriptors()
        with patch.object(browser_probe.subprocess, "Popen", side_effect=OSError("synthetic")):
            with self.assertRaises(browser_probe.ProbeError):
                browser_probe._launch(Path(self.profile.name))
        self.assertEqual(self.open_descriptors(), before)

    def test_close_response_is_followed_by_command_eof_before_reaping(self):
        # This tests EOF ordering, not a half-second Python startup SLA. Use the
        # same bounded budget as other success paths; a missing EOF still times out.
        self.run_probe("close_waits_for_eof")

    def test_validates_version_identity_and_exact_owned_profile(self):
        profile = Path(self.profile.name)
        browser = self.browser()
        with patch.object(browser_probe, "BROWSER", browser):
            for version in ("", "153", "153.0.beta.1", "153.0.0.0/private"):
                with self.subTest(version=version), self.assertRaises(browser_probe.ProbeError):
                    browser_probe.probe(profile, version)
            profile.chmod(0o755)
            with self.assertRaises(browser_probe.ProbeError):
                browser_probe.probe(profile, VERSION)
            profile.chmod(0o700)
            link = profile.with_name(profile.name + "-link")
            link.symlink_to(profile, target_is_directory=True)
            self.addCleanup(link.unlink, missing_ok=True)
            with self.assertRaises(browser_probe.ProbeError):
                browser_probe.probe(link, VERSION)
            with patch.object(browser_probe.os, "geteuid", return_value=0):
                with self.assertRaises(browser_probe.ProbeError):
                    browser_probe.probe(profile, VERSION)
            with patch.object(browser_probe.platform, "machine", return_value="arm64"):
                with self.assertRaises(browser_probe.ProbeError):
                    browser_probe.probe(profile, VERSION)
            self.dmi.write_text("another-machine\n")
            with self.assertRaises(browser_probe.ProbeError):
                browser_probe.probe(profile, VERSION)

    def test_main_emits_only_fixed_success_or_failure_lines(self):
        success = {
            "schema_version": 1,
            "status": "browser_dom_passed",
            "browser_version": VERSION,
        }
        output, error = io.StringIO(), io.StringIO()
        with patch.object(browser_probe, "probe", return_value=success), \
                redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(browser_probe.main([self.profile.name, VERSION]), 0)
        self.assertEqual(output.getvalue(), json.dumps(success, separators=(",", ":")) + "\n")
        self.assertEqual(error.getvalue(), "")

        output, error = io.StringIO(), io.StringIO()
        with patch.object(browser_probe, "probe", side_effect=RuntimeError(
                "private path /tmp/private and credential")), \
                redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(browser_probe.main([self.profile.name, VERSION]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(error.getvalue(), browser_probe.FAILURE_MESSAGE + "\n")


if __name__ == "__main__":
    unittest.main()
