"""Token-burn gauge: Claude Code and Codex dials, and the kill switch.

Once a second this reads three cheap things and nothing else:
  * usage counters appended to Claude Code's own session logs (numbers only;
    message text on those lines is parsed and dropped, never kept or written);
  * new rows in Quotalis's usage database, opened read-only (Codex tokens);
  * per-process written-byte counters of the agent processes (the live signal
    that moves while a reply is still streaming, before any token count exists).
No model calls. Nothing under the Codex home folder is opened.
"""
import ctypes
import json
import logging
import os
import sqlite3
import subprocess
import time
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path

log = logging.getLogger("agents")
WINDOW_S = 60
RESCAN_S = 10
PROC_LIST_S = 5
STREAM_OPS = 3        # writes in one second that mark an answer streaming
TOKENS_PER_OP = 3.0   # first guess, until the logs have given 300 output tokens
RECENT_FILE_S = 900
USAGE_KEYS = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
              "cache_read_input_tokens")

DEFAULTS = {
    "claude_projects_dir": str(Path.home() / ".claude" / "projects"),
    "quotalis_db": str(Path(os.environ.get("LOCALAPPDATA", "")) / "Quotalis" / "usage.db"),
    "quotalis_codex_provider": 1,
    "agent_burn_tokens_per_min": 50000,
    "agent_redline_tokens_per_min": 150000,
    "agent_scale_max_tokens_per_min": 500000,
    # Claude Code's needle: output tokens per second, summed over every session.
    "agent_burn_tokens_per_s": 200,
    "agent_redline_tokens_per_s": 500,
    "agent_scale_max_tokens_per_s": 2000,
    # Time to first token: seconds waited after a request went out.
    "ttft_burn_s": 8,
    "ttft_redline_s": 20,
    "ttft_scale_max_s": 60,
    # Model tiers for the title colour: first name fragment found in the model's name wins.
    # 1 = top (purple), 2 = orange, 3 = yellow, 4 = bottom (blue). Claude then Codex.
    "model_tiers": [["fable", 1], ["mythos", 1], ["opus", 2], ["sonnet", 3], ["haiku", 4],
                    ["sol", 1], ["astra", 2], ["luna", 3], ["mini", 4], ["nano", 4]],
    # Effort levels, coloured the same way.
    "effort_tiers": [["max", 1], ["xhigh", 1], ["high", 2], ["medium", 3], ["low", 4], ["minimal", 4]],
    "tap_port": 8832,
    "active_window_s": 5,
    "active_min_bytes": 1000,   # written in one second for that second to count
    "active_min_seconds": 3,    # such seconds needed within the window
}


# ---------------------------------------------------------------- processes

def _win_process_reader():
    """Returns a function giving [{pid, kind, bytes}] for agent processes."""
    from ctypes import wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("pid", wt.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wt.DWORD), ("threads", wt.DWORD),
                    ("ppid", wt.DWORD), ("pri", wt.LONG), ("flags", wt.DWORD),
                    ("exe", wt.WCHAR * 260)]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in
                    ("rops", "wops", "oops", "rbytes", "wbytes", "obytes")]

    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.Process32FirstW.argtypes = k32.Process32NextW.argtypes = [wt.HANDLE,
                                                                 ctypes.POINTER(Entry)]
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.CloseHandle.argtypes = [wt.HANDLE]
    k32.GetProcessIoCounters.argtypes = [wt.HANDLE, ctypes.POINTER(Io)]
    k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                               ctypes.POINTER(wt.DWORD)]
    QUERY_LIMITED = 0x1000
    known = {"at": 0.0, "pids": {}}  # pid -> kind

    def image_path(h):
        buf = ctypes.create_unicode_buffer(1024)
        n = wt.DWORD(1024)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""

    def list_agents():
        pids = {}
        snap = k32.CreateToolhelp32Snapshot(2, 0)
        e = Entry()
        e.dwSize = ctypes.sizeof(Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            name = e.exe.lower()
            if name == "codex.exe":
                pids[e.pid] = "codex"
            elif name == "quotalis.exe":
                pids[e.pid] = "quotalis"
            elif name == "claude.exe":
                # The desktop app and its helpers share the name; only the
                # session processes live under a "claude-code" folder.
                h = k32.OpenProcess(QUERY_LIMITED, False, e.pid)
                if h:
                    if "\\claude-code\\" in image_path(h).lower():
                        pids[e.pid] = "claude"
                    k32.CloseHandle(h)
            ok = k32.Process32NextW(snap, ctypes.byref(e))
        k32.CloseHandle(snap)
        return pids

    def read():
        now = time.monotonic()
        if now - known["at"] >= PROC_LIST_S or not known["pids"]:
            known["pids"], known["at"] = list_agents(), now
        out = []
        for pid, kind in known["pids"].items():
            total = ops = 0
            if kind != "quotalis":
                h = k32.OpenProcess(QUERY_LIMITED, False, pid)
                if not h:
                    continue
                io = Io()
                got = k32.GetProcessIoCounters(h, ctypes.byref(io))
                k32.CloseHandle(h)
                if not got:
                    continue
                # Bytes written only: replies streamed on to the app and log lines.
                # Bytes read are left out because one process re-reading big files
                # from disk (seen: 100 to 250 MB a second) would drown the signal.
                total = io.wbytes
                ops = io.wops
            out.append({"pid": pid, "kind": kind, "bytes": total, "ops": ops})
        return out

    return read


def taskkill(pid):
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                       timeout=10, creationflags=flags)
    return r.returncode == 0


class ProcWatch:
    """Bytes written per second by one kind of process, and which are busy."""

    def __init__(self, kind, window_s, min_bytes, min_seconds):
        self.kind, self.window_s, self.min_bytes = kind, window_s, min_bytes
        self.min_seconds = min_seconds
        self.last = {}   # pid -> byte counter
        self.hist = {}   # pid -> deque of (time, bytes moved)
        self.rate = 0.0
        # Write operations: a streaming answer is passed on piece by piece, one
        # small write each, so their count follows the answer as it arrives.
        self.last_ops = {}
        self.ops = 0
        self.last_write_t = None
        self.streaming = False
        self.wait_s = None    # quiet time before the newest answer stream began
        self.wait_at = None

    def update(self, rows, now):
        mine = {r["pid"]: r["bytes"] for r in rows if r["kind"] == self.kind}
        ops = {r["pid"]: r.get("ops", 0) for r in rows if r["kind"] == self.kind}
        self.ops = sum(max(0, n - self.last_ops[p]) for p, n in ops.items()
                       if p in self.last_ops)
        self.last_ops = ops
        streaming = self.ops >= STREAM_OPS
        if streaming and not self.streaming and self.last_write_t is not None:
            self.wait_s, self.wait_at = round(now - self.last_write_t, 1), now
        self.streaming = streaming
        moved = 0
        for pid, total in mine.items():
            if pid in self.last:
                d = max(0, total - self.last[pid])
                moved += d
                self.hist.setdefault(pid, deque()).append((now, d))
        for pid in list(self.hist):
            if pid not in mine:
                del self.hist[pid]
                continue
            h = self.hist[pid]
            while h and now - h[0][0] > self.window_s:
                h.popleft()
        self.last = mine
        self.rate = float(moved)
        if moved > 0:
            self.last_write_t = now
        return len(mine)

    def active(self):
        """A streaming reply writes every second; a lone burst (a log flush,
        a periodic job) does not make a process busy."""
        return sorted(p for p, h in self.hist.items()
                      if sum(d >= self.min_bytes for _, d in h) >= self.min_seconds)


# ------------------------------------------------------------ token sources

def model_tier(model, tiers):
    """-> 1 (top) to 4 (bottom) for a model name, or None when no fragment matches."""
    name = (model or "").lower()
    for fragment, tier in tiers or []:
        if fragment.lower() in name:
            return tier
    return None


class ModelTally:
    """Which models produced output lately: (time, model, output tokens), last 10 minutes.

    The title follows the model in use now: the one with the most output in the last two
    minutes. A different model takes the title only with a real answer behind it (200
    output tokens in those two minutes) or once the old one has been silent for the whole
    ten, so a small background call on another model does not flip it.
    """

    WINDOW_S = 600
    RECENT_S = 120
    TAKEOVER_TOKENS = 200

    def __init__(self):
        self.events = deque()
        self.last = None      # (model, time) of the newest one seen, kept after the window empties
        self.main = None

    def add(self, ts, model, out_tokens):
        if not isinstance(model, str) or not model or model.startswith("<"):
            return
        self.events.append((ts, model, max(0, int(out_tokens or 0))))
        if self.last is None or ts >= self.last[1]:
            self.last = (model, ts)

    def report(self, now):
        """-> (main model or None, seconds since it was last seen, [{model, share_pct, age_s}])."""
        while self.events and now - self.events[0][0] > self.WINDOW_S:
            self.events.popleft()
        out, seen, recent = {}, {}, {}
        for ts, model, n in self.events:
            out[model] = out.get(model, 0) + n
            seen[model] = max(seen.get(model, 0), ts)
            if now - ts <= self.RECENT_S:
                recent[model] = recent.get(model, 0) + n
        if recent:
            top = max(recent, key=lambda m: (recent[m], seen[m]))
            if self.main not in out or top == self.main or recent[top] >= self.TAKEOVER_TOKENS:
                self.main = top
        if self.main not in out and out:
            self.main = max(out, key=lambda m: (out[m], seen[m]))
        total = sum(out.values())
        models = [{"model": m, "share_pct": round(100 * out[m] / total) if total else None,
                   "age_s": round(max(0, now - seen[m]))}
                  for m in sorted(out, key=lambda m: (m != self.main, -out[m], -seen[m]))]
        if models:
            return models[0]["model"], models[0]["age_s"], models
        if self.last:
            return self.last[0], round(max(0, now - self.last[1])), []
        return None, None, []


class ClaudeTail:
    """Follows Claude Code's session logs and yields usage-counter increases."""

    def __init__(self, root):
        self.root = Path(root)
        self.files = {}       # path -> read offset
        self.partial = {}     # path -> bytes of an unfinished last line
        self.seen = OrderedDict()  # message id -> counters already counted
        self.scanned = None
        self.model = None
        self.tally = ModelTally()
        self.effort = {}      # model -> effort level named on its newest log line
        self.now = None
        self.request_t = None  # when the newest "request sent" line was read

    def _rescan(self, now):
        first = self.scanned is None
        self.scanned = now
        cutoff = time.time() - RECENT_FILE_S
        for dirpath, _dirs, names in os.walk(self.root):
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if p in self.files or st.st_mtime < cutoff:
                    continue
                # History that was there before the gauge started is not burn.
                self.files[p] = st.st_size if first else 0
                if first and now - st.st_mtime <= ModelTally.WINDOW_S:
                    self._seed_model(p, st.st_size, now)
        for p in [p for p in self.files if not os.path.exists(p)]:
            del self.files[p]
            self.partial.pop(p, None)

    def _seed_model(self, path, size, now):
        """Which model and effort the newest answers in a log used, read from its tail
        when the gauge starts. Names and times only: these tokens are not counted as burn."""
        try:
            with open(path, "rb") as f:
                f.seek(max(0, size - 400_000))
                lines = f.read().split(b"\n")[1:]
        except OSError:
            return
        newest = {}   # message id -> (time, model, output tokens); a message is logged in pieces
        for line in lines:
            if b'"usage"' not in line:
                continue
            try:
                obj = json.loads(line)
                msg = obj["message"]
                model, out = msg["model"], int(msg["usage"].get("output_tokens") or 0)
                ts = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00")).timestamp()
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if not isinstance(model, str) or model.startswith("<") or now - ts > ModelTally.WINDOW_S:
                continue
            newest[msg.get("id") or id(line)] = (ts, model, out)
            effort = obj.get("effort") or obj.get("perTurnEffort")
            if isinstance(effort, str) and effort:
                self.effort[model] = effort
        for ts, model, out in sorted(newest.values()):
            self.tally.add(ts, model, out)

    def _count(self, line):
        try:
            obj = json.loads(line)
            msg = obj.get("message")
        except (ValueError, AttributeError):
            return None
        if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            return None
        u = msg["usage"]
        cur = tuple(int(u.get(k) or 0) for k in USAGE_KEYS)
        mid = msg.get("id") or id(line)
        old = self.seen.get(mid, (0, 0, 0, 0))
        delta = tuple(max(0, c - o) for c, o in zip(cur, old))
        self.seen[mid] = tuple(max(c, o) for c, o in zip(cur, old))
        self.seen.move_to_end(mid)
        while len(self.seen) > 2000:
            self.seen.popitem(last=False)
        if isinstance(msg.get("model"), str) and not msg["model"].startswith("<"):
            self.model = msg["model"]
            effort = obj.get("effort") or obj.get("perTurnEffort")
            if isinstance(effort, str) and effort:
                self.effort[msg["model"]] = effort
            if any(delta):
                self.tally.add(self.now, msg["model"], delta[1])
        return delta if any(delta) else None

    def poll(self, now):
        """-> list of (input, output, cache_write, cache_read) increases."""
        if not self.root.is_dir():
            raise OSError(f"{self.root} not found")
        self.now = now
        if self.scanned is None or now - self.scanned >= RESCAN_S:
            self._rescan(now)
        out = []
        for p, off in list(self.files.items()):
            try:
                size = os.path.getsize(p)
                if size < off:      # the file was rewritten; start over at its end
                    self.files[p] = size
                    continue
                if size == off:
                    continue
                with open(p, "rb") as f:
                    f.seek(off)
                    data = self.partial.pop(p, b"") + f.read(size - off)
            except OSError:
                continue
            self.files[p] = size
            lines = data.split(b"\n")
            if lines[-1]:
                self.partial[p] = lines[-1]
            for line in lines[:-1]:
                # The session writes this line as a request goes out; only its
                # arrival time is used, the line itself is dropped.
                if line.startswith(b'{"type":"api-request"'):
                    self.request_t = now
                elif b'"usage"' in line:
                    d = self._count(line)
                    if d:
                        out.append(d)
        return out


class QuotalisTail:
    """New Codex rows in Quotalis's database. Read-only; Quotalis is not changed."""

    COLS = ("event_key", "ts_ms", "input", "output", "cache_w5m", "cache_w1h", "cache_read",
            "model")

    def __init__(self, db_path, provider):
        self.db_path, self.provider = Path(db_path), provider
        self.since_ms = None
        self.seen = OrderedDict()
        self.stamp = None
        self.model = None
        self.effort = None
        self.tally = ModelTally()
        self.newest_ms = None

    def _stamp(self):
        s = []
        for suffix in ("", "-wal"):
            try:
                st = os.stat(str(self.db_path) + suffix)
                s.append((st.st_mtime_ns, st.st_size))
            except OSError:
                s.append(None)
        return s

    def _effort(self, con):
        """Effort of the newest Codex session, from the state Quotalis keeps per log file.
        Quotalis reads Codex's logs; this gauge reads only Quotalis. None if not recorded."""
        try:
            r = con.execute("select session_id from usage_events where provider = ? "
                            "order by ts_ms desc limit 1", (self.provider,)).fetchone()
            if not r or not r[0]:
                return None
            states = con.execute("select state from file_cursors where state like ? "
                                 "order by mtime_ticks desc limit 20", (f"%{r[0]}%",)).fetchall()
        except sqlite3.Error:
            return None
        for (text,) in states:
            try:
                state = json.loads(text)
            except ValueError:
                continue
            if isinstance(state, dict) and state.get("sessionId", state.get("SessionId")) == r[0]:
                effort = state.get("effort") or state.get("Effort")
                return effort if isinstance(effort, str) else None
        return None

    def poll(self, now):
        """-> list of (ts, input, output, cache_write, cache_read). `now` is epoch s."""
        if not self.db_path.exists():
            raise OSError("Quotalis database not found")
        if self.since_ms is None:
            self.since_ms = int((now - WINDOW_S) * 1000)
        stamp = self._stamp()
        if stamp == self.stamp:
            return []
        uri = "file:" + self.db_path.as_posix() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            if self.tally.last is None:
                # The model Codex used last, even if that was before the gauge started.
                r = con.execute("select model, ts_ms from usage_events where provider = ? "
                                "order by ts_ms desc limit 1", (self.provider,)).fetchone()
                if r and r[0]:
                    self.tally.last = (r[0], r[1] / 1000.0)
                    self.model = self.model or r[0]
            rows = con.execute(
                f"select {', '.join(self.COLS)} from usage_events "
                "where provider = ? and ts_ms >= ?", (self.provider, self.since_ms)).fetchall()
            self.effort = self._effort(con) or self.effort
        except sqlite3.Error as e:
            raise OSError(f"Quotalis database: {e}") from None
        finally:
            con.close()
        self.stamp = stamp
        out = []
        for key, ts_ms, inp, outp, w5, w1, cr, model in rows:
            cur = (inp, outp, w5 + w1, cr)
            old = self.seen.get(key, (0, 0, 0, 0))
            delta = tuple(max(0, c - o) for c, o in zip(cur, old))
            self.seen[key] = tuple(max(c, o) for c, o in zip(cur, old))
            if any(delta):
                out.append((ts_ms / 1000.0,) + delta)
                self.model = model
                self.tally.add(ts_ms / 1000.0, model, delta[1])
                self.newest_ms = max(self.newest_ms or 0, ts_ms)
        while len(self.seen) > 5000:
            self.seen.popitem(last=False)
        # Keep re-reading the last two minutes in case a row is updated in place.
        self.since_ms = max(self.since_ms, int((now - 120) * 1000))
        return out


# -------------------------------------------------------------------- dials

class AgentDial:
    def __init__(self, host, title, cfg):
        self.host, self.title, self.cfg = host, title, cfg
        self.events = deque()            # (time, input, output, cache_write, cache_read)
        self.spark = deque(maxlen=60)
        self.spark2 = deque(maxlen=60)   # the second dial's last 60 values
        self.totals = [0, 0, 0, 0]
        self.seq = 0
        self.payload_bytes = b"{}"

    def add(self, t, counts):
        self.events.append((t,) + tuple(counts))
        for i, c in enumerate(counts):
            self.totals[i] += c

    def build(self, now, watch, n_procs, token_error, extra, per_second=None):
        """`per_second`, when given, puts the needle on tokens per second:
        {"value", "label", "ring", "ring_label", "busy"}; the per-minute sums
        from the token counts stay in `detail`."""
        while self.events and now - self.events[0][0] > WINDOW_S:
            self.events.popleft()
        c = self.cfg
        sums = [sum(e[i] for e in self.events) for i in (1, 2, 3, 4)]
        new_tokens = sums[0] + sums[1] + sums[2]   # cache reads are shown apart
        last10 = sum(e[1] + e[2] + e[3] for e in self.events if now - e[0] <= 10)
        value = None if token_error else new_tokens
        unit = "min"
        if per_second:
            value, unit = per_second["value"], "s"
        active = watch.active()
        self.spark.append(value or 0)
        # Per second, the state follows a 10 s mean so it does not flap between
        # BURNING and IDLE on single samples; the needle still shows each second.
        judged = value
        if per_second and value is not None:
            recent = list(self.spark)[-10:]
            judged = sum(recent) / len(recent)
        if judged is not None and judged >= c[f"agent_redline_tokens_per_{unit}"]:
            state = "REDLINE"
        elif judged is not None and judged >= c[f"agent_burn_tokens_per_{unit}"]:
            state = "BURNING"
        elif (value or 0) > 0 or active or (per_second and per_second.get("busy")):
            state = "WARM"
        else:
            state = "IDLE"
        # The second dial is the other time base: per minute when the needle is
        # per second, and a 10 s average of output tokens per second otherwise.
        if per_second:
            v2, u2, label2 = (None if token_error else new_tokens), "min", "new tokens per minute"
        else:
            out10 = sum(e[2] for e in self.events if now - e[0] <= 10)
            v2, u2 = (None if token_error else round(out10 / 10)), "s"
            label2 = "output tokens per second, 10 s average"
        self.spark2.append(v2 or 0)
        if v2 is not None and v2 >= c[f"agent_redline_tokens_per_{u2}"]:
            state2 = "REDLINE"
        elif v2 is not None and v2 >= c[f"agent_burn_tokens_per_{u2}"]:
            state2 = "BURNING"
        else:
            state2 = "WARM" if (v2 or 0) > 0 else "IDLE"
        self.seq += 1
        detail = {
            "second_dial": {"label": label2, "value": v2, "unit": f"tokens/{u2}",
                            "scale_max": c[f"agent_scale_max_tokens_per_{u2}"], "log": True,
                            "redline": c[f"agent_redline_tokens_per_{u2}"], "state": state2,
                            "spark": list(self.spark2)},
            "tokens_in_per_min": sums[0], "tokens_out_per_min": sums[1],
            "cache_write_per_min": sums[2], "cache_read_per_min": sums[3],
            "tokens_last_10s": last10, "processes": n_procs, "active_pids": active,
            "bytes_per_s": watch.rate, "spark": list(self.spark),
            "since_start": dict(zip(("input", "output", "cache_write", "cache_read"),
                                    self.totals)),
            "token_error": token_error,
        }
        detail.update(extra)
        payload = {
            "ts": now, "seq": self.seq, "host": self.host, "source_ok": True,
            "stale_after_s": c["stale_after_s"], "poll_s": c["poll_s"],
            "dial": {"primary": {"label": per_second["label"] if per_second
                                 else "new tokens per minute", "value": value,
                                 "unit": f"tokens/{unit}",
                                 "scale_max": c[f"agent_scale_max_tokens_per_{unit}"],
                                 "log": True,
                                 "redline": c[f"agent_redline_tokens_per_{unit}"]},
                     "ring": {"label": per_second["ring_label"], "value": per_second["ring"]}
                     if per_second else
                             {"label": "live output, KB per second",
                              "value": round(watch.rate / 1024, 1)},
                     "fuel": {"label": None, "value": None, "of": None},
                     "state": state},
            "detail": detail, "error": None,
        }
        self.payload_bytes = json.dumps(payload).encode()
        return payload


def read_tap(port):
    """The live tap's counters, or None when the tap is not answering."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/gauge-tap.json", timeout=0.3) as r:
            d = json.loads(r.read())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


class Agents:
    def __init__(self, cfg, proc_reader=None, killer=taskkill, abort_local=None, pause_remote=None,
                 tap_reader=None):
        self.cfg = {**DEFAULTS, **cfg}
        c = self.cfg
        self.tap_reader = tap_reader or (lambda: read_tap(c["tap_port"]))
        self.ops_since = 0      # streaming writes since the logs last added output
        self.out_seen = 0
        self.ratio = None
        # Time to first token for sessions outside the tap.
        self.request_seen = None   # the request line already acted on
        self.wait_from = None      # a request is out and nothing has come back yet
        self.first_s = None        # the newest finished wait, seconds
        self.first_at = None
        self.first_id = None       # tap request id already put in the history
        self.ttft_spark = deque(maxlen=60)
        self.proc_reader = proc_reader or _win_process_reader()
        self.killer, self.abort_local, self.pause_remote = killer, abort_local, pause_remote
        self.watch = {k: ProcWatch(k, c["active_window_s"], c["active_min_bytes"],
                                    c["active_min_seconds"])
                      for k in ("claude", "codex")}
        self.claude_tail = ClaudeTail(c["claude_projects_dir"])
        self.codex_tail = QuotalisTail(c["quotalis_db"], c["quotalis_codex_provider"])
        self.claude = AgentDial("claude-code", "Claude Code", c)
        self.codex = AgentDial("codex", "Codex", c)

    def step(self, now=None):
        now = time.time() if now is None else now
        try:
            rows = self.proc_reader()
        except OSError as e:
            rows = []
            log.warning("process read failed: %s", e)
        n = {k: w.update(rows, now) for k, w in self.watch.items()}

        err = None
        try:
            for counts in self.claude_tail.poll(now):
                self.claude.add(now, counts)
        except OSError as e:
            err = str(e)
        tap = self.tap_reader()
        # The tap only sees sessions started after it was switched on. When nothing
        # has passed through it lately, the logs are the only sign of work elsewhere.
        last = (tap or {}).get("last_request") or {}
        tap_live = bool(tap) and (tap["active_requests"] > 0 or tap["output_tokens_per_s"] > 0
                                  or tap["input_tokens_per_s"] > 0
                                  or (last.get("age_s") is not None and last["age_s"] < 15))
        # Learn how many output tokens one write stands for, from the exact counts
        # the logs give after each answer.
        # The ratio moves only when the logs add output: the output added, over the
        # streaming writes seen since the previous addition. Dividing totals since
        # start made it drift down during each answer and jump when the log landed.
        w = self.watch["claude"]
        if w.streaming:
            self.ops_since += w.ops
        out_total = self.claude.totals[1]
        added = out_total - self.out_seen
        if added > 0:
            if self.ops_since >= 20 and added >= 50:
                sample = min(20.0, max(0.5, added / self.ops_since))
                self.ratio = sample if self.ratio is None else 0.7 * self.ratio + 0.3 * sample
            self.out_seen, self.ops_since = out_total, 0
        ratio = self.ratio if self.ratio is not None else TOKENS_PER_OP
        ttft = self._ttft(now, tap_live, last, w.streaming or added > 0)
        if tap_live:
            # Read off the live API stream: the request, the prompt's size and the
            # answer as it arrives. Output is an estimate until each answer ends.
            per_second = {"value": round(tap["output_tokens_per_s"]),
                          "label": "output tokens per second",
                          "ring": tap["input_tokens_per_s"],
                          "ring_label": "prompt tokens sent this second",
                          "busy": tap["active_requests"] > 0}
            source = "live API stream through the tap; output is estimated until an answer ends"
        else:
            # Desktop-app sessions do not pass through the tap. Their processes hand
            # each piece of an answer on as it arrives, so the writes are counted
            # every second and scaled by the learned ratio.
            # Output tokens only: prompt and cache tokens are not generated a second
            # at a time, and counting them read a 54,000-token cached prompt as
            # thousands of tokens per second (found 11/11/2026).
            out10 = sum(e[2] for e in self.claude.events if now - e[0] <= 10)
            per_second = {"value": round(w.ops * ratio) if w.streaming
                          else (None if err else round(out10 / 10)),
                          "label": "output tokens per second, estimated from the live stream"
                          if w.streaming else "output tokens per second, 10 s average from logs",
                          "ring": None, "ring_label": "prompt tokens sent this second",
                          "busy": w.streaming}
            source = ("estimated: answer pieces counted as each session process passes "
                      "them on, scaled by exact counts from the logs")
        main, main_age, models = self.claude_tail.tally.report(now)
        # The log is the authority; the tap's last request can be a background call.
        claude_model = (main or self.claude_tail.model
                        or ((tap or {}).get("last_request") or {}).get("model"))
        claude_effort = self.claude_tail.effort.get(claude_model)
        self.claude.build(now, self.watch["claude"], n["claude"], err, {
            # The model doing most of the output in the last 10 minutes, so a small
            # background call on another model does not take the label.
            "model": claude_model, "model_tier": model_tier(claude_model, self.cfg["model_tiers"]),
            "effort": claude_effort,
            "effort_tier": None if not claude_effort else next(
                (t for name, t in self.cfg["effort_tiers"] if name == claude_effort.lower()), None),
            "model_age_s": main_age, "models": models,
            "tap": None if not tap else {
                "active_requests": tap["active_requests"],
                "requests_total": tap["requests_total"],
                "upstream_errors": tap["upstream_errors"],
                "chars_per_token": tap["chars_per_token"],
                "last_request": tap["last_request"]},
            "stream": {"writes_per_s": w.ops, "tokens_per_write": round(ratio, 2),
                       "learned": self.ratio is not None, "wait_s": w.wait_s,
                       "wait_age_s": None if w.wait_at is None else round(now - w.wait_at)},
            "ttft": ttft,
            "token_source": source}, per_second)

        err = None
        quotalis_up = any(r["kind"] == "quotalis" for r in rows)
        try:
            for ev in self.codex_tail.poll(now):
                self.codex.add(ev[0], ev[1:])
        except OSError as e:
            err = str(e)
        if not err and not quotalis_up:
            err = "Quotalis is not running, so no Codex token counts arrive"
        newest = self.codex_tail.newest_ms
        main, main_age, models = self.codex_tail.tally.report(now)
        self.codex.build(now, self.watch["codex"], n["codex"], err, {
            "model": main or self.codex_tail.model,
            "model_tier": model_tier(main or self.codex_tail.model, self.cfg["model_tiers"]),
            # Codex's effort comes from Quotalis (0.8.1 and later record it); the Codex home
            # folder is never opened by this gauge.
            "effort": self.codex_tail.effort,
            "effort_tier": None if not self.codex_tail.effort else next(
                (t for name, t in self.cfg["effort_tiers"]
                 if name == self.codex_tail.effort.lower()), None),
            "model_age_s": main_age, "models": models,
            "newest_row_age_s": None if newest is None else round(now - newest / 1000, 1),
            "token_source": "Quotalis database (read-only); rows arrive some seconds late"})

    def _ttft(self, now, tap_live, last, answering):
        """Seconds from a request going out to the first piece of its answer.
        Exact from the tap; otherwise from the session log's "request sent" line
        to the first streamed write, to the nearest second."""
        c = self.cfg
        rt = self.claude_tail.request_t
        fresh = rt is not None and rt != self.request_seen
        if fresh:
            self.request_seen, self.wait_from = rt, rt
        elif self.wait_from is not None:
            if answering:
                self.first_s, self.first_at = round(now - self.wait_from, 1), now
                self.wait_from = None
                if not tap_live:
                    self.ttft_spark.append(self.first_s)
            elif now - self.wait_from > 600:   # nothing came back; stop counting
                self.wait_from = None
        if tap_live and last:
            waiting = last.get("first_token_s") is None and last.get("done_s") is None
            value = last.get("age_s") if waiting else last.get("first_token_s")
            age = last.get("age_s")
            basis = "live API stream through the tap"
            if not waiting and value is not None and last.get("id") != self.first_id:
                self.first_id = last.get("id")
                self.ttft_spark.append(value)
        else:
            waiting = self.wait_from is not None
            value = round(now - self.wait_from, 1) if waiting else self.first_s
            age = None if waiting or self.first_at is None else round(now - self.first_at)
            basis = ("estimated: session log's request line to the first streamed "
                     "write, to the nearest second")
        if not waiting or value is None:
            state = "IDLE"
        elif value >= c["ttft_redline_s"]:
            state = "REDLINE"
        elif value >= c["ttft_burn_s"]:
            state = "BURNING"
        else:
            state = "WARM"
        return {"value_s": value, "waiting": waiting, "age_s": age, "basis": basis,
                "state": state, "scale_max": c["ttft_scale_max_s"],
                "redline": c["ttft_redline_s"], "spark": list(self.ttft_spark)}

    def kill(self, target):
        """Stops what is in flight for one dial. -> dict for the page."""
        if target == "local":
            n = self.abort_local() if self.abort_local else None
            if n is None:
                return {"ok": False, "message": "the counting proxy is not running"}
            return {"ok": True, "killed": n,
                    "message": f"{n} local generation(s) cut off" if n
                    else "no local generation was running through the proxy"}
        if target in ("model", "model-resume"):   # passed on; the remote host decides what it holds
            route = "/resume" if target == "model-resume" else "/kill"
            return self.pause_remote(route) if self.pause_remote else {
                "ok": False, "message": "no pause switch is set up"}
        if target not in self.watch:
            return {"ok": False, "message": "unknown target"}
        pids = self.watch[target].active()
        if not pids:
            return {"ok": True, "killed": 0, "message": "nothing in flight; no process was busy "
                    f"in the last {self.cfg['active_window_s']} s"}
        done = [p for p in pids if self.killer(p)]
        log.warning("kill switch: target %s, busy pids %s, ended %s", target, pids, done)
        return {"ok": len(done) == len(pids), "killed": len(done),
                "message": f"ended {len(done)} of {len(pids)} busy process(es)"}
