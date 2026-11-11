"""Live check on this PC (acceptance steps 11, 15-17): run one real local generation
through the counting proxy and measure how fast the gauge reacts.

Usage: python tests/live_generation_check.py <model-id>
Needs collector_b.py, proxy.py and the local runtime to be running.
"""
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text())
PAGE = f"http://127.0.0.1:{CFG['page_port']}"
PROXY = f"http://127.0.0.1:{CFG['proxy_port']}"
MARKER = "QUOKKA-MARKER-90417"


def get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        return json.loads(r.read())


samples = []
stop = threading.Event()


def watch():
    while not stop.is_set():
        try:
            p = get(PAGE + "/metrics-b.json")
            samples.append((time.time(), p["dial"]["primary"]["value"], p["dial"]["state"],
                            p["detail"]["tokens_per_s"]))
        except OSError:
            pass
        time.sleep(0.2)


def main():
    model = sys.argv[1]
    if len(sys.argv) > 2:  # seconds of rest first, so the page's burn flash is armed
        time.sleep(float(sys.argv[2]))
    threading.Thread(target=watch, daemon=True).start()
    time.sleep(3)
    before = get(PROXY + "/metrics.json")
    idle = get(PAGE + "/metrics-b.json")["detail"]["idle_watts"]
    body = json.dumps({
        "model": model, "stream": True, "max_tokens": 2500,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "user", "content":
                      f"Write about 1500 words on how lighthouses work. Ignore this tag: {MARKER}"}],
    }).encode()
    req = urllib.request.Request(PROXY + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t_start = time.time()
    t_first = None
    usage = None
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            if not line.startswith(b"data:") or b"[DONE]" in line:
                continue
            obj = json.loads(line[5:])
            if t_first is None and obj.get("choices"):
                t_first = time.time()
            usage = obj.get("usage") or usage
    t_end = time.time()
    time.sleep(3)
    stop.set()
    after = get(PROXY + "/metrics.json")

    moved = next((t for t, w, s, _ in samples if t >= t_start and s != "IDLE"), None)
    states = []
    for t, _, s, _ in samples:
        if t >= t_start - 1 and (not states or states[-1] != s):
            states.append(s)
    peak_w = max(w for t, w, _, _ in samples if t >= t_start)
    tps_seen = [x for t, _, _, x in samples if t_first and t_first + 1.5 <= t <= t_end - 0.5 and x]
    counted = after["completion_tokens_total"] - before["completion_tokens_total"]
    result = {
        "idle_watts": idle,
        "peak_watts": peak_w,
        "seconds_from_request_to_needle_leaving_idle": round(moved - t_start, 2) if moved else None,
        "seconds_from_request_to_first_token": round(t_first - t_start, 2) if t_first else None,
        "generation_seconds": round(t_end - t_start, 1),
        "state_sequence": states,
        "watts_by_second_from_request": [
            [round(t - t_start, 1), w, s] for t, w, s, _ in samples
            if -1 <= t - t_start <= 8][::2],
        "runtime_usage": usage,
        "proxy_counted_completion_tokens": counted,
        "runtime_tokens_per_s": round(usage["completion_tokens"] / (t_end - t_first), 1)
        if usage and t_first else None,
        "gauge_tokens_per_s_median": round(sorted(tps_seen)[len(tps_seen) // 2], 1)
        if tps_seen else None,
    }
    leaks = []
    for name in ("proxy.log", "collector-b.log", "metrics-b.json", "history-b.jsonl"):
        f = ROOT / name
        if f.exists() and MARKER in f.read_text(encoding="utf-8", errors="replace"):
            leaks.append(name)
    if MARKER in json.dumps(after):
        leaks.append("proxy /metrics.json")
    result["marker_found_in"] = leaks
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
