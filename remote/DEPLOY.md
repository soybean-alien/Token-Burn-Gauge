# Token-burn gauge: deploying the remote host half

Written 11/11/2026 by Claude Code on the Windows PC. These files were tested only on
Windows against a fixture database. Nothing here has run on the remote host. Target folder:
`/opt/data/scripts/token-gauge/` (the agent's lane).

## Files to copy

| File | From (Windows) | Purpose |
|---|---|---|
| `collector_a.py` | `%USERPROFILE%\.token-gauge\remote\` | the collector |
| `config.json` | `%USERPROFILE%\.token-gauge\remote\` | cadences, thresholds, paths |
| `run-a.sh` | `%USERPROFILE%\.token-gauge\remote\` | cron launcher (LF line endings) |
| `gauge.html` | `%USERPROFILE%\.token-gauge\` | the page, for viewing on the remote host itself |

## Steps

1. Copy the four files into `/opt/data/scripts/token-gauge/` and run `chmod +x run-a.sh`.
2. Check Python: `python3 --version` must be 3.9 or newer. No pip installs are needed.
3. Check the two things this PC could not see, and edit the code or config if they differ:
   - `last_seen` in `session_model_usage`: the collector accepts epoch seconds, epoch
     milliseconds or ISO text. Run
     `sqlite3 'file:/opt/data/state.db?mode=ro' 'select last_seen from session_model_usage limit 1'`.
   - `/opt/data/state/nous-spend.json`: the collector takes the last item of a list of
     snapshots (or of the first list inside an object) and reads `credits_remaining`,
     `period_end` and `ts` from it.
4. Run it once by hand: `./run-a.sh &`, then `cat metrics-a.json` after 10 s. Expect
   `"source_ok": true`, a `seq` that rises every 5 s, and an estimated USD per hour of 0
   while the agent is idle.
5. Run the plan's checks 2 to 8 (section 4.1). The same checks passed here against the
   fixture; check 4 can use `detail.since_start.output_tokens`, which is the exact sum of
   output-token deltas since the collector started.
6. Install the cron entry: `* * * * * /opt/data/scripts/token-gauge/run-a.sh`.
   The collector takes `collector-a.lock` and a second copy exits at once, so cron only
   restarts it after a crash.
7. Page on the remote host: `http://127.0.0.1:8830/` (the collector serves `gauge.html` and
   `metrics-a.json` on loopback only; set `"serve_port": null` to turn that off).

## Wallet balance (decision D7)

By default the collector reads the newest snapshot from `nous-spend.json`, which the
existing cron job refreshes every 15 minutes; the page shows how old the balance is. To
get the 60-second read the plan asks for, set `wallet_command` in `config.json` to a
command that prints one JSON object with `credits_remaining` (and optionally
`period_end`) on stdout. That command must own its own auth: `run-a.sh` starts the
collector with an empty environment, and no credential belongs in `config.json`.

## Letting the Windows page read the remote host dial

The Windows collector fetches `metrics-a.json` server-side from the URL in its
`remote_url` setting. The remote host server binds loopback only, so expose it to the
tailnet and nowhere else, for example:

    tailscale serve --bg --tcp 8830 tcp://127.0.0.1:8830

Then on the Windows PC set `"remote_url": "http://<remote-address>:8830/metrics-a.json"`
in `%USERPROFILE%\.token-gauge\config.json` and restart `collector_b.py`. If the agent runs
in a container, the port has to be published to the host's loopback first. This step was
not tested.

## Known differences from the plan

- The needle uses a 30-second rolling window (`rate_window_s`), not a single 5-second
  delta, so one API call does not flick the needle to a huge value and back. It still
  moves on the first sample after activity.
- Low credits turn on a fuel light (`detail.low_fuel`); they do not force REDLINE.
- History is written every sample while active and once a minute while idle, to stay
  inside the plan's 20 MB per month budget.
