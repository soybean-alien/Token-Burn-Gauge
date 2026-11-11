"""Token-burn gauge: VPS collector (Hermes side).

Every few seconds: read session_model_usage from state.db (read-only), diff it
against the previous sample, and write metrics-a.json in the shared contract
shape. No model calls; counters and timestamps only; the database is never
written to.
"""
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

HERE = Path(__file__).resolve().parent
KEY = ("session_id", "model", "task")
FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
          "reasoning_tokens", "api_call_count", "estimated_cost_usd")
ACTIVE_S = 60


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def atomic_write(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(data)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # only on Windows, when a reader has the file open
            time.sleep(0.05)


def open_ro(db_path):
    """Read-only by URI: any write on this connection raises OperationalError."""
    uri = "file:" + quote(Path(db_path).as_posix(), safe="/:") + "?mode=ro"
    return sqlite3.connect(uri, uri=True, timeout=2)


def read_rows(db_path):
    conn = open_ro(db_path)
    try:
        cur = conn.execute("SELECT * FROM session_model_usage")
        cols = [c[0] for c in cur.description]
        missing = [c for c in KEY + FIELDS + ("last_seen",) if c not in cols]
        if missing:
            raise sqlite3.DatabaseError(f"schema changed: missing columns {missing}")
        rows = {}
        for r in cur:
            d = dict(zip(cols, r))
            rows[tuple(d[k] for k in KEY)] = d
        return rows
    finally:
        conn.close()


def to_epoch(v):
    """last_seen as epoch seconds, whether stored as a number or as ISO text."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 1e11 else float(v)
    s = str(v).strip()
    try:
        return to_epoch(float(s))
    except ValueError:
        pass
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def diff(prev, cur):
    """Positive deltas only, per row. A dropped counter is a reset, never a negative.

    A row that was not there last sample counts in full: it was created since then.
    """
    out = []
    for key, row in cur.items():
        old = prev.get(key)
        d = {}
        for f in FIELDS:
            new = row.get(f) or 0
            d[f] = new if old is None else max(0, new - (old.get(f) or 0))
        if any(d.values()):
            out.append((row["model"], d))
    return out


def read_wallet_file(path):
    """Newest snapshot from the existing wallet reader's history file."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "credits_remaining" not in data:
        data = next((v for v in data.values() if isinstance(v, list) and v), [])
    snap = data[-1] if isinstance(data, list) else data
    return {"credits_remaining": snap["credits_remaining"],
            "period_end": snap.get("period_end"), "ts": to_epoch(snap.get("ts"))}


def read_wallet_command(cmd):
    """Optional: a command owned by Hermes that prints one JSON snapshot on stdout."""
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20)
    snap = json.loads(out.stdout)
    return {"credits_remaining": snap["credits_remaining"],
            "period_end": snap.get("period_end"), "ts": time.time()}


class Collector:
    def __init__(self, cfg):
        self.cfg = cfg
        self.prev = None
        self.seq = 0
        self.start = None
        self.window = deque()   # (time, [(model, deltas)])
        self.spark = deque(maxlen=60)
        self.totals = {"output_tokens": 0, "estimated_cost_usd": 0.0}
        self.wallet = None
        self.wallet_checked = 0.0
        self.last_payload = None

    def _wallet(self, now):
        cfg = self.cfg
        if now - self.wallet_checked < cfg["wallet_s"]:
            return
        self.wallet_checked = now
        try:
            if cfg.get("wallet_command"):
                self.wallet = read_wallet_command(cfg["wallet_command"])
            else:
                self.wallet = read_wallet_file(cfg["wallet_path"])
        except Exception as e:  # the balance is a fuel level; losing it must not stop the needle
            log(f"wallet read failed: {type(e).__name__}: {e}")

    def sample(self, now=None):
        now = time.time() if now is None else now
        cfg = self.cfg
        try:
            cur = read_rows(cfg["db_path"])
        except sqlite3.Error as e:
            return self._failed(f"state.db: {e}", now)
        if self.start is None:
            self.start = now
        deltas = diff(self.prev, cur) if self.prev is not None else []
        self.prev = cur
        for _, d in deltas:
            self.totals["output_tokens"] += d["output_tokens"]
            self.totals["estimated_cost_usd"] += d["estimated_cost_usd"]
        win = cfg["rate_window_s"]
        self.window.append((now, deltas))
        while self.window and self.window[0][0] <= now - win:
            self.window.popleft()
        span = max(cfg["sample_s"], min(win, now - self.start))

        total = dict.fromkeys(FIELDS, 0)
        models = {}
        for _, ds in self.window:
            for model, d in ds:
                m = models.setdefault(model, {"out": 0, "usd": 0.0})
                m["out"] += d["output_tokens"]
                m["usd"] += d["estimated_cost_usd"]
                for f in FIELDS:
                    total[f] += d[f]
        per_min = {f: total[f] * 60 / span for f in FIELDS}
        usd_hr = total["estimated_cost_usd"] * 3600 / span
        active = {k[0] for k, r in cur.items()
                  if (to_epoch(r.get("last_seen")) or 0) >= now - ACTIVE_S}

        if usd_hr >= cfg["redline_usd_per_hr"]:
            state = "REDLINE"
        elif usd_hr >= cfg["burn_usd_per_hr"]:
            state = "BURNING"
        elif any(total.values()):
            state = "WARM"
        else:
            state = "IDLE"

        self._wallet(now)
        w = self.wallet or {}
        credits = w.get("credits_remaining")
        self.spark.append(round(usd_hr, 4))
        self.seq += 1
        top = sorted(models.items(), key=lambda kv: -kv[1]["usd"])[:5]
        payload = {
            "ts": now, "seq": self.seq, "host": "vps", "source_ok": True,
            "stale_after_s": cfg["stale_after_s"], "poll_s": 2,
            "dial": {
                "primary": {"label": "USD per hour", "value": usd_hr, "unit": "$/hr",
                            "scale_max": cfg["scale_max_usd_per_hr"], "log": True,
                            "redline": cfg["redline_usd_per_hr"]},
                "ring": {"label": "tokens per minute",
                         "value": per_min["input_tokens"] + per_min["output_tokens"]},
                "fuel": {"label": "credits remaining", "value": credits, "of": None},
                "state": state,
            },
            "detail": {
                "tokens_in_per_min": per_min["input_tokens"],
                "tokens_out_per_min": per_min["output_tokens"],
                "cache_read_per_min": per_min["cache_read_tokens"],
                "cache_write_per_min": per_min["cache_write_tokens"],
                "reasoning_per_min": per_min["reasoning_tokens"],
                "api_calls_per_min": per_min["api_call_count"],
                "active_sessions": len(active),
                "models": [{"model": m, "out_per_min": v["out"] * 60 / span,
                            "usd_per_hr": v["usd"] * 3600 / span} for m, v in top],
                "period_end": w.get("period_end"),
                "cost_basis": "estimated",
                "low_fuel": credits is not None and credits < cfg["low_credits_usd"],
                "fuel_age_s": round(now - w["ts"]) if w.get("ts") else None,
                "rate_window_s": span,
                "since_start": {"s": round(now - self.start, 1), **self.totals},
                "spark": list(self.spark),
            },
            "error": None,
        }
        self.last_payload = payload
        return payload

    def _failed(self, msg, now):
        """Keep the last good payload (same ts and seq, so the page goes STALE) and say why."""
        if self.last_payload is not None:
            return {**self.last_payload, "error": msg}
        return {"ts": now, "seq": 0, "host": "vps", "source_ok": False,
                "stale_after_s": self.cfg["stale_after_s"], "poll_s": 2,
                "dial": {"primary": {"label": "USD per hour", "value": None, "unit": "$/hr",
                                     "scale_max": self.cfg["scale_max_usd_per_hr"], "log": True},
                         "ring": {"label": "tokens per minute", "value": None},
                         "fuel": {"label": "credits remaining", "value": None, "of": None},
                         "state": "IDLE"},
                "detail": {"cost_basis": "estimated"}, "error": msg}


class History:
    """Append-only JSONL: every sample while active, one a minute while idle."""

    def __init__(self, path, days):
        self.path, self.days = Path(path), days
        self.last_write = 0.0
        self.last_trim = 0.0

    def trim(self, now):
        self.last_trim = now
        if not self.path.exists():
            return
        cutoff = now - self.days * 86400
        keep = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                try:
                    if json.loads(line)["ts"] >= cutoff:
                        keep.append(line)
                except (ValueError, KeyError, TypeError):
                    continue
        atomic_write(self.path, "".join(keep).encode())

    def add(self, p, now):
        if now - self.last_trim > 86400:
            self.trim(now)
        if p["error"] or (p["dial"]["state"] == "IDLE" and now - self.last_write < 60):
            return
        self.last_write = now
        x = p["detail"]
        line = {"ts": round(p["ts"], 1), "usd_hr": round(p["dial"]["primary"]["value"], 4),
                "in": round(x["tokens_in_per_min"]), "out": round(x["tokens_out_per_min"]),
                "cache_read": round(x["cache_read_per_min"]),
                "calls": round(x["api_calls_per_min"], 1),
                "credits": p["dial"]["fuel"]["value"], "state": p["dial"]["state"]}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")


def acquire_lock(path):
    """Single instance. Returns the open lock file, or None if another copy holds it."""
    f = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    FILES = {"/": ("gauge.html", "text/html; charset=utf-8"),
             "/gauge.html": ("gauge.html", "text/html; charset=utf-8"),
             "/metrics-a.json": ("metrics-a.json", "application/json")}

    def log_message(self, *a):
        pass

    def do_GET(self):
        name, ctype = self.FILES.get(urlsplit(self.path).path, (None, None))
        try:
            body = (HERE / name).read_bytes() if name else b'{"error": "not found"}'
            status = 200 if name else 404
        except OSError:
            body, status = b'{"error": "not written yet"}', 503
        self.send_response(status)
        self.send_header("Content-Type", ctype or "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    lock = acquire_lock(HERE / "collector-a.lock")
    if lock is None:
        return  # another copy is running; the cron entry relies on this
    if cfg.get("serve_port"):
        try:
            server = Server(("127.0.0.1", cfg["serve_port"]), Handler)
        except OSError as e:
            sys.exit(f"collector_a: port {cfg['serve_port']} is busy ({e}); not starting.")
        threading.Thread(target=server.serve_forever, daemon=True).start()
    log(f"collector_a started, pid {os.getpid()}")
    collector = Collector(cfg)
    history = History(HERE / "history-a.jsonl", cfg["history_days"])
    last_error = None
    next_t = time.monotonic()
    while True:
        now = time.time()
        p = collector.sample(now)
        atomic_write(HERE / "metrics-a.json", json.dumps(p).encode())
        history.add(p, now)
        if p["error"] != last_error:
            log(f"source error: {p['error']}" if p["error"] else "source ok again")
            last_error = p["error"]
        next_t += cfg["sample_s"]
        time.sleep(max(0.0, next_t - time.monotonic()))


if __name__ == "__main__":
    main()
