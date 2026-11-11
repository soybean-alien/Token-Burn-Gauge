"""The remote host kill button: what this PC sends, and how it reports each outcome."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402
import collector_b  # noqa: E402

SECRET = "test-only-shared-secret"


class FakeRemote(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        FakeRemote.seen.append((self.path, self.headers.get("X-Gauge-Kill"),
                             self.headers.get("X-Gauge-Kill-Token")))
        if self.headers.get("X-Gauge-Kill-Token") != SECRET:
            self.send_response(403)
            self.end_headers()
            return
        body = json.dumps({"ok": True, "killed": 1, "message": "ended 1 run"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class PauseRemoteTests(unittest.TestCase):
    def setUp(self):
        FakeRemote.seen = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeRemote)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/metrics-a.json"

    def test_request_goes_to_kill_next_to_the_metrics_file_with_the_secret(self):
        r = collector_b.pause_remote({"remote_url": self.url, "remote_pause_token": SECRET})
        self.assertEqual(FakeRemote.seen, [("/kill", "1", SECRET)])
        self.assertEqual(r, {"ok": True, "killed": 1, "message": "ended 1 run"})
        self.assertNotIn(SECRET, json.dumps(r))

    def test_resume_goes_to_its_own_route(self):
        cfg = {"remote_url": self.url, "remote_pause_token": SECRET}
        self.assertTrue(collector_b.pause_remote(cfg, "/resume")["ok"])
        self.assertEqual(FakeRemote.seen, [("/resume", "1", SECRET)])

    def test_wrong_secret_is_reported_as_refused(self):
        r = collector_b.pause_remote({"remote_url": self.url, "remote_pause_token": "wrong"})
        self.assertFalse(r["ok"])
        self.assertIn("403", r["message"])

    def test_unset_config_sends_nothing(self):
        for cfg in ({}, {"remote_url": self.url}, {"remote_pause_token": SECRET}):
            self.assertFalse(collector_b.pause_remote(cfg)["ok"])
        self.assertEqual(FakeRemote.seen, [])

    def test_unreachable_remote_is_reported(self):
        r = collector_b.pause_remote({"remote_url": "http://127.0.0.1:9/metrics-a.json",
                                  "remote_pause_token": SECRET})
        self.assertFalse(r["ok"])
        self.assertIn("unreachable", r["message"])
        self.assertIn("pause not sent", r["message"])

    def test_agents_routes_the_remote_target(self):
        a = agents.Agents({"stale_after_s": 5, "poll_s": 1}, proc_reader=lambda: [],
                          pause_remote=lambda route: {"ok": True, "message": route})
        self.assertEqual(a.kill("model")["message"], "/kill")
        self.assertEqual(a.kill("model-resume")["message"], "/resume")
        b = agents.Agents({"stale_after_s": 5, "poll_s": 1}, proc_reader=lambda: [])
        self.assertFalse(b.kill("model")["ok"])


if __name__ == "__main__":
    unittest.main()
