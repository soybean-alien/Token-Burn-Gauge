"""Points Claude Code at the live tap, or back at the real API.

    python tap_switch.py on      route Claude Code through the tap (new sessions only)
    python tap_switch.py off     remove the setting; Claude Code talks to the API directly
    python tap_switch.py         say which it is

It changes one key, env.ANTHROPIC_BASE_URL, in ~/.claude/settings.json and leaves
the rest of the file as it was. Sessions already running keep what they started with.
"""
import json
import os
import sys
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
HERE = Path(__file__).resolve().parent


def main():
    cfg = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
    url = f"http://127.0.0.1:{cfg.get('tap_port', 8832)}"
    text = SETTINGS.read_text(encoding="utf-8")
    data = json.loads(text)
    env = data.get("env") or {}
    now = env.get("ANTHROPIC_BASE_URL")
    want = sys.argv[1] if len(sys.argv) > 1 else None
    if want not in ("on", "off"):
        print("tap is ON:" if now == url else "tap is OFF:", now or "no ANTHROPIC_BASE_URL set")
        return
    if want == "on":
        env["ANTHROPIC_BASE_URL"] = url
    else:
        env.pop("ANTHROPIC_BASE_URL", None)
    data["env"] = env
    indent = 2 if '\n  "' in text else 4
    tmp = SETTINGS.with_suffix(".json.tap-tmp")
    tmp.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, SETTINGS)
    print(f"tap {want}: new Claude Code sessions will use",
          url if want == "on" else "the API directly")


if __name__ == "__main__":
    main()
