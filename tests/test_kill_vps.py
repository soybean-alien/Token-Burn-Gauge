"""The VPS kill button: what this PC sends, and how it reports each outcome."""
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


class FakeVps(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        FakeVps.seen.append((self.path, self.headers.get("X-Gauge-Kill"),
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


class KillVpsTests(unittest.TestCase):
    def setUp(self):
        FakeVps.seen = []
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeVps)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/metrics-a.json"

    def test_request_goes_to_kill_next_to_the_metrics_file_with_the_secret(self):
        r = collector_b.kill_vps({"vps_metrics_url": self.url, "vps_kill_token": SECRET})
        self.assertEqual(FakeVps.seen, [("/kill", "1", SECRET)])
        self.assertEqual(r, {"ok": True, "killed": 1, "message": "ended 1 run"})
        self.assertNotIn(SECRET, json.dumps(r))

    def test_resume_goes_to_its_own_route(self):
        cfg = {"vps_metrics_url": self.url, "vps_kill_token": SECRET}
        self.assertTrue(collector_b.kill_vps(cfg, "/resume")["ok"])
        self.assertEqual(FakeVps.seen, [("/resume", "1", SECRET)])

    def test_wrong_secret_is_reported_as_refused(self):
        r = collector_b.kill_vps({"vps_metrics_url": self.url, "vps_kill_token": "wrong"})
        self.assertFalse(r["ok"])
        self.assertIn("403", r["message"])

    def test_unset_config_sends_nothing(self):
        for cfg in ({}, {"vps_metrics_url": self.url}, {"vps_kill_token": SECRET}):
            self.assertFalse(collector_b.kill_vps(cfg)["ok"])
        self.assertEqual(FakeVps.seen, [])

    def test_unreachable_vps_is_reported(self):
        r = collector_b.kill_vps({"vps_metrics_url": "http://127.0.0.1:9/metrics-a.json",
                                  "vps_kill_token": SECRET})
        self.assertFalse(r["ok"])
        self.assertIn("unreachable", r["message"])
        self.assertIn("pause not sent", r["message"])

    def test_agents_routes_the_vps_target(self):
        a = agents.Agents({"stale_after_s": 5, "poll_s": 1}, proc_reader=lambda: [],
                          kill_vps=lambda route: {"ok": True, "message": route})
        self.assertEqual(a.kill("vps")["message"], "/kill")
        self.assertEqual(a.kill("vps-resume")["message"], "/resume")
        b = agents.Agents({"stale_after_s": 5, "poll_s": 1}, proc_reader=lambda: [])
        self.assertFalse(b.kill("vps")["ok"])


if __name__ == "__main__":
    unittest.main()
