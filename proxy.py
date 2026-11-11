"""Token-burn gauge: counting proxy in front of the local model runtime.

Forwards every request to the runtime untouched, streams the response back
byte for byte, and counts tokens from `usage` (or from streamed chunks while a
stream is running). It never stores or logs request or response text. If this
proxy is not running, clients can still use the runtime's own port directly.
"""
import http.client
import json
import logging
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
HOP = {"connection", "keep-alive", "transfer-encoding", "content-length", "te", "upgrade",
       "proxy-authorization", "proxy-authenticate", "trailer"}
log = logging.getLogger("proxy")


class Counters:
    def __init__(self):
        self.lock = threading.Lock()
        self.started = time.time()
        self.requests_total = 0
        self.active_requests = 0
        self.prompt_tokens_total = 0
        self.completion_tokens_total = 0  # settled figure, from `usage` where given
        self.completion_tokens_live = 0   # moves during a stream, reconciled at its end
        self.upstream_errors = 0
        self.last_model = None

    def snapshot(self):
        with self.lock:
            return {
                "ts": time.time(), "started": self.started,
                "requests_total": self.requests_total,
                "active_requests": self.active_requests,
                "prompt_tokens_total": self.prompt_tokens_total,
                "completion_tokens_total": self.completion_tokens_total,
                "completion_tokens_live": self.completion_tokens_live,
                "upstream_errors": self.upstream_errors,
                "last_model": self.last_model,
            }


def chunk_has_text(obj):
    for ch in obj.get("choices") or []:
        d = ch.get("delta") or {}
        if d.get("content") or d.get("reasoning_content") or d.get("tool_calls") or ch.get("text"):
            return True
    return False


class Server(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


def make_server(listen_port, upstream_url):
    up = urlsplit(upstream_url)
    counters = Counters()
    upstream = set()  # sockets to the runtime with a request in flight

    def abort_all():
        """Kill switch: cut every request in flight. The runtime stops generating
        when its connection closes, exactly as when a client walks away."""
        with counters.lock:
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

        def _own(self, body, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _record_usage(self, obj, live_counted):
            usage = obj.get("usage") if isinstance(obj, dict) else None
            with counters.lock:
                if isinstance(obj, dict) and isinstance(obj.get("model"), str):
                    counters.last_model = obj["model"]
                if isinstance(usage, dict):
                    done = int(usage.get("completion_tokens") or 0)
                    counters.prompt_tokens_total += int(usage.get("prompt_tokens") or 0)
                    counters.completion_tokens_total += done
                    counters.completion_tokens_live += done - live_counted
                    return True
            return False

        def forward(self):
            if self.command == "GET" and self.path in ("/metrics.json", "/metrics"):
                snap = counters.snapshot()
                if self.path == "/metrics.json":
                    return self._own(json.dumps(snap).encode(), "application/json")
                text = "".join(f"token_gauge_{k} {v}\n" for k, v in snap.items()
                               if isinstance(v, (int, float)))
                return self._own(text.encode(), "text/plain; version=0.0.4")
            if self.command == "POST" and self.path == "/gauge-abort":
                return self._own(json.dumps({"aborted": abort_all()}).encode(),
                                 "application/json")

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers["Host"] = up.netloc
            headers["Accept-Encoding"] = "identity"
            conn = http.client.HTTPConnection(up.hostname, up.port or 80, timeout=600)
            with counters.lock:
                counters.requests_total += 1
                counters.active_requests += 1
            sock = None
            try:
                try:
                    conn.request(self.command, self.path, body=body, headers=headers)
                    sock = conn.sock  # http.client lets go of it once the reply starts
                    with counters.lock:
                        upstream.add(sock)
                    resp = conn.getresponse()
                except OSError:
                    with counters.lock:
                        counters.upstream_errors += 1
                    msg = b'{"error": "token-gauge proxy: the runtime is not reachable"}'
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
                    self._stream(resp)
                else:
                    data = resp.read()
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    if "json" in ctype:
                        try:
                            self._record_usage(json.loads(data), 0)
                        except ValueError:
                            pass
            except (OSError, ValueError, http.client.HTTPException):
                # The client went away, or the kill switch cut the upstream.
                self.close_connection = True
            finally:
                conn.close()
                with counters.lock:
                    counters.active_requests -= 1
                    upstream.discard(sock)

        def _stream(self, resp):
            live = 0
            settled = False
            while True:
                line = resp.readline()
                if not line:
                    break
                self.wfile.write(line)
                self.wfile.flush()
                if not line.startswith(b"data:"):
                    continue
                try:
                    obj = json.loads(line[5:])
                except ValueError:
                    continue  # "[DONE]" or a partial frame
                if not isinstance(obj, dict):
                    continue
                if self._record_usage(obj, live):
                    settled = True
                elif chunk_has_text(obj):
                    live += 1
                    with counters.lock:
                        counters.completion_tokens_live += 1
            if not settled:  # the runtime sent no usage frame: the chunk count stands
                with counters.lock:
                    counters.completion_tokens_total += live

        do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = do_PATCH = forward

    server = Server(("127.0.0.1", listen_port), Handler)
    server.counters = counters
    return server


def main():
    handler = RotatingFileHandler(HERE / "proxy.log", maxBytes=1_000_000, backupCount=1,
                                  encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    try:
        server = make_server(cfg["proxy_port"], cfg["runtime_url"])
    except OSError as e:
        msg = f"proxy: port {cfg['proxy_port']} is busy ({e}); not starting."
        log.error(msg)
        sys.exit(msg)
    log.info("proxy on 127.0.0.1:%d -> %s", cfg["proxy_port"], cfg["runtime_url"])
    server.serve_forever()


if __name__ == "__main__":
    main()
