"""Tests for the VPS collector, run against a fixture SQLite database."""
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vps"))
import collector_a as ca  # noqa: E402

COLUMNS = """session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
    billing_mode TEXT, task TEXT, api_call_count INTEGER, input_tokens INTEGER,
    output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER,
    reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL,
    cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL"""
T0 = 1_794_387_000.0


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "state.db"
        con = sqlite3.connect(self.db)
        con.execute(f"CREATE TABLE session_model_usage ({COLUMNS})")
        con.commit()
        con.close()
        self.wallet = self.dir / "nous-spend.json"
        self.wallet.write_text(json.dumps([
            {"ts": T0 - 900, "period_used": 1.0, "credits_remaining": 7.5, "purchased": 10,
             "period_end": "2026-11-22T03:24:36Z"},
            {"ts": T0 - 10, "period_used": 1.4, "credits_remaining": 7.1, "purchased": 10,
             "period_end": "2026-11-22T03:24:36Z"}]))
        self.cfg = json.loads((ROOT / "vps" / "config.json").read_text())
        self.cfg.update(db_path=str(self.db), wallet_path=str(self.wallet))
        self.put("s1", "model-x", "chat", out=1000, inp=5000, cost=0.10, last_seen=T0 - 3600)

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, sid, model, task, out=0, inp=0, cache=0, calls=1, cost=0.0, last_seen=T0):
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM session_model_usage WHERE session_id=? AND model=? AND task=?",
                    (sid, model, task))
        con.execute(
            "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, model, "nous", "https://example.invalid", "metered", task, calls, inp, out,
             cache, 0, 0, cost, 0.0, "estimated", "provider_models_api", T0 - 7200, last_seen))
        con.commit()
        con.close()


class ConfigTest(Base):
    def test_every_key_present(self):  # acceptance step 1
        for k in ("sample_s", "wallet_s", "burn_usd_per_hr", "redline_usd_per_hr",
                  "low_credits_usd", "db_path", "wallet_path", "stale_after_s"):
            self.assertIn(k, self.cfg)
        self.assertEqual((self.cfg["burn_usd_per_hr"], self.cfg["redline_usd_per_hr"],
                          self.cfg["low_credits_usd"]), (3.0, 8.0, 2.0))


class ReadOnlyTest(Base):
    def test_write_attempt_raises(self):  # acceptance step 3
        con = ca.open_ro(self.db)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM session_model_usage").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("UPDATE session_model_usage SET output_tokens = 0")
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("CREATE TABLE x (y)")
        finally:
            con.close()
        self.assertEqual(ca.read_rows(self.db)[("s1", "model-x", "chat")]["output_tokens"], 1000)

    def test_read_only_open_does_not_create_a_missing_db(self):
        missing = self.dir / "nope.db"
        with self.assertRaises(sqlite3.OperationalError):
            ca.open_ro(missing).execute("SELECT 1 FROM session_model_usage")
        self.assertFalse(missing.exists())


class RateTest(Base):
    def test_idle_is_zero_and_small(self):  # acceptance step 2
        c = ca.Collector(self.cfg)
        for i in range(13):  # 60 s of 5 s samples
            p = c.sample(T0 + 5 * i)
        self.assertEqual(p["dial"]["primary"]["value"], 0)
        self.assertEqual(p["detail"]["tokens_out_per_min"], 0)
        self.assertEqual(p["dial"]["state"], "IDLE")
        self.assertEqual(p["detail"]["active_sessions"], 0)
        self.assertLess(len(json.dumps(p)), 4096)

    def test_first_sample_never_counts_existing_totals(self):
        p = ca.Collector(self.cfg).sample(T0)
        self.assertEqual(p["dial"]["primary"]["value"], 0)

    def test_rate_maths_match_a_manual_delta(self):  # acceptance step 4
        c = ca.Collector(self.cfg)
        c.sample(T0)
        self.put("s1", "model-x", "chat", out=1600, inp=5300, cost=0.15, calls=3, last_seen=T0 + 4)
        p = c.sample(T0 + 5)
        self.assertAlmostEqual(p["detail"]["tokens_out_per_min"], 600 * 60 / 5)
        self.assertAlmostEqual(p["detail"]["tokens_in_per_min"], 300 * 60 / 5)
        self.assertAlmostEqual(p["detail"]["api_calls_per_min"], 2 * 60 / 5)
        self.assertAlmostEqual(p["dial"]["primary"]["value"], 0.05 * 3600 / 5)
        self.assertEqual(p["detail"]["active_sessions"], 1)  # step 5
        self.assertEqual(p["detail"]["since_start"]["output_tokens"], 600)
        self.assertAlmostEqual(p["detail"]["since_start"]["estimated_cost_usd"], 0.05)
        self.assertEqual(p["detail"]["cost_basis"], "estimated")
        self.assertEqual(p["detail"]["models"][0]["model"], "model-x")
        for i in range(2, 7):
            p = c.sample(T0 + 5 * i)
        self.assertAlmostEqual(p["detail"]["tokens_out_per_min"], 600 * 60 / 30)  # 30 s window
        p = c.sample(T0 + 35)
        self.assertEqual(p["dial"]["primary"]["value"], 0)  # the burst has left the window

    def test_states_by_lowering_thresholds(self):  # acceptance step 18, VPS side
        self.put("s1", "model-x", "chat", out=1000, cost=0.10)
        seen = []
        for burn, red in ((1000, 2000), (10, 2000), (10, 20)):
            cfg = dict(self.cfg, burn_usd_per_hr=burn, redline_usd_per_hr=red)
            c = ca.Collector(cfg)
            c.sample(T0)
            self.put("s1", "model-x", "chat", out=1100, cost=0.10 + 0.05)
            seen.append(c.sample(T0 + 5)["dial"]["state"])  # 36 USD/hr estimated
            self.put("s1", "model-x", "chat", out=1000, cost=0.10)
        self.assertEqual(seen, ["WARM", "BURNING", "REDLINE"])

    def test_counter_drop_is_a_reset_not_a_negative(self):
        c = ca.Collector(self.cfg)
        c.sample(T0)
        self.put("s1", "model-x", "chat", out=10, inp=10, cost=0.01)
        p = c.sample(T0 + 5)
        self.assertEqual(p["dial"]["primary"]["value"], 0)
        self.assertEqual(p["detail"]["tokens_out_per_min"], 0)

    def test_new_row_counts_and_vanished_row_does_not(self):
        c = ca.Collector(self.cfg)
        c.sample(T0)
        self.put("s2", "model-y", "chat", out=50, cost=0.01, last_seen=T0 + 5)
        con = sqlite3.connect(self.db)
        con.execute("DELETE FROM session_model_usage WHERE session_id='s1'")
        con.commit()
        con.close()
        p = c.sample(T0 + 5)
        self.assertAlmostEqual(p["detail"]["tokens_out_per_min"], 50 * 60 / 5)

    def test_last_seen_as_iso_text(self):
        self.assertAlmostEqual(ca.to_epoch("2026-11-10T12:00:00Z"),
                               ca.to_epoch("2026-11-10 12:00:00"))
        self.assertEqual(ca.to_epoch(1794387000000), 1794387000.0)
        self.assertIsNone(ca.to_epoch("not a time"))


class FailureTest(Base):
    def test_db_error_keeps_last_good_payload(self):
        c = ca.Collector(self.cfg)
        good = c.sample(T0)
        c.cfg["db_path"] = str(self.dir / "gone.db")
        bad = c.sample(T0 + 5)
        self.assertIn("state.db", bad["error"])
        self.assertEqual((bad["ts"], bad["seq"]), (good["ts"], good["seq"]))  # page goes STALE

    def test_db_error_from_the_start_is_offline_not_zero(self):
        cfg = dict(self.cfg, db_path=str(self.dir / "gone.db"))
        p = ca.Collector(cfg).sample(T0)
        self.assertFalse(p["source_ok"])
        self.assertIsNone(p["dial"]["primary"]["value"])
        self.assertTrue(p["error"])

    def test_schema_change_is_reported(self):
        con = sqlite3.connect(self.db)
        con.execute("ALTER TABLE session_model_usage RENAME COLUMN output_tokens TO out_tok")
        con.commit()
        con.close()
        p = ca.Collector(self.cfg).sample(T0)
        self.assertIn("schema changed", p["error"])

    def test_missing_wallet_does_not_stop_the_needle(self):
        self.wallet.unlink()
        p = ca.Collector(self.cfg).sample(T0)
        self.assertTrue(p["source_ok"])
        self.assertIsNone(p["dial"]["fuel"]["value"])


class WalletTest(Base):
    def test_newest_snapshot_and_low_fuel(self):
        p = ca.Collector(self.cfg).sample(T0)
        self.assertEqual(p["dial"]["fuel"]["value"], 7.1)
        self.assertEqual(p["detail"]["period_end"], "2026-11-22T03:24:36Z")
        self.assertEqual(p["detail"]["fuel_age_s"], 10)
        self.assertFalse(p["detail"]["low_fuel"])
        self.wallet.write_text(json.dumps({"history": [{"ts": T0, "credits_remaining": 1.5}]}))
        self.assertTrue(ca.Collector(self.cfg).sample(T0)["detail"]["low_fuel"])


class PayloadTest(Base):
    def test_contract_keys_size_and_no_secrets(self):
        c = ca.Collector(self.cfg)
        c.sample(T0)
        for i in range(8):
            self.put(f"s{i}", f"provider/some-long-model-name-{i}", "chat", out=10 ** 6,
                     inp=10 ** 7, cache=10 ** 8, cost=12.3456789, last_seen=T0)
        for i in range(1, 62):
            p = c.sample(T0 + 5 * i)
        for k in ("ts", "seq", "host", "source_ok", "stale_after_s", "dial", "detail", "error"):
            self.assertIn(k, p)
        for k in ("primary", "ring", "fuel", "state"):
            self.assertIn(k, p["dial"])
        text = json.dumps(p)
        self.assertLess(len(text), 4096)
        self.assertEqual(len(p["detail"]["spark"]), 60)
        for word in ("example.invalid", "billing", "session_id", "s1", "key", "token\""):
            self.assertNotIn(word, text.replace("tokens", ""))

    def test_source_makes_no_network_or_model_calls(self):
        src = (ROOT / "vps" / "collector_a.py").read_text()
        for bad in ("urllib.request", "http.client", "requests", "anthropic", "openai", "socket."):
            self.assertNotIn(bad, src)


class AtomicAndLockTest(Base):
    def test_reader_never_sees_a_partial_file(self):  # acceptance step 8
        path = self.dir / "metrics-a.json"
        payload = json.dumps({"pad": "x" * 3000, "end": True}).encode()
        ca.atomic_write(path, payload)
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                try:
                    data = path.read_bytes()
                except PermissionError:
                    continue
                if data != payload:
                    bad.append(len(data))

        t = threading.Thread(target=reader)
        t.start()
        for _ in range(300):
            ca.atomic_write(path, payload)
        stop.set()
        t.join()
        self.assertEqual(bad, [])

    def test_second_instance_is_refused_by_the_lock(self):  # acceptance step 7
        first = ca.acquire_lock(self.dir / "collector-a.lock")
        self.assertIsNotNone(first)
        self.assertIsNone(ca.acquire_lock(self.dir / "collector-a.lock"))
        first.close()
        again = ca.acquire_lock(self.dir / "collector-a.lock")
        self.assertIsNotNone(again)
        again.close()

    def test_history_is_line_readable_after_a_torn_write(self):  # acceptance step 19
        h = ca.History(self.dir / "history-a.jsonl", 30)
        c = ca.Collector(self.cfg)
        h.add(c.sample(T0), T0)
        with open(h.path, "a") as f:
            f.write('{"ts": 17943')  # the collector was killed mid-line
        lines = [ln for ln in h.path.read_text().splitlines()]
        self.assertEqual(json.loads(lines[0])["state"], "IDLE")
        h.trim(T0 + 40 * 86400)
        self.assertEqual(h.path.read_text(), "")


if __name__ == "__main__":
    unittest.main()
