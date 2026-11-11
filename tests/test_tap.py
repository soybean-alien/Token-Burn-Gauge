"""The live tap: what it reads from a stream, and that it keeps no text."""
import json
import sys
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tap  # noqa: E402

SECRET_TEXT = "PINEAPPLE-ANSWER-TEXT"
PROMPT_TEXT = "WALRUS-PROMPT-TEXT"
LOGIN = "made-up-login-value"


def sse(kind, obj):
    return f"event: {kind}\ndata: {json.dumps(dict(obj, type=kind))}\n\n".encode()


class FakeApi(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        FakeApi.seen.append((self.path, self.headers.get("Authorization"), self.rfile.read(n)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        w = self.wfile
        w.write(sse("message_start", {"message": {"model": "test-model", "usage": {
            "input_tokens": 12, "cache_read_input_tokens": 9000,
            "cache_creation_input_tokens": 300, "output_tokens": 1}}}))
        w.flush()
        time.sleep(0.3)
        for _ in range(4):
            w.write(sse("content_block_delta",
                        {"index": 0, "delta": {"type": "text_delta", "text": SECRET_TEXT}}))
            w.flush()
            time.sleep(0.05)
        w.write(sse("message_delta", {"delta": {}, "usage": {"output_tokens": 21}}))
        w.write(sse("message_stop", {}))


class TapTests(unittest.TestCase):
    def setUp(self):
        FakeApi.seen = []
        self.api = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        self.srv = tap.make_server(0, f"http://127.0.0.1:{self.api.server_address[1]}")
        for s in (self.api, self.srv):
            threading.Thread(target=s.serve_forever, daemon=True).start()
            self.addCleanup(s.server_close)
            self.addCleanup(s.shutdown)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def call(self):
        body = json.dumps({"stream": True, "messages": [{"role": "user", "content": PROMPT_TEXT}]})
        req = urllib.request.Request(self.base + "/v1/messages?beta=true", data=body.encode(),
                                     method="POST", headers={"Authorization": LOGIN})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.read(), len(body)

    def snap(self):
        with urllib.request.urlopen(self.base + "/gauge-tap.json", timeout=5) as r:
            raw = r.read()
        return json.loads(raw), raw

    def test_stream_passes_through_and_is_counted(self):
        out, sent_bytes = self.call()
        self.assertEqual(out.count(SECRET_TEXT.encode()), 4)          # the client got it all
        path, login, body = FakeApi.seen[0]
        self.assertEqual(path, "/v1/messages?beta=true")
        self.assertEqual(login, LOGIN)                                # login passed on untouched
        self.assertIn(PROMPT_TEXT.encode(), body)
        s, _ = self.snap()
        last = s["last_request"]
        self.assertEqual((last["input"], last["cache_read"], last["cache_write"]), (12, 9000, 300))
        self.assertEqual(last["output"], 21)                          # the API's exact figure
        self.assertEqual(last["body_bytes"], sent_bytes)
        self.assertEqual(last["model"], "test-model")
        self.assertGreaterEqual(last["first_token_s"], 0.3)           # time to first token
        self.assertLess(last["accepted_s"], last["first_token_s"])
        self.assertEqual(last["ended"], "http 200")
        self.assertEqual(s["output_total"], 21)
        self.assertEqual(s["active_requests"], 0)
        self.assertEqual(sum(s["input_per_s_60"]), 9312)              # the prompt spike
        self.assertAlmostEqual(sum(s["output_per_s_60"]), 4 * len(SECRET_TEXT) / 4, delta=0.5)

    def test_no_text_and_no_login_in_what_the_tap_serves(self):
        self.call()
        _, raw = self.snap()
        for forbidden in (SECRET_TEXT, PROMPT_TEXT, LOGIN):
            self.assertNotIn(forbidden.encode(), raw)

    def test_api_down_is_a_502_and_counted(self):
        self.api.shutdown()
        self.api.server_close()
        req = urllib.request.Request(self.base + "/v1/messages", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 502)
        s, _ = self.snap()
        self.assertEqual(s["upstream_errors"], 1)
        self.assertEqual(s["active_requests"], 0)

    def test_ratio_learns_from_finished_answers(self):
        t = tap.Tap()
        self.assertEqual(t.ratio(), 4.0)
        r = t.begin(10)
        t.delta(r, 3000)
        t.settled(r, 1000)
        t.end(r, "http 200")
        self.assertEqual(t.ratio(), 3.0)


if __name__ == "__main__":
    unittest.main()
