import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import agents  # noqa: E402
import collector_b  # noqa: E402

MARKER = "SECRET-MESSAGE-TEXT-9931"


def usage_line(mid, inp, out, cw=0, cr=0, model="claude-test"):
    return json.dumps({"type": "assistant", "message": {
        "id": mid, "model": model, "content": [{"type": "text", "text": MARKER}],
        "usage": {"input_tokens": inp, "output_tokens": out,
                  "cache_creation_input_tokens": cw, "cache_read_input_tokens": cr}}}) + "\n"


def make_db(path):
    con = sqlite3.connect(path)
    con.execute("""create table usage_events (provider integer, event_key text, ts_ms integer,
        day text, model text, input integer, output integer, cache_w5m integer,
        cache_w1h integer, cache_read integer, session_id text, project text,
        primary key (provider, event_key))""")
    con.commit()
    return con


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.proj = self.root / "projects" / "p1"
        self.proj.mkdir(parents=True)
        self.db = self.root / "usage.db"
        self.con = make_db(self.db)
        self.addCleanup(self.con.close)
        self.procs = [{"pid": 9, "kind": "quotalis", "bytes": 0}]
        self.killed = []
        self.cfg = {"claude_projects_dir": str(self.root / "projects"),
                    "quotalis_db": str(self.db), "stale_after_s": 5, "poll_s": 1}

    def make(self, **kw):
        kw.setdefault("tap_reader", lambda: None)   # no live tap unless a test gives one
        return agents.Agents(self.cfg, proc_reader=lambda: list(self.procs),
                             killer=lambda pid: self.killed.append(pid) or True, **kw)

    @staticmethod
    def payload(dial):
        return json.loads(dial.payload_bytes)


class ClaudeTailTests(Base):
    def test_old_history_is_not_counted_and_new_lines_are(self):
        log = self.proj / "s.jsonl"
        log.write_text(usage_line("m0", 5000, 5000))
        a = self.make()
        a.step(1000.0)
        self.assertEqual(self.payload(a.claude)["dial"]["primary"]["value"], 0)
        with open(log, "a") as f:
            f.write(usage_line("m1", 10, 200, cw=300, cr=4000))
        a.step(1001.0)
        p = self.payload(a.claude)
        self.assertEqual(p["dial"]["primary"]["value"], 20)   # 200 output tokens over 10 s
        d = p["detail"]                                        # cache reads shown apart
        self.assertEqual(d["tokens_in_per_min"] + d["tokens_out_per_min"]
                         + d["cache_write_per_min"], 510)
        self.assertEqual(p["detail"]["cache_read_per_min"], 4000)
        self.assertEqual(p["dial"]["state"], "WARM")

    def test_repeated_message_id_counts_once_and_growth_counts_the_difference(self):
        log = self.proj / "s.jsonl"
        log.write_text("")
        a = self.make()
        a.step(1000.0)
        with open(log, "a") as f:
            f.write(usage_line("m1", 10, 100) + usage_line("m1", 10, 100))
            f.write(usage_line("m1", 10, 250))
        a.step(1001.0)
        self.assertEqual(self.payload(a.claude)["dial"]["primary"]["value"], 25)  # output only

    def test_half_written_line_waits_for_its_end(self):
        log = self.proj / "s.jsonl"
        log.write_text("")
        a = self.make()
        a.step(1000.0)
        line = usage_line("m1", 0, 100)
        with open(log, "a") as f:
            f.write(line[:40])
        a.step(1001.0)
        self.assertEqual(self.payload(a.claude)["dial"]["primary"]["value"], 0)
        with open(log, "a") as f:
            f.write(line[40:])
        a.step(1002.0)
        self.assertEqual(self.payload(a.claude)["dial"]["primary"]["value"], 10)

    def test_window_forgets_after_a_minute_and_states_follow_thresholds(self):
        log = self.proj / "s.jsonl"
        log.write_text("")
        a = self.make()
        a.step(1000.0)
        with open(log, "a") as f:
            f.write(usage_line("m1", 0, 2500))
        a.step(1001.0)
        p = self.payload(a.claude)
        self.assertEqual(p["dial"]["primary"]["value"], 250)
        self.assertEqual(p["dial"]["state"], "WARM")      # state follows the 10 s mean
        for t in range(1002, 1006):
            a.step(float(t))
        self.assertEqual(self.payload(a.claude)["dial"]["state"], "BURNING")
        with open(log, "a") as f:
            f.write(usage_line("m2", 0, 5000))
        for t in range(1006, 1011):
            a.step(float(t))
        self.assertEqual(self.payload(a.claude)["dial"]["state"], "REDLINE")
        for t in range(1060, 1071):
            a.step(float(t))
        p = self.payload(a.claude)
        self.assertEqual((p["dial"]["primary"]["value"], p["dial"]["state"]), (0, "IDLE"))

    def test_live_tap_drives_the_needle_in_tokens_per_second(self):
        snap = {"output_tokens_per_s": 143.6, "input_tokens_per_s": 53901, "active_requests": 1,
                "requests_total": 3, "upstream_errors": 0, "chars_per_token": 3.1,
                "last_request": {"model": "m-live", "first_token_s": 1.7, "input": 2,
                                 "cache_read": 0, "cache_write": 53899, "output": None,
                                 "output_est": 80, "done_s": None, "age_s": 2.0}}
        a = self.make(tap_reader=lambda: snap)
        a.step(1000.0)
        p = self.payload(a.claude)
        pr = p["dial"]["primary"]
        self.assertEqual((pr["value"], pr["unit"]), (144, "tokens/s"))
        self.assertEqual(p["dial"]["ring"]["value"], 53901)
        self.assertEqual(p["dial"]["state"], "WARM")
        self.assertEqual(p["detail"]["tap"]["last_request"]["first_token_s"], 1.7)
        self.assertEqual(p["detail"]["model"], "m-live")
        snap["output_tokens_per_s"] = 600
        a.step(1001.0)
        self.assertNotEqual(self.payload(a.claude)["dial"]["state"], "REDLINE")  # one sample
        for t in range(1002, 1012):
            a.step(float(t))
        self.assertEqual(self.payload(a.claude)["dial"]["state"], "REDLINE")

    def test_quiet_tap_falls_back_to_the_stream_estimate(self):
        snap = {"output_tokens_per_s": 0, "input_tokens_per_s": 0, "active_requests": 0,
                "requests_total": 1, "upstream_errors": 0, "chars_per_token": 4.0,
                "last_request": {"model": "m", "age_s": 300.0}}
        a = self.make(tap_reader=lambda: snap)
        self.procs[:] = [{"pid": 7, "kind": "claude", "bytes": 1000, "ops": 100}]
        a.step(1000.0)
        self.procs[:] = [{"pid": 7, "kind": "claude", "bytes": 1500, "ops": 101}]
        a.step(1001.0)          # one write: a log line, not a stream
        self.assertEqual(self.payload(a.claude)["dial"]["primary"]["value"], 0)
        self.procs[:] = [{"pid": 7, "kind": "claude", "bytes": 1500, "ops": 101}]
        a.step(1002.0)
        a.step(1003.0)
        self.procs[:] = [{"pid": 7, "kind": "claude", "bytes": 9000, "ops": 141}]
        a.step(1004.0)          # 40 writes in a second: an answer streaming
        p = self.payload(a.claude)
        self.assertEqual(p["dial"]["primary"]["value"], 120)   # 40 x first guess of 3.0
        self.assertEqual(p["dial"]["primary"]["unit"], "tokens/s")
        self.assertIn("estimated", p["dial"]["primary"]["label"])
        self.assertEqual(p["detail"]["stream"]["wait_s"], 3.0)
        self.assertFalse(p["detail"]["stream"]["learned"])
        a.claude.add(1004.5, (10, 410, 0, 0))   # the logs give the exact count
        self.procs[:] = [{"pid": 7, "kind": "claude", "bytes": 9900, "ops": 151}]
        a.step(1005.0)
        p = self.payload(a.claude)
        self.assertTrue(p["detail"]["stream"]["learned"])
        self.assertEqual(p["dial"]["primary"]["value"], round(10 * 410 / 50))  # 410 over 40+10

    def test_log_fallback_counts_output_tokens_only(self):
        a = self.make()
        a.claude.add(999.0, (2, 104, 54000, 300000))   # big cached prompt, short answer
        a.step(1000.0)
        p = self.payload(a.claude)
        self.assertEqual(p["dial"]["primary"]["value"], 10)   # 104 output over 10 s
        self.assertIn("output tokens per second", p["dial"]["primary"]["label"])

    def test_tap_idle_but_request_in_flight_is_warm(self):
        snap = {"output_tokens_per_s": 0, "input_tokens_per_s": 0, "active_requests": 1,
                "requests_total": 1, "upstream_errors": 0, "chars_per_token": 4.0,
                "last_request": None}
        a = self.make(tap_reader=lambda: snap)
        a.step(1000.0)
        self.assertEqual(self.payload(a.claude)["dial"]["state"], "WARM")

    def test_second_dial_is_the_other_time_base(self):
        a = self.make()
        a.claude.add(999.0, (100, 50, 25, 9000))
        a.step(1000.0)
        d2 = self.payload(a.claude)["detail"]["second_dial"]
        self.assertEqual((d2["value"], d2["unit"], d2["state"]), (175, "tokens/min", "WARM"))
        self.assertEqual(d2["spark"], [175])
        a.codex.add(999.0, (100, 3000, 0, 0))
        a.step(1001.0)
        d2 = self.payload(a.codex)["detail"]["second_dial"]
        self.assertEqual((d2["value"], d2["unit"], d2["state"]), (300, "tokens/s", "BURNING"))

    def test_first_token_wait_counts_up_from_the_request_line_then_holds(self):
        log = self.proj / "s.jsonl"
        log.write_text("")
        a = self.make()
        self.procs[:] = [{"pid": 7, "kind": "claude", "bytes": 1000, "ops": 100}]
        a.step(1000.0)
        self.assertIsNone(self.payload(a.claude)["detail"]["ttft"]["value_s"])
        with open(log, "a") as f:
            f.write('{"type":"api-request","sessionId":"s","body":"' + MARKER + '"}\n')
        a.step(1001.0)
        a.step(1004.0)
        t = self.payload(a.claude)["detail"]["ttft"]
        self.assertEqual((t["waiting"], t["value_s"], t["state"]), (True, 3.0, "WARM"))
        a.step(1010.0)
        self.assertEqual(self.payload(a.claude)["detail"]["ttft"]["state"], "BURNING")
        self.procs[:] = [{"pid": 7, "kind": "claude", "bytes": 9000, "ops": 140}]
        a.step(1011.0)          # the answer starts streaming
        a.step(1015.0)
        t = self.payload(a.claude)["detail"]["ttft"]
        self.assertEqual((t["waiting"], t["value_s"], t["age_s"], t["state"]),
                         (False, 10.0, 4, "IDLE"))
        self.assertEqual(t["spark"], [10.0])
        self.assertIn("estimated", t["basis"])
        self.assertNotIn(MARKER.encode(), a.claude.payload_bytes)

    def test_first_token_wait_is_exact_from_the_tap(self):
        snap = {"output_tokens_per_s": 0, "input_tokens_per_s": 0, "active_requests": 1,
                "requests_total": 1, "upstream_errors": 0, "chars_per_token": 4.0,
                "last_request": {"id": 1, "model": "m", "first_token_s": None, "done_s": None,
                                 "age_s": 2.5}}
        a = self.make(tap_reader=lambda: snap)
        a.step(1000.0)
        t = self.payload(a.claude)["detail"]["ttft"]
        self.assertEqual((t["waiting"], t["value_s"]), (True, 2.5))
        snap["last_request"].update(first_token_s=3.1, age_s=4.0)
        a.step(1001.0)
        a.step(1002.0)
        t = self.payload(a.claude)["detail"]["ttft"]
        self.assertEqual((t["waiting"], t["value_s"], t["spark"]), (False, 3.1, [3.1]))

    def test_message_text_never_reaches_the_payload(self):
        log = self.proj / "s.jsonl"
        log.write_text("")
        a = self.make()
        a.step(1000.0)
        with open(log, "a") as f:
            f.write(usage_line("m1", 1, 1))
        a.step(1001.0)
        self.assertNotIn(MARKER.encode(), a.claude.payload_bytes)

    def test_missing_folder_shows_no_number_not_zero(self):
        self.cfg["claude_projects_dir"] = str(self.root / "nope")
        a = self.make()
        a.step(1000.0)
        p = self.payload(a.claude)
        self.assertIsNone(p["dial"]["primary"]["value"])
        self.assertIn("not found", p["detail"]["token_error"])


class QuotalisTests(Base):
    def add(self, key, ts, inp, out, provider=1):
        self.con.execute("insert or replace into usage_events values (?,?,?,?,?,?,?,?,?,?,?,?)",
                         (provider, key, int(ts * 1000), "d", "gpt-test", inp, out, 0, 0, 7,
                          "sess", "proj"))
        self.con.commit()

    def test_codex_effort_comes_from_quotalis_file_state(self):
        a = self.make()
        self.add("k0", 999.0, 10, 5)
        a.step(1000.0)
        self.assertIsNone(self.payload(a.codex)["detail"]["effort"])   # no cursor table yet
        self.con.execute("create table file_cursors (path text primary key, length integer, "
                         "mtime_ticks integer, offset integer, state text)")
        self.con.execute("insert into file_cursors values ('other', 1, 9, 1, ?)",
                         (json.dumps({"sessionId": "someone-else", "effort": "low"}),))
        self.con.execute("insert into file_cursors values ('mine', 1, 5, 1, ?)",
                         (json.dumps({"sessionId": "sess", "effort": "high"}),))
        self.add("k1", 1000.5, 100, 50)
        a.step(1001.0)
        d = self.payload(a.codex)["detail"]
        self.assertEqual((d["effort"], d["effort_tier"]), ("high", 2))

    def test_codex_rows_are_counted_and_other_providers_ignored(self):
        self.add("old", 100.0, 9999, 9999)
        self.add("claude-row", 1000.0, 5000, 5000, provider=0)
        a = self.make()
        a.step(1000.0)
        self.assertEqual(self.payload(a.codex)["dial"]["primary"]["value"], 0)
        self.add("k1", 1000.5, 100, 50)
        a.step(1001.0)
        p = self.payload(a.codex)
        self.assertEqual(p["dial"]["primary"]["value"], 150)
        self.assertEqual(p["detail"]["newest_row_age_s"], 0.5)
        a.step(1002.0)
        self.assertEqual(self.payload(a.codex)["dial"]["primary"]["value"], 150)

    def test_database_is_opened_read_only(self):
        uri = "file:" + self.db.as_posix() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True)
        self.addCleanup(con.close)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("delete from usage_events")
        src = Path(agents.__file__).read_text(encoding="utf-8")
        self.assertIn("?mode=ro", src)
        self.assertEqual(src.count("sqlite3.connect("), 1)

    def test_quotalis_not_running_shows_no_number(self):
        self.procs = []
        a = self.make()
        a.step(1000.0)
        p = self.payload(a.codex)
        self.assertIsNone(p["dial"]["primary"]["value"])
        self.assertIn("Quotalis is not running", p["detail"]["token_error"])


class KillTests(Base):
    def test_only_busy_processes_are_ended(self):
        self.procs += [{"pid": 1, "kind": "claude", "bytes": 0},
                       {"pid": 2, "kind": "claude", "bytes": 0},
                       {"pid": 3, "kind": "codex", "bytes": 0}]
        a = self.make()
        a.step(1000.0)
        for i in range(1, 4):             # pid 1 streams for three seconds
            self.procs[1]["bytes"] = 6000 * i
            if i == 1:
                self.procs[2]["bytes"] = 900000   # pid 2: one lone burst, not busy
            a.step(1000.0 + i)
        self.assertEqual(self.payload(a.claude)["detail"]["active_pids"], [1])
        self.assertEqual(self.payload(a.claude)["dial"]["state"], "WARM")
        r = a.kill("claude")
        self.assertEqual((r["killed"], self.killed), (1, [1]))

    def test_nothing_busy_ends_nothing(self):
        self.procs += [{"pid": 1, "kind": "claude", "bytes": 100}]
        a = self.make()
        a.step(1000.0)
        a.step(1001.0)
        r = a.kill("claude")
        self.assertEqual((r["killed"], self.killed), (0, []))
        self.assertIn("nothing in flight", r["message"])

    def test_busy_state_expires(self):
        self.procs += [{"pid": 1, "kind": "codex", "bytes": 0}]
        a = self.make()
        a.step(1000.0)
        for i in range(1, 4):
            self.procs[1]["bytes"] = 6000 * i
            a.step(1000.0 + i)
        self.assertEqual(a.watch["codex"].active(), [1])
        for t in range(1004, 1012):
            a.step(float(t))
        self.assertEqual(a.kill("codex")["killed"], 0)

    def test_unknown_target_and_local(self):
        a = self.make(abort_local=lambda: 2)
        self.assertFalse(a.kill("everything")["ok"])
        self.assertEqual(a.kill("local")["killed"], 2)
        self.assertFalse(self.make(abort_local=lambda: None).kill("local")["ok"])


class KillEndpointTests(Base):
    def setUp(self):
        super().setUp()
        self.agents = self.make()
        self.calls = []
        self.agents.kill = lambda target: self.calls.append(target) or {"ok": True}
        col = type("C", (), {"payload_bytes": b"{}"})()
        self.srv = collector_b.Server(("127.0.0.1", 0), collector_b.make_handler(
            col, collector_b.VpsRelay(""), "tok", self.agents))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def post(self, path, headers):
        req = urllib.request.Request(self.base + path, data=b"", method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_button_header_is_required(self):
        self.assertEqual(self.post("/kill?target=codex", {}), 403)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.post("/kill?target=codex", {"X-Gauge-Kill": "1"}), 200)
        self.assertEqual(self.calls, ["codex"])

    def test_foreign_origin_is_refused(self):
        h = {"X-Gauge-Kill": "1", "Origin": "https://example.com"}
        self.assertEqual(self.post("/kill?target=codex", h), 403)
        self.assertEqual(self.calls, [])

    def test_forwarded_request_needs_the_token(self):
        h = {"X-Gauge-Kill": "1", "X-Forwarded-For": "100.1.2.3"}
        self.assertEqual(self.post("/kill?target=codex", h), 403)
        self.assertEqual(self.post("/kill?target=codex&t=tok", h), 200)

    def test_agent_metrics_are_served(self):
        self.agents.step = None
        with urllib.request.urlopen(self.base + "/metrics-c.json", timeout=3) as r:
            self.assertEqual(r.status, 200)


class SeedModelTest(unittest.TestCase):
    def test_start_reads_the_model_from_the_log_tail_without_counting_it(self):
        import tempfile, time as _t, datetime as _dt
        with tempfile.TemporaryDirectory() as root:
            now = _t.time()
            stamp = _dt.datetime.fromtimestamp(now - 30, _dt.timezone.utc).isoformat().replace("+00:00", "Z")
            line = json.dumps({"type": "assistant", "timestamp": stamp, "effort": "high", "message": {
                "id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 5, "output_tokens": 700}}})
            with open(os.path.join(root, "s.jsonl"), "w") as f:
                f.write("first line\n" + line + "\n")
            tail = agents.ClaudeTail(root)
            self.assertEqual(tail.poll(now), [])                 # history is not burn
            main, _age, models = tail.tally.report(now)
            self.assertEqual(main, "claude-opus-5-5")
            self.assertEqual(tail.effort["claude-opus-5-5"], "high")


class ModelTallyTest(unittest.TestCase):
    def test_tier_comes_from_the_first_fragment_in_the_name(self):
        tiers = agents.DEFAULTS["model_tiers"]
        self.assertEqual(agents.model_tier("claude-fable-5-2", tiers), 1)
        self.assertEqual(agents.model_tier("claude-opus-5-5", tiers), 2)
        self.assertEqual(agents.model_tier("claude-sonnet-5-5", tiers), 3)
        self.assertEqual(agents.model_tier("claude-haiku-5-5", tiers), 4)
        self.assertEqual(agents.model_tier("gpt-6.1-sol", tiers), 1)
        self.assertEqual(agents.model_tier("gpt-6-astra", tiers), 2)
        self.assertEqual(agents.model_tier("gpt-6-luna", tiers), 3)
        self.assertIsNone(agents.model_tier("codex-auto-review", tiers))
        self.assertIsNone(agents.model_tier(None, tiers))

    def test_main_model_is_the_one_with_most_output(self):
        t = agents.ModelTally()
        t.add(100, "big-model", 900)
        t.add(150, "small-model", 100)      # newer, but a small background call
        t.add(160, "<synthetic>", 50)       # not a model
        main, age, models = t.report(160)
        self.assertEqual(main, "big-model")
        self.assertEqual(age, 60)
        self.assertEqual([m["model"] for m in models], ["big-model", "small-model"])
        self.assertEqual([m["share_pct"] for m in models], [90, 10])

    def test_title_follows_a_switch_but_not_a_small_background_call(self):
        t = agents.ModelTally()
        t.add(100, "luna", 5000)
        self.assertEqual(t.report(110)[0], "luna")
        t.add(300, "tiny-helper", 40)            # alone in the last two minutes, but small
        self.assertEqual(t.report(310)[0], "luna")
        t.add(320, "sol", 600)                   # a real answer on another model
        main, _age, models = t.report(330)
        self.assertEqual(main, "sol")
        self.assertEqual(models[0]["model"], "sol")   # the title's model is listed first
        self.assertEqual(t.report(900)[0], "sol")     # luna left the window; sol stays

    def test_last_model_is_kept_after_the_window_empties(self):
        t = agents.ModelTally()
        t.add(100, "big-model", 900)
        main, age, models = t.report(100 + 3600)
        self.assertEqual((main, age, models), ("big-model", 3600, []))
        self.assertEqual(agents.ModelTally().report(5), (None, None, []))


if __name__ == "__main__":
    unittest.main()
