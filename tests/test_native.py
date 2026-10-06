"""Native host tests: message framing, start in background, stop via /api/shutdown."""
import io
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(ROOT, "native"))
import host  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class NativeHostTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["JOBTRACKER_DATA"] = self.tmp
        self.port = free_port()

    def tearDown(self):
        if host.is_running(self.port):
            self.shutdown()
        os.environ.pop("JOBTRACKER_DATA", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def shutdown(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/shutdown", data=b"", method="POST")
        with urllib.request.urlopen(req) as r:
            return json.load(r)

    def test_framing_round_trip(self):
        buf = io.BytesIO()
        host.write_message(buf, {"cmd": "status", "x": "é"})
        buf.seek(0)
        self.assertEqual(host.read_message(buf), {"cmd": "status", "x": "é"})
        self.assertIsNone(host.read_message(io.BytesIO(b"")))

    def test_host_process_speaks_protocol(self):
        msg = json.dumps({"cmd": "status"}).encode()
        out = subprocess.run([sys.executable, os.path.join(ROOT, "native", "host.py")],
                             input=struct.pack("<I", len(msg)) + msg, capture_output=True, timeout=10).stdout
        (n,) = struct.unpack("<I", out[:4])
        reply = json.loads(out[4:4 + n])
        self.assertTrue(reply["ok"])
        self.assertIn("running", reply)

    def test_unknown_command(self):
        self.assertFalse(host.handle({"cmd": "rm -rf"}, self.port)["ok"])

    def test_start_then_shutdown(self):
        self.assertFalse(host.is_running(self.port))
        r = host.start(self.port)
        self.assertEqual(r, {"ok": True, "started": True})
        self.assertTrue(host.is_running(self.port))
        self.assertEqual(host.start(self.port)["started"], False)  # idempotent
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "applications.csv")))
        self.assertTrue(self.shutdown()["stopping"])
        deadline = time.time() + 5
        while host.is_running(self.port) and time.time() < deadline:
            time.sleep(0.1)
        self.assertFalse(host.is_running(self.port))

    def test_shutdown_blocked_for_websites(self):
        host.start(self.port)
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/shutdown", data=b"", method="POST",
                                     headers={"Origin": "https://evil.example"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 403)
        self.assertTrue(host.is_running(self.port))


if __name__ == "__main__":
    unittest.main()
