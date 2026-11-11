"""Token-burn gauge: local collector (this Windows PC).

Samples nvidia-smi once a second, writes metrics-b.json in the shared contract
shape, and serves gauge.html on 127.0.0.1. No model calls; counters and
timestamps only.
"""
import json
import logging
import os
import re
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agents import Agents

HERE = Path(__file__).resolve().parent
QUERY = "utilization.gpu,power.draw,memory.used,memory.total,power.limit,name"
KEEP_LAST_GOOD_S = 3
CALIBRATE_S = 60
SECRET_ENV = re.compile(r"(API_?KEY|_TOKEN|_SECRET|ACCESS_KEY)$", re.I)
log = logging.getLogger("collector_b")


def scrub_env():
    """The collector needs no provider keys, so drop any it inherited."""
    names = [k for k in os.environ if SECRET_ENV.search(k)]
    for k in names:
        del os.environ[k]
    return len(names)


def atomic_write(path, data):
    tmp = Path(str(path) + ".tmp")
    tmp.write_bytes(data)
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # a reader has the file open (Windows)
            time.sleep(0.05)


def parse_gpu(line):
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 6:
        raise ValueError(f"unexpected nvidia-smi output ({len(parts)} fields)")
    util, power, used, total, limit = parts[:5]
    if "N/A" in power:
        raise ValueError("nvidia-smi reports power.draw as [N/A] on this card/driver")
    return {
        "util": float(util),
        "watts": float(power),
        "vram_used": float(used),
        "vram_total": float(total),
        "limit": float(limit),
        "name": ", ".join(parts[5:]),
    }


def read_gpu():
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=flags,
        )
    except FileNotFoundError:
        raise ValueError("nvidia-smi not found") from None
    except subprocess.TimeoutExpired:
        raise ValueError("nvidia-smi timed out") from None
    if out.returncode != 0 or not out.stdout.strip():
        raise ValueError(f"nvidia-smi failed (exit {out.returncode})")
    return parse_gpu(out.stdout.strip().splitlines()[0])


def read_proxy(port):
    """Counters from the counting proxy, or None when it is not running."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics.json", timeout=0.3) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def port_open(url):
    u = urlsplit(url)
    try:
        with socket.create_connection((u.hostname, u.port or 80), timeout=0.2):
            return True
    except OSError:
        return False


CPU_COUNTERS = [r"\Energy Meter(RAPL_Package0_PKG)\Power",
                r"\Processor Information(_Total)\% Processor Utility"]
CPU_DEFAULTS = {"cpu_scale_max_watts": 250, "cpu_redline_watts": 200,
                "cpu_warm_util_pct": 25, "cpu_burn_util_pct": 60}


def parse_cpu(line):
    """One typeperf CSV sample -> (watts, load %), or None for headers and gaps."""
    parts = [p.strip().strip('"') for p in line.split(",")]
    if len(parts) != 3 or parts[0].startswith("(PDH"):
        return None
    try:
        return float(parts[1]) / 1000.0, min(100.0, float(parts[2]))  # counter is in mW
    except ValueError:
        return None


def cpu_name():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
            return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
    except (ImportError, OSError):
        return None


class CpuWatch:
    """CPU package watts and load, once a second, from Windows' own counters.
    One typeperf child at a time; it ends itself after five minutes and is
    started again, so a killed collector cannot leave one running for long."""

    def __init__(self, cfg):
        self.cfg = {**CPU_DEFAULTS, **cfg}
        self.latest = None   # (time, watts, load %)
        self.spark = deque(maxlen=60)
        self.name = cpu_name()
        self.error = None

    def add(self, line, now):
        s = parse_cpu(line)
        if s:
            self.latest, self.error = (now,) + s, None
            self.spark.append(round(s[0], 1))

    def run(self):
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        while True:
            try:
                p = subprocess.Popen(["typeperf", *CPU_COUNTERS, "-si", "1", "-sc", "300"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     stdin=subprocess.DEVNULL, text=True, creationflags=flags)
                got = False
                for line in p.stdout:
                    before = self.latest
                    self.add(line, time.time())
                    got = got or self.latest is not before
                p.wait()
                if not got:
                    self.error = "this PC gives no CPU power counter (Energy Meter)"
                    time.sleep(60)
            except OSError as e:
                self.error = f"typeperf could not start: {e}"
                time.sleep(60)

    def detail(self, now):
        c = self.cfg
        d = {"watts": None, "util_pct": None, "name": self.name, "state": "IDLE",
             "scale_max": c["cpu_scale_max_watts"], "redline": c["cpu_redline_watts"],
             "spark": list(self.spark), "error": self.error}
        if self.latest is None or now - self.latest[0] > 5:
            d["error"] = self.error or "no CPU reading in the last 5 s"
            return d
        _, watts, util = self.latest
        d.update(watts=round(watts, 1), util_pct=round(util, 1), error=None)
        if watts >= c["cpu_redline_watts"]:
            d["state"] = "REDLINE"
        elif util >= c["cpu_burn_util_pct"]:
            d["state"] = "BURNING"
        elif util >= c["cpu_warm_util_pct"]:
            d["state"] = "WARM"
        return d


class StateMachine:
    """IDLE / WARM / BURNING / REDLINE from watts against the idle baseline."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.burn_since = None
        self.red_since = None

    def update(self, watts, limit, idle, now):
        c = self.cfg
        over_burn = watts >= idle + c["burn_watts_over_idle"]
        over_red = watts >= c["redline_watts_frac"] * limit
        self.burn_since = (self.burn_since or now) if over_burn else None
        self.red_since = (self.red_since or now) if over_red else None
        if over_red and now - self.red_since >= c["redline_sustain_s"]:
            return "REDLINE"
        if over_burn and now - self.burn_since >= c["burn_sustain_s"]:
            return "BURNING"
        if watts >= idle + c["warm_watts_over_idle"]:
            return "WARM"
        return "IDLE"


class Collector:
    def __init__(self, cfg, cfg_path=None, gpu_reader=read_gpu, proxy_reader=read_proxy,
                 runtime_probe=port_open, calibrate_s=CALIBRATE_S, cpu=None):
        self.cfg = cfg
        self.cpu = cpu
        self.cfg_path = cfg_path
        self.gpu_reader = gpu_reader
        self.proxy_reader = proxy_reader
        self.runtime_probe = runtime_probe
        self.calibrate_s = calibrate_s
        self.sm = StateMachine(cfg)
        self.seq = 0
        self.last_good = None
        self.last_good_t = 0.0
        self.calib = []
        self.calib_start = None
        self.spark = deque(maxlen=60)
        self.tok = deque(maxlen=4)  # (time, live completion-token counter)
        self.runtime_up = None
        self.runtime_checked = 0.0
        self.payload_bytes = b"{}"

    def _idle_watts(self, watts, now):
        """Return (idle_watts, calibrating). First run: median of 60 s of samples."""
        idle = self.cfg.get("idle_watts")
        if idle is not None:
            return idle, False
        if self.calib_start is None:
            self.calib_start = now
        self.calib.append(watts)
        if now - self.calib_start < self.calibrate_s:
            return None, True
        idle = round(statistics.median(self.calib), 1)
        self.cfg["idle_watts"] = idle
        if self.cfg_path:
            atomic_write(self.cfg_path, json.dumps(self.cfg, indent=2).encode())
        log.info("idle baseline measured: %.1f W over %d samples", idle, len(self.calib))
        return idle, False

    def _tokens_per_s(self, now):
        m = self.proxy_reader(self.cfg["proxy_port"])
        if m is None:
            self.tok.clear()
            return None, None
        self.tok.append((now, m.get("completion_tokens_live", 0)))
        t0, c0 = self.tok[0]
        tps = max(0.0, (self.tok[-1][1] - c0) / (now - t0)) if now > t0 else 0.0
        return round(tps, 1), m

    def step(self, now=None):
        now = time.time() if now is None else now
        cfg = self.cfg
        error = None
        try:
            gpu = self.gpu_reader()
            self.last_good, self.last_good_t = gpu, now
        except ValueError as e:
            error = str(e)
            gpu = self.last_good if now - self.last_good_t <= KEEP_LAST_GOOD_S else None

        if now - self.runtime_checked >= 5:
            self.runtime_up = self.runtime_probe(cfg["runtime_url"])
            self.runtime_checked = now
        tps, proxy = self._tokens_per_s(now)

        self.seq += 1
        primary = {"label": "GPU watts", "value": None, "unit": "W",
                   "scale_max": None, "log": False, "redline": None}
        fuel = {"label": "VRAM used (MiB)", "value": None, "of": None}
        detail = {
            "tokens_per_s": tps,
            "proxy": "up" if proxy is not None else "down",
            "runtime": "up" if self.runtime_up else "down",
            "active_requests": proxy.get("active_requests") if proxy else None,
            "model": proxy.get("last_model") if proxy else None,
        }
        if self.cpu:
            detail["cpu"] = self.cpu.detail(now)
        state = "IDLE"
        if gpu:
            idle, calibrating = self._idle_watts(gpu["watts"], now)
            if not calibrating:
                state = self.sm.update(gpu["watts"], gpu["limit"], idle, now)
            self.spark.append(round(gpu["watts"], 1))
            primary.update(value=gpu["watts"], scale_max=gpu["limit"],
                           redline=round(cfg["redline_watts_frac"] * gpu["limit"], 1))
            fuel.update(value=gpu["vram_used"], of=gpu["vram_total"])
            detail.update(gpu=gpu["name"], gpu_util_pct=gpu["util"], idle_watts=idle,
                          calibrating=calibrating, spark=list(self.spark))
        payload = {
            "ts": now, "seq": self.seq, "host": cfg["host"],
            "source_ok": gpu is not None,
            "stale_after_s": cfg["stale_after_s"], "poll_s": cfg["poll_s"],
            "dial": {"primary": primary,
                     "ring": {"label": "tokens per second", "value": tps},
                     "fuel": fuel, "state": state},
            "detail": detail,
            "error": error if gpu is None else None,
        }
        self.payload_bytes = json.dumps(payload).encode()
        return payload


class History:
    """Append-only JSONL: every 5 s while active, every 60 s while idle."""

    def __init__(self, path, days):
        self.path, self.days = path, days
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
                    continue  # a torn last line from a killed collector
        atomic_write(self.path, "".join(keep).encode())

    def add(self, p):
        now = p["ts"]
        if now - self.last_trim > 86400:
            self.trim(now)
        every = 60 if p["dial"]["state"] == "IDLE" else 5
        if now - self.last_write < every:
            return
        self.last_write = now
        d = p["dial"]
        line = {"ts": round(now, 1), "w": d["primary"]["value"], "vram": d["fuel"]["value"],
                "util": p["detail"].get("gpu_util_pct"), "tps": d["ring"]["value"],
                "state": d["state"], "ok": p["source_ok"]}
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")


class UsageTail:
    """The model's token burn, in new tokens per minute (changed 11/11/2026: the
    dial was metered spend; the wallet does not move while the plan covers use).

    Read from the usage export Quotalis keeps on this PC; `usage_export_path` in
    config.json names the file (rewritten about once a minute). Opened read-only;
    counters and timestamps only. The rate is the change between the two newest
    exports, so it moves once a minute. A missing or old export is answered with
    source_ok false: the dial shows OFFLINE with the reason, never zero.
    """

    def __init__(self, cfg, path=None, clock=time.time):
        path = path or cfg.get("usage_export_path")
        self.path = Path(path) if path else None
        self.cfg, self.clock = cfg, clock
        self.lock = threading.Lock()
        self.at = 0.0
        self.last = None        # newest export: (generated_at, counter rows)
        self.rates = None       # per minute, between the two newest exports
        self.spark = deque(maxlen=60)
        self.seq = 0
        self.result = self.offline("no reading yet")

    @staticmethod
    def offline(reason):
        return 200, json.dumps({"host": "model", "source_ok": False, "error": reason}).encode()

    FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "api_call_count")

    def read(self):
        d = json.loads(self.path.read_text(encoding="utf-8"))
        # One counter row per model, provider, task and session.
        rows = {(m.get("model"), m.get("billing_provider"), m.get("task"), m.get("session")):
                ([int(m.get(k) or 0) for k in self.FIELDS], m.get("first_seen"))
                for m in d.get("model_usage") or []}
        return (float(d["generated_at"]), rows), (d.get("current_model") or {}).get("model"), d.get("last_activity_at")

    @staticmethod
    def step(prev, cur):
        """New counts between two exports, row by row, so a row the export drops
        (old sessions age out) does not hide what the others did. A row that is
        new counts in full only if it began after the previous export. A row
        whose counter went down is not understood: no rate for that step."""
        new = [0, 0, 0, 0]
        for key, (now_counts, first_seen) in cur[1].items():
            if key in prev[1]:
                d = [b - a for a, b in zip(prev[1][key][0], now_counts)]
                if min(d) < 0:
                    return None
            elif isinstance(first_seen, (int, float)) and first_seen >= prev[0] - 120:
                d = now_counts
            else:
                continue
            new = [x + y for x, y in zip(new, d)]
        return new

    def payload(self):
        c, now = self.cfg, self.clock()
        if self.path is None:
            return self.offline("usage_export_path is not set in config.json")
        try:
            sample, model, active_at = self.read()
        except FileNotFoundError:
            return self.offline("usage export not found (Quotalis writes it)")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            return self.offline(f"usage export could not be read ({type(e).__name__})")
        if self.last is None or sample[0] != self.last[0]:
            prev, self.last = self.last, sample
            minutes = (sample[0] - prev[0]) / 60 if prev else 0
            new = self.step(prev, sample) if minutes > 0 else None
            self.rates = [x / minutes for x in new] if new else None
            if self.rates:
                self.spark.append(round(self.rates[0] + self.rates[1]))
            self.seq += 1
        age = now - sample[0]
        if age > c.get("usage_stale_after_s", 180):
            return self.offline(f"usage export is {age / 60:.0f} min old "
                                "(Quotalis writes it once a minute)")
        r = self.rates
        value = round(r[0] + r[1]) if r else None
        burn, red = c.get("usage_burn_tokens_per_min", 5000), c.get("usage_redline_tokens_per_min", 50000)
        state = ("IDLE" if not value else "REDLINE" if value >= red
                 else "BURNING" if value >= burn else "WARM")
        return 200, json.dumps({
            "ts": now, "seq": self.seq, "host": "model", "source_ok": True,
            "stale_after_s": 150, "poll_s": 5, "error": None,
            "dial": {"primary": {"label": "new tokens per minute" if r else
                                 "new tokens per minute, waiting for a second reading",
                                 "value": value, "unit": "tokens/min", "log": True,
                                 "scale_max": c.get("usage_scale_max_tokens_per_min", 100000),
                                 "redline": red},
                     "state": state, "ring": {"value": round(r[3], 1) if r else None}},
            # Shown on the board without the provider prefix, in its own fixed colour (tier 5).
            "detail": {"model": (model or "").rsplit("/", 1)[-1], "model_tier": 5,
                       "tokens_in_per_min": round(r[0]) if r else None,
                       "tokens_out_per_min": round(r[1]) if r else None,
                       "cache_read_per_min": round(r[2]) if r else None,
                       "api_calls_per_min": round(r[3], 1) if r else None,
                       "export_age_s": round(age),
                       "last_activity_age_s": round(now - active_at) if active_at else None,
                       "spark": list(self.spark),
                       "token_source": "the usage export kept by Quotalis (read-only); "
                                       "it changes once a minute"},
        }).encode()

    def get(self):
        with self.lock:
            if time.monotonic() - self.at >= 5:
                self.result = self.payload()
                self.at = time.monotonic()
            return self.result


class Server(ThreadingHTTPServer):
    allow_reuse_address = False  # on Windows, reuse would let a second copy bind silently
    daemon_threads = True


def abort_local(port):
    """Asks the counting proxy to cut its requests in flight. None if it is down."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/gauge-abort", data=b"",
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2) as r:
            return json.loads(r.read()).get("aborted", 0)
    except (OSError, ValueError):
        return None


def pause_remote(cfg, route="/kill"):
    """Presses pause (`/kill`) or resume (`/resume`) on the remote host collector, next to
    its metrics file. the agent pauses new runs; a turn already running finishes.

    The shared secret `remote_pause_token` stays in config.json on both machines;
    it travels in a header over Tailscale and is never served to the page.
    """
    url, secret = cfg.get("remote_url"), cfg.get("remote_pause_token")
    if not url or not secret:
        return {"ok": False, "message": "remote_url or remote_pause_token is not set "
                                        "in config.json"}
    word = "resume" if route == "/resume" else "pause"
    req = urllib.request.Request(
        urlsplit(url)._replace(path=route, query="").geturl(), data=b"", method="POST",
        headers={"X-Gauge-Kill": "1", "X-Gauge-Kill-Token": secret})
    try:
        # 10 s: the remote host gives its own pause call up to 5 s before it answers.
        with urllib.request.urlopen(req, timeout=10) as r:
            answer = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "message": f"the remote host refused the {word} request (HTTP {e.code})"}
    except (OSError, ValueError) as e:
        return {"ok": False, "message": f"remote host unreachable, {word} not sent ({type(e).__name__})"}
    if not isinstance(answer, dict):
        return {"ok": False, "message": "the remote host gave an answer that is not an object"}
    log.warning("kill switch: remote %s, answer %s", word, answer.get("message"))
    return {"ok": bool(answer.get("ok")), "killed": answer.get("killed"),
            "message": str(answer.get("message") or "the remote host gave no message")[:200]}


def make_handler(collector, relay, token, agents=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _token_ok(self, url):
            # Requests that arrive through `tailscale serve` carry forwarding headers;
            # those must present the static token. Direct loopback requests need none.
            if "X-Forwarded-For" in self.headers or "Tailscale-User-Login" in self.headers:
                given = parse_qs(url.query).get("t", [""])[0]
                return bool(token) and secrets.compare_digest(given, token)
            return True

        def do_POST(self):
            url = urlsplit(self.path)
            if not self._token_ok(url):
                return self._send(403, b'{"error": "token required"}')
            if url.path != "/kill" or agents is None:
                return self._send(404, b'{"error": "not found"}')
            # Only this page may press the button: a custom header a foreign web
            # page cannot send without a preflight this server never grants, and
            # an Origin that must match the address the page was served from.
            origin = self.headers.get("Origin")
            if self.headers.get("X-Gauge-Kill") != "1" or (
                    origin and urlsplit(origin).netloc != self.headers.get("Host")):
                return self._send(403, b'{"error": "not allowed"}')
            target = parse_qs(url.query).get("target", [""])[0]
            self._send(200, json.dumps(agents.kill(target)).encode())

        def _send(self, status, body, ctype="application/json"):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urlsplit(self.path)
            if not self._token_ok(url):
                return self._send(403, b'{"error": "token required"}')
            if agents is not None and url.path == "/metrics-c.json":
                return self._send(200, agents.claude.payload_bytes)
            if agents is not None and url.path == "/metrics-x.json":
                return self._send(200, agents.codex.payload_bytes)
            if url.path in ("/", "/gauge.html"):
                return self._send(200, (HERE / "gauge.html").read_bytes(),
                                  "text/html; charset=utf-8")
            if url.path == "/metrics-b.json":
                return self._send(200, collector.payload_bytes)
            if url.path == "/metrics-a.json":
                return self._send(*relay.get())
            self._send(404, b'{"error": "not found"}')

    return Handler


def main():
    handler = RotatingFileHandler(HERE / "collector-b.log", maxBytes=1_000_000, backupCount=1,
                                  encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")
    removed = scrub_env()
    cfg_path = HERE / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    if "--recalibrate" in sys.argv:
        cfg["idle_watts"] = None
    if not cfg.get("remote_token"):
        cfg["remote_token"] = secrets.token_urlsafe(24)
        atomic_write(cfg_path, json.dumps(cfg, indent=2).encode())

    cpu = CpuWatch(cfg)
    threading.Thread(target=cpu.run, daemon=True).start()
    collector = Collector(cfg, cfg_path, cpu=cpu)
    relay = UsageTail(cfg)
    agents = Agents(cfg, abort_local=lambda: abort_local(cfg["proxy_port"]),
                    pause_remote=lambda route: pause_remote(cfg, route))
    try:
        server = Server(("127.0.0.1", cfg["page_port"]),
                        make_handler(collector, relay, cfg["remote_token"], agents))
    except OSError as e:
        msg = f"collector_b: port {cfg['page_port']} is busy ({e}); not starting."
        log.error(msg)
        sys.exit(msg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("started on 127.0.0.1:%d; %d secret-looking env vars dropped",
             cfg["page_port"], removed)

    history = History(HERE / "history-b.jsonl", cfg["history_days"])
    was_ok = True
    next_t = time.monotonic()
    def agents_loop():  # its own thread, so a slow nvidia-smi never delays it
        t = time.monotonic()
        while True:
            try:
                agents.step()
            except Exception:
                log.exception("agents step failed")
            t += 1
            time.sleep(max(0.0, t - time.monotonic()))

    threading.Thread(target=agents_loop, daemon=True).start()
    while True:
        p = collector.step()
        atomic_write(HERE / "metrics-b.json", collector.payload_bytes)
        history.add(p)
        if p["source_ok"] != was_ok:
            log.warning("GPU source %s: %s", "ok" if p["source_ok"] else "lost", p["error"])
            was_ok = p["source_ok"]
        next_t += cfg["gpu_s"]
        time.sleep(max(0.0, next_t - time.monotonic()))


if __name__ == "__main__":
    main()
