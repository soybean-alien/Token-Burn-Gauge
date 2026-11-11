"""Tests for the local collector and its page server."""
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import collector_b as cb  # noqa: E402

LINE = "3, 42.57, 5461, 16303, 400.00, NVIDIA GeForce RTX 5080"


def cfg():
    c = json.loads((ROOT / "config.json").read_text())
    c.update(idle_watts=40.0, remote_token="test-token")
    return c


def gpu(watts):
    return lambda: dict(cb.parse_gpu(LINE), watts=watts)


def make(c=None, reader=None, proxy=lambda port: None, **kw):
    return cb.Collector(c or cfg(), gpu_reader=reader or gpu(42.0), proxy_reader=proxy,
                        runtime_probe=lambda url: False, **kw)


class ParseTest(unittest.TestCase):
    def test_real_line(self):
        g = cb.parse_gpu(LINE)
        self.assertEqual((g["watts"], g["util"], g["vram_total"], g["limit"]),
                         (42.57, 3.0, 16303.0, 400.0))
        self.assertEqual(g["name"], "NVIDIA GeForce RTX 5080")

    def test_power_not_available_is_an_error_not_a_zero(self):
        with self.assertRaises(ValueError):
            cb.parse_gpu("3, [N/A], 5461, 16303, [N/A], Some Card")


class CpuTest(unittest.TestCase):
    HEAD = '"(PDH-CSV 4.0)","\\\\PC\\Energy Meter(RAPL_Package0_PKG)\\Power","\\\\PC\\x"'
    ROW = '"11/11/2026 09:25:20.903","84564.887884","8.382492"'

    def test_typeperf_row_is_watts_and_load_and_headers_are_skipped(self):
        self.assertIsNone(cb.parse_cpu(self.HEAD))
        self.assertIsNone(cb.parse_cpu('"11/11/2026 09:25:21.000"," "," "'))
        w, u = cb.parse_cpu(self.ROW)
        self.assertEqual((round(w, 1), round(u, 1)), (84.6, 8.4))

    def test_reading_reaches_the_payload_and_a_stale_one_is_not_a_number(self):
        cpu = cb.CpuWatch(cfg())
        c = make(cpu=cpu)
        self.assertIsNone(c.step(1000.0)["detail"]["cpu"]["watts"])
        cpu.add(self.ROW, 1000.5)
        d = c.step(1001.0)["detail"]["cpu"]
        self.assertEqual((d["watts"], d["util_pct"], d["state"], d["error"]),
                         (84.6, 8.4, "IDLE", None))
        cpu.add('"t","210000.0","70.0"', 1002.0)
        self.assertEqual(c.step(1002.5)["detail"]["cpu"]["state"], "REDLINE")
        d = c.step(1010.0)["detail"]["cpu"]
        self.assertIsNone(d["watts"])
        self.assertIn("no CPU reading", d["error"])


class StateTest(unittest.TestCase):
    def test_every_state_is_reachable(self):  # acceptance step 18, local side
        sm = cb.StateMachine(cfg())
        self.assertEqual(sm.update(45, 400, 40, 0), "IDLE")
        self.assertEqual(sm.update(60, 400, 40, 1), "WARM")
        self.assertEqual(sm.update(120, 400, 40, 2), "WARM")      # over, but not yet for 3 s
        self.assertEqual(sm.update(120, 400, 40, 5), "BURNING")
        self.assertEqual(sm.update(380, 400, 40, 6), "BURNING")   # over 90%, not yet for 30 s
        self.assertEqual(sm.update(380, 400, 40, 36), "REDLINE")
        self.assertEqual(sm.update(41, 400, 40, 37), "IDLE")
        self.assertEqual(sm.update(120, 400, 40, 38), "WARM")     # the sustain timer restarted


class CollectorTest(unittest.TestCase):
    def test_contract_shape_and_explicit_null_tokens(self):  # acceptance step 10
        p = make().step(1000.0)
        for k in ("ts", "seq", "host", "source_ok", "stale_after_s", "dial", "detail", "error"):
            self.assertIn(k, p)
        self.assertEqual(p["dial"]["primary"]["value"], 42.0)
        self.assertEqual(p["dial"]["primary"]["unit"], "W")
        self.assertFalse(p["dial"]["primary"]["log"])
        self.assertEqual(p["dial"]["primary"]["redline"], 360.0)
        self.assertEqual((p["dial"]["fuel"]["value"], p["dial"]["fuel"]["of"]), (5461.0, 16303.0))
        self.assertIn("tokens_per_s", p["detail"])
        self.assertIsNone(p["detail"]["tokens_per_s"])
        self.assertIsNone(p["dial"]["ring"]["value"])
        self.assertEqual(p["dial"]["state"], "IDLE")
        self.assertLess(len(json.dumps(p)), 4096)

    def test_idle_baseline_is_the_median_of_the_first_run(self):
        c = cfg()
        c["idle_watts"] = None
        watts = iter([40, 41, 300, 42, 43, 44])
        col = make(c, reader=lambda: dict(cb.parse_gpu(LINE), watts=next(watts)), calibrate_s=4)
        states = [col.step(1000.0 + i) for i in range(5)]
        self.assertTrue(states[0]["detail"]["calibrating"])
        self.assertFalse(states[4]["detail"]["calibrating"])
        self.assertEqual(c["idle_watts"], 42)  # median of 40 41 300 42 43

    def test_transient_failure_keeps_last_good_then_goes_offline(self):
        ok = [True]

        def reader():
            if not ok[0]:
                raise ValueError("nvidia-smi not found")
            return cb.parse_gpu(LINE)

        col = make(reader=reader)
        col.step(1000.0)
        ok[0] = False
        p = col.step(1002.0)
        self.assertTrue(p["source_ok"])
        self.assertEqual(p["dial"]["primary"]["value"], 42.57)
        p = col.step(1004.5)
        self.assertFalse(p["source_ok"])
        self.assertIsNone(p["dial"]["primary"]["value"])  # never a zero
        self.assertEqual(p["error"], "nvidia-smi not found")

    def test_tokens_per_second_from_the_proxy_counter(self):
        count = [0]
        col = make(proxy=lambda port: {"completion_tokens_live": count[0], "active_requests": 1,
                                       "last_model": "m"})
        col.step(1000.0)
        count[0] = 40
        p = col.step(1001.0)
        self.assertEqual(p["detail"]["tokens_per_s"], 40.0)
        self.assertEqual(p["dial"]["ring"]["value"], 40.0)
        self.assertEqual(p["detail"]["proxy"], "up")

    def test_scrub_env(self):
        os.environ["FAKEPROVIDER_API_KEY"] = "x"
        os.environ["TOKEN_GAUGE_PLAIN"] = "y"
        cb.scrub_env()
        self.assertNotIn("FAKEPROVIDER_API_KEY", os.environ)
        self.assertEqual(os.environ.pop("TOKEN_GAUGE_PLAIN"), "y")

    def test_history_lines(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            h = cb.History(Path(d) / "history-b.jsonl", 30)
            col = make()
            h.add(col.step(1000.0))
            h.add(col.step(1001.0))   # idle: at most one line a minute
            h.add(col.step(1061.0))
            lines = h.path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[0])["w"], 42.0)


class UsageTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "usage.json"
        self.now = 1000.0
        self.tail = cb.UsageTail({}, path=self.path, clock=lambda: self.now)

    def export(self, at, tin, tout, calls=0):
        self.path.write_text(json.dumps({
            "generated_at": at, "last_activity_at": at, "current_model": {"model": "some/model"},
            "model_usage": [{"input_tokens": tin, "output_tokens": tout,
                             "cache_read_tokens": 0, "api_call_count": calls}]}), encoding="utf-8")

    def read(self):
        return json.loads(self.tail.payload()[1])

    def test_no_path_set_is_offline(self):
        d = json.loads(cb.UsageTail({}).payload()[1])
        self.assertFalse(d["source_ok"])
        self.assertIn("not set", d["error"])

    def test_missing_export_is_offline(self):
        d = self.read()
        self.assertFalse(d["source_ok"])
        self.assertIn("not found", d["error"])

    def test_first_reading_has_no_value(self):
        self.export(1000, 5000, 100)
        d = self.read()
        self.assertTrue(d["source_ok"])
        self.assertIsNone(d["dial"]["primary"]["value"])

    def test_rate_is_the_change_between_two_exports(self):
        self.export(1000, 5000, 100, 10)
        self.read()
        self.now = 1120.0
        self.export(1120, 9000, 300, 14)  # two minutes later: +4000 in, +200 out, +4 calls
        d = self.read()
        self.assertEqual(d["dial"]["primary"]["value"], 2100)
        self.assertEqual(d["detail"]["tokens_in_per_min"], 2000)
        self.assertEqual(d["detail"]["api_calls_per_min"], 2.0)
        self.assertEqual(d["seq"], 2)
        self.assertEqual(self.read()["seq"], 2)  # same export read again: seq holds, so the app can call it STALE

    def test_totals_going_down_give_no_rate(self):
        self.export(1000, 5000, 100)
        self.read()
        self.now = 1060.0
        self.export(1060, 10, 0)
        self.assertIsNone(self.read()["dial"]["primary"]["value"])

    def rows(self, at, rows):
        self.path.write_text(json.dumps({"generated_at": at, "model_usage": [
            {"session": s, "input_tokens": n, "output_tokens": 0, "first_seen": first} for s, n, first in rows]}),
            encoding="utf-8")

    def test_a_dropped_row_does_not_hide_the_others(self):
        self.rows(1000, [("old", 900000, 1), ("live", 1000, 900)])
        self.read()
        self.now = 1060.0
        self.rows(1060, [("live", 1500, 900)])  # "old" aged out of the export; "live" did 500
        self.assertEqual(self.read()["dial"]["primary"]["value"], 500)

    def test_a_new_row_counts_only_if_it_began_after_the_last_export(self):
        self.rows(1000, [("live", 1000, 900)])
        self.read()
        self.now = 1060.0
        self.rows(1060, [("live", 1000, 900), ("fresh", 300, 1030), ("backfilled", 70000, 5)])
        self.assertEqual(self.read()["dial"]["primary"]["value"], 300)

    def test_old_export_is_offline_not_zero(self):
        self.export(1000, 5000, 100)
        self.now = 1000.0 + 600
        d = self.read()
        self.assertFalse(d["source_ok"])
        self.assertIn("10 min old", d["error"])


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.col = make()
        self.col.step(1000.0)
        self.server = cb.Server(("127.0.0.1", 0),
                                cb.make_handler(self.col, cb.UsageTail({}, path="no-such-export.json"), "test-token"))
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read(), e.headers

    def test_bound_to_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_routes(self):
        status, body, headers = self.get("/metrics-b.json")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["host"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(self.get("/")[0], 200)
        status, body, _ = self.get("/metrics-a.json")  # no export: OFFLINE, not a zero
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["source_ok"])
        self.assertIn("not found", json.loads(body)["error"])

    def test_config_and_other_files_are_never_served(self):
        for path in ("/config.json", "/collector_b.py", "/history-b.jsonl", "/../config.json"):
            status, body, _ = self.get(path)
            self.assertEqual(status, 404, path)
            self.assertNotIn(b"test-token", body)

    def test_forwarded_requests_need_the_token(self):
        fwd = {"X-Forwarded-For": "100.64.0.1"}
        self.assertEqual(self.get("/metrics-b.json", fwd)[0], 403)
        self.assertEqual(self.get("/metrics-b.json?t=wrong", fwd)[0], 403)
        self.assertEqual(self.get("/metrics-b.json?t=test-token", fwd)[0], 200)

    def test_busy_port_is_refused(self):
        with self.assertRaises(OSError):
            cb.Server(("127.0.0.1", self.port), cb.make_handler(self.col, cb.UsageTail({}, path="no-such-export.json"), "t"))

    def test_payload_carries_no_token(self):
        self.assertNotIn(b"test-token", self.get("/metrics-b.json")[1])
        self.assertNotIn(b"test-token", self.get("/")[1])


if __name__ == "__main__":
    unittest.main()
