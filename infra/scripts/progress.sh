#!/usr/bin/env bash
# Is a campaign running, and how far along? Answered from what the campaign writes, not from the log.
#
# `overnight.log` is the wrong place to look: Python block-buffers stdout when it is a file, so the
# `[n/25]` lines sit unflushed and a perfectly healthy run appears frozen for half an hour.
# ground_truth.jsonl is appended per injection and results/incidents/ per detection, so both move in
# real time.
#
# "Running" is decided by the lock rather than by pattern-matching `ps`: any command line mentioning
# overnight.sh matches such a pattern, including the one asking the question. The lock is held only
# by an actual run.
cd "$(dirname "$0")/../.."
LOCK=${LOCK:-/tmp/rca-sim-overnight.lock}

if flock -n "$LOCK" true 2>/dev/null; then
  echo "NOT RUNNING (finished, or stopped)"
else
  echo "RUNNING"
fi

.venv/bin/python - "$@" <<'PY'
import json, pathlib, time, yaml

config = yaml.safe_load(pathlib.Path("experiments/configs/base.yaml").read_text())
planned = config["repetitions"] * len(config["faults"]) * len(config["services"])

truth = pathlib.Path("results/ground_truth.jsonl")
rows = [json.loads(line) for line in truth.read_text().splitlines() if line.strip()] if truth.exists() else []
incidents = len(list(pathlib.Path("results/incidents").glob("*/"))) if pathlib.Path("results/incidents").is_dir() else 0

print(f"injected   {len(rows)} / {planned}")
print(f"incidents  {incidents}")
now = int(time.time())
for row in rows[-3:]:
    print(f"  {row['fault_type']:6} -> {row['target_service']:24} {(now - row['t_start']) // 60:>4} min ago")
PY
