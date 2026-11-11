"""Tests for the counting proxy, against a fake runtime on a loopback port."""
import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import proxy  # noqa: E402

MARKER = "ZEBRA-MARKER-7731"
CHUNKS = [f"word{i} {MARKER} " for i in range(12)]


def sse_body(with_usage):
    out = b""
    for c in CHUNKS:
        frame = {"model": "fake-model", "choices": [{"delta": {"content": c}}]}
        out += b"data: " + json.dumps(frame).encode() + b"\n\n"
    if with_usage:
        frame = {"model": "fake-model", "choices": [],
                 "usage": {"prompt_tokens": 9, "completion_tokens": 30}}
        out += b"data: " + json.dumps(frame).encode() + b"\n\n"
    return out + b"data: [DONE]\n\n"


class FakeRuntime(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        FakeRuntime.seen.append(body)
        req = json.loads(body)
        if req.get("stream"):
            data, ctype = sse_body(req.get("usage", True)), "text/event-stream"
        else:
            data = json.dumps({"model": "fake-model",
                               "choices": [{"message": {"content": MARKER}}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 7}}).encode()
            ctype = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ProxyTest(unittest.TestCase):
    def setUp(self):
        FakeRuntime.seen = []
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeRuntime)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.px = proxy.make_server(0, f"http://127.0.0.1:{self.up.server_address[1]}")
        threading.Thread(target=self.px.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.px.server_address[1]}"

    def tearDown(self):
        for s in (self.px, self.up):
            s.shutdown()
            s.server_close()

    def post(self, payload):
        body = json.dumps(payload).encode()
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            return body, r.read()

    def metrics(self):
        with urllib.request.urlopen(self.base + "/metrics.json", timeout=3) as r:
            raw = r.read()
        return json.loads(raw), raw

    def test_bound_to_loopback_only(self):
        self.assertEqual(self.px.server_address[0], "127.0.0.1")

    def test_stream_is_forwarded_untouched_and_counted_from_usage(self):
        sent, got = self.post({"stream": True, "messages": [{"role": "user", "content": MARKER}]})
        self.assertEqual(got, sse_body(True))          # byte for byte
        self.assertEqual(FakeRuntime.seen[-1], sent)   # request body unchanged
        m, _ = self.metrics()
        self.assertEqual(m["completion_tokens_total"], 30)
        self.assertEqual(m["completion_tokens_live"], 30)  # 12 chunks, reconciled to usage
        self.assertEqual(m["prompt_tokens_total"], 9)
        self.assertEqual((m["requests_total"], m["active_requests"]), (1, 0))
        self.assertEqual(m["last_model"], "fake-model")

    def test_stream_without_usage_falls_back_to_the_chunk_count(self):
        self.post({"stream": True, "usage": False})
        m, _ = self.metrics()
        self.assertEqual(m["completion_tokens_total"], len(CHUNKS))
        self.assertEqual(m["completion_tokens_live"], len(CHUNKS))

    def test_non_stream_usage(self):
        _, got = self.post({"stream": False})
        self.assertIn(MARKER.encode(), got)
        m, _ = self.metrics()
        self.assertEqual((m["prompt_tokens_total"], m["completion_tokens_total"]), (5, 7))

    def test_no_prompt_or_reply_text_is_kept_anywhere(self):  # acceptance step 17
        self.post({"stream": True, "messages": [{"role": "user", "content": MARKER}]})
        self.post({"stream": False, "messages": [{"role": "user", "content": MARKER}]})
        _, raw = self.metrics()
        self.assertNotIn(MARKER.encode(), raw)
        with urllib.request.urlopen(self.base + "/metrics", timeout=3) as r:
            self.assertNotIn(MARKER.encode(), r.read())
        self.assertNotIn(MARKER, json.dumps(vars(self.px.counters), default=str))

    def test_runtime_down_gives_a_clear_502(self):
        self.up.shutdown()
        self.up.server_close()
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.post({"stream": False})
        self.assertEqual(e.exception.code, 502)
        m, _ = self.metrics()
        self.assertEqual((m["upstream_errors"], m["active_requests"]), (1, 0))
        self.up = ThreadingHTTPServer(("127.0.0.1", 0), FakeRuntime)  # for tearDown
        threading.Thread(target=self.up.serve_forever, daemon=True).start()


if __name__ == "__main__":
    unittest.main()
