"""Token-burn gauge: live tap on Claude Code's own API traffic.

Claude Code is pointed at this port (ANTHROPIC_BASE_URL) and the tap passes every
request to the real API untouched, streaming the answer back line by line. On the
way it reads the stream's own counters, so the gauge sees a request the moment it
is sent, not when a log file is written afterwards:

  * the moment a prompt leaves this PC, and its size in bytes
  * the input-token count the API reports when it accepts the prompt
  * the time to the first token of the answer
  * output as it streams (estimated from characters until the API's exact count
    arrives at the end of the answer)

It keeps numbers and timestamps only. Prompt text, answer text and request
headers (which carry the login) are passed through and never stored or logged.
"""
import http.client
import json
import logging
import socket
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "te", "upgrade",
       "proxy-authorization", "proxy-authenticate", "trailer"}
KEEP_S = 120          # seconds of per-second history kept
DEFAULT_CHARS_PER_TOKEN = 4.0
log = logging.getLogger("tap")


class Tap:
    """All counters. One lock; every method is quick."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.lock = threading.Lock()
        self.started = clock()
        self.seq = 0
        self.active = 0
        self.requests_total = 0
        self.upstream_errors = 0
        self.input_total = 0          # exact, from the API
        self.cache_read_total = 0
        self.cache_write_total = 0
        self.output_total = 0         # exact, settled at the end of each answer
        self.chars_done = 0           # for the characters-per-token ratio
        self.tokens_done = 0
        self.buckets = {}             # int second -> [input, cache_read, cache_write, output_est]
        self.recent = deque(maxlen=20)

    def ratio(self):
        if self.tokens_done >= 200 and self.chars_done:
            return max(1.0, min(8.0, self.chars_done / self.tokens_done))
        return DEFAULT_CHARS_PER_TOKEN

    def _bucket(self, now):
        sec = int(now)
        b = self.buckets.get(sec)
        if b is None:
            b = self.buckets[sec] = [0, 0, 0, 0.0]
            for old in [s for s in self.buckets if s < sec - KEEP_S]:
                del self.buckets[old]
        return b

    def begin(self, body_bytes):
        now = self.clock()
        with self.lock:
            self.seq += 1
            self.requests_total += 1
            self.active += 1
            req = {"id": self.seq, "sent": now, "body_bytes": body_bytes, "model": None,
                   "accepted_s": None, "first_token_s": None, "input": None, "cache_read": None,
                   "cache_write": None, "output_est": 0.0, "output": None, "chars": 0,
                   "done_s": None, "ended": None}
            self.recent.append(req)
            return req

    def accepted(self, req, usage, model):
        """`message_start`: the API has taken the prompt and says how big it was."""
        now = self.clock()
        with self.lock:
            req["accepted_s"] = round(now - req["sent"], 3)
            req["model"] = model if isinstance(model, str) else None
            vals = [int(usage.get(k) or 0) for k in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")]
            req["input"], req["cache_read"], req["cache_write"] = vals
            self.input_total += vals[0]
            self.cache_read_total += vals[1]
            self.cache_write_total += vals[2]
            b = self._bucket(now)
            for i in range(3):
                b[i] += vals[i]

    def delta(self, req, n_chars):
        now = self.clock()
        with self.lock:
            if req["first_token_s"] is None:
                req["first_token_s"] = round(now - req["sent"], 3)
            req["chars"] += n_chars
            est = n_chars / self.ratio()
            req["output_est"] += est
            self._bucket(now)[3] += est

    def settled(self, req, output_tokens):
        """The API's exact output count for this answer."""
        with self.lock:
            req["output"] = int(output_tokens)

    def end(self, req, how):
        now = self.clock()
        with self.lock:
            self.active -= 1
            req["done_s"] = round(now - req["sent"], 3)
            req["ended"] = how
            if req["output"] is not None:
                self.output_total += req["output"]
                if req["chars"] and req["output"]:
                    self.chars_done += req["chars"]
                    self.tokens_done += req["output"]
            else:
                self.output_total += round(req["output_est"])

    def snapshot(self):
        now = self.clock()
        sec = int(now)
        with self.lock:
            def col(i, n):
                return [round(self.buckets.get(s, (0, 0, 0, 0))[i], 1)
                        for s in range(sec - n + 1, sec + 1)]
            last = dict(self.recent[-1]) if self.recent else None
            if last:
                last.pop("chars", None)
                last["output_est"] = round(last["output_est"])
                last["age_s"] = round(now - last.pop("sent"), 1)
            # The second still filling reads low, so "now" is the last full second or
            # the current one, whichever is higher.
            out = col(3, 2)
            inp = [sum(x) for x in zip(col(0, 2), col(1, 2), col(2, 2))]
            return {
                "ts": now, "seq": self.seq, "started": self.started,
                "active_requests": self.active, "requests_total": self.requests_total,
                "upstream_errors": self.upstream_errors,
                "output_tokens_per_s": max(out), "input_tokens_per_s": max(inp),
                "output_per_s_60": col(3, 60),
                "input_per_s_60": [sum(x) for x in zip(col(0, 60), col(1, 60), col(2, 60))],
                "input_total": self.input_total, "cache_read_total": self.cache_read_total,
                "cache_write_total": self.cache_write_total, "output_total": self.output_total,
                "chars_per_token": round(self.ratio(), 2),
                "last_request": last,
            }


def delta_chars(obj):
    """Characters of answer in one stream event. The text itself is not kept."""
    d = obj.get("delta")
    if not isinstance(d, dict):
        return 0
    return sum(len(d[k]) for k in ("text", "thinking", "partial_json") if isinstance(d.get(k), str))


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def make_server(listen_port, upstream_url, tap=None):
    up = urlsplit(upstream_url)
    tap = tap or Tap()
    upstream = set()

    def connect():
        if up.scheme == "https":
            return http.client.HTTPSConnection(up.hostname, up.port or 443, timeout=600)
        return http.client.HTTPConnection(up.hostname, up.port or 80, timeout=600)

    def abort_all():
        with tap.lock:
            socks = list(upstream)
        n = 0
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
                n += 1
            except OSError:
                pass
        log.warning("kill switch: %d request(s) cut off", n)
        return n

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _own(self, body):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def forward(self):
            if self.command == "GET" and self.path == "/gauge-tap.json":
                return self._own(json.dumps(tap.snapshot()).encode())
            if self.command == "POST" and self.path == "/gauge-abort":
                return self._own(json.dumps({"aborted": abort_all()}).encode())

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers["Host"] = up.netloc
            headers["Accept-Encoding"] = "identity"
            counted = self.command == "POST" and self.path.split("?")[0].endswith("/v1/messages")
            req = tap.begin(length) if counted else None
            how = "error"
            conn = connect()
            sock = None
            try:
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    sock = conn.sock
                    with tap.lock:
                        upstream.add(sock)
                    resp = conn.getresponse()
                except OSError:
                    with tap.lock:
                        tap.upstream_errors += 1
                    msg = b'{"error": "token-gauge tap: the API is not reachable"}'
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(msg)))
                    self.end_headers()
                    self.wfile.write(msg)
                    return
                ctype = resp.getheader("Content-Type") or ""
                self.send_response(resp.status)
                for k, v in resp.getheaders():
                    if k.lower() not in HOP:
                        self.send_header(k, v)
                if "text/event-stream" in ctype:
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self._stream(resp, req)
                else:
                    data = resp.read()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    if req and "json" in ctype:
                        try:
                            obj = json.loads(data)
                            usage = obj.get("usage") if isinstance(obj, dict) else None
                            if isinstance(usage, dict):
                                tap.accepted(req, usage, obj.get("model"))
                                tap.settled(req, usage.get("output_tokens") or 0)
                        except ValueError:
                            pass
                how = f"http {resp.status}"
            except (OSError, ValueError, http.client.HTTPException):
                how = "cut off"  # the client went away, or the kill switch cut the stream
                self.close_connection = True
            finally:
                conn.close()
                with tap.lock:
                    upstream.discard(sock)
                if req:
                    tap.end(req, how)

        def _stream(self, resp, req):
            while True:
                line = resp.readline()
                if not line:
                    break
                self.wfile.write(line)
                if line == b"\n" or line == b"\r\n":
                    self.wfile.flush()  # one flush per event
                if req is None or not line.startswith(b"data:"):
                    continue
                try:
                    obj = json.loads(line[5:])
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                kind = obj.get("type")
                if kind == "content_block_delta":
                    n = delta_chars(obj)
                    if n:
                        tap.delta(req, n)
                elif kind == "message_start":
                    m = obj.get("message") or {}
                    if isinstance(m.get("usage"), dict):
                        tap.accepted(req, m["usage"], m.get("model"))
                elif kind == "message_delta":
                    u = obj.get("usage")
                    if isinstance(u, dict) and u.get("output_tokens") is not None:
                        tap.settled(req, u["output_tokens"])
            self.wfile.flush()

        do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = do_PATCH = forward

    server = Server(("127.0.0.1", listen_port), Handler)
    server.tap = tap
    return server


def main():
    handler = RotatingFileHandler(HERE / "tap.log", maxBytes=1_000_000, backupCount=1,
                                  encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    port = cfg.get("tap_port", 8832)
    upstream = cfg.get("tap_upstream", "https://api.anthropic.com")
    try:
        server = make_server(port, upstream)
    except OSError as e:
        msg = f"tap: port {port} is busy ({e}); not starting."
        log.error(msg)
        sys.exit(msg)
    log.info("tap on 127.0.0.1:%d -> %s", port, upstream)
    server.serve_forever()


if __name__ == "__main__":
    main()
