"""The kill switch against real things: a real process and a real stream."""
import json
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402
import proxy  # noqa: E402


class TaskkillTests(unittest.TestCase):
    def test_a_real_process_and_its_child_are_ended(self):
        child_code = "import time; time.sleep(60)"
        parent_code = ("import subprocess, sys, time; "
                       f"p = subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
                       "print(p.pid, flush=True); time.sleep(60)")
        parent = subprocess.Popen([sys.executable, "-c", parent_code], stdout=subprocess.PIPE)
        child_pid = int(parent.stdout.readline())
        self.assertTrue(agents.taskkill(parent.pid))
        self.assertIsNotNone(parent.wait(timeout=5))
        parent.stdout.close()
        time.sleep(0.5)
        out = subprocess.run(["tasklist", "/FI", f"PID eq {child_pid}", "/NH"],
                             capture_output=True, text=True).stdout
        self.assertNotIn(str(child_pid), out)


class SlowStream(BaseHTTPRequestHandler):
    """A stand-in runtime that streams one chunk every 0.1 s for a minute."""
    sent = 0
    stopped = threading.Event()

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for _ in range(600):
                chunk = {"choices": [{"delta": {"content": "x"}}]}
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                self.wfile.flush()
                SlowStream.sent += 1
                time.sleep(0.1)
        except OSError:
            SlowStream.stopped.set()


class AbortTests(unittest.TestCase):
    def test_stream_in_flight_is_cut_and_the_runtime_stops(self):
        up = ThreadingHTTPServer(("127.0.0.1", 0), SlowStream)
        up.daemon_threads = True
        px = proxy.make_server(0, f"http://127.0.0.1:{up.server_address[1]}")
        for s in (up, px):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        base = f"http://127.0.0.1:{px.server_address[1]}"
        got = []

        def client():
            req = urllib.request.Request(base + "/v1/chat/completions", data=b"{}", method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    for line in r:
                        got.append(line)
            except OSError:
                pass

        t = threading.Thread(target=client, daemon=True)
        t.start()
        time.sleep(1.0)
        self.assertGreater(len(got), 2)
        req = urllib.request.Request(base + "/gauge-abort", data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            self.assertEqual(json.loads(r.read())["aborted"], 1)
        t.join(timeout=3)
        self.assertFalse(t.is_alive(), "the client's stream did not end")
        self.assertTrue(SlowStream.stopped.wait(3), "the runtime kept generating")
        self.assertLess(SlowStream.sent, 40)
        self.assertEqual(px.counters.snapshot()["active_requests"], 0)


if __name__ == "__main__":
    unittest.main()
