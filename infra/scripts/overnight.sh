#!/usr/bin/env bash
# Unattended: settle -> calibrate -> monitors -> RCA service -> campaign -> score.
#
# Every step that needed a human judgement call now has a defensible default, and the two that could
# silently ruin the run refuse instead: calibration will not apply a partial result, and the campaign
# will not start if a metric family is missing.
set -euo pipefail
cd "$(dirname "$0")/../.."

# One run at a time. Two concurrent campaigns inject overlapping faults, so every ground-truth
# record names one service while two are broken -- which silently invalidates the whole experiment,
# and the scores look perfectly plausible afterwards. Cheap to prevent, impossible to detect later.
LOCK=${LOCK:-/tmp/rca-sim-overnight.lock}
exec 9>"$LOCK"
flock -n 9 || {
  echo "ABORT: another overnight run holds $LOCK. Holding processes:" >&2
  # The lock lives on an open fd, not on the file, so "who holds it" is a question about fds. Saying
  # "kill it first" without answering that sent someone hunting with pgrep for a process that was
  # not the holder.
  for proc in /proc/[0-9]*; do
    [ "${proc#/proc/}" = "$$" ] && continue  # this script's own fd 9 is not a holder worth reporting
    ls -l "$proc/fd" 2>/dev/null | grep -q "$(basename "$LOCK")" &&
      echo "  ${proc#/proc/}: $(tr "\0" " " <"$proc/cmdline")" >&2
  done
  exit 1
}

# Every long-running child below closes fd 9 (`9>&-`). Without that they inherit the lock, and an
# interrupted run leaves an orphaned `sleep` or poller holding it with the script long gone -- the
# next run then aborts against a lock whose owner does not appear in any process listing.

# The settle is not a warm-up: it is the input to the next step. `calibrate --hours 1` reads the
# preceding hour, so an injection inside it raises every threshold above the faults the night is
# meant to detect. Set it to 0 only when the cluster has already been quiet for an hour, or when
# CALIBRATE=0 makes the question moot.
SETTLE_SECONDS=${SETTLE_SECONDS:-3600}
MONITOR_SETTLE_SECONDS=${MONITOR_SETTLE_SECONDS:-300}
CALIBRATE=${CALIBRATE:-1}   # 0 reuses the thresholds already in monitors.yaml
CONFIG=${CONFIG:-experiments/configs/base.yaml}
PY=.venv/bin/python

step() { echo; echo "=== $(date -u '+%H:%M:%SZ')  $*"; }

step "checking every metric family is arriving before committing to the night"
$PY -m app.cli freshness | tee /tmp/freshness.txt
# error_rate legitimately has no series on a healthy baseline; the others must be live or the
# campaign would score faults against telemetry that was never collected.
if grep -vE '^error_rate' /tmp/freshness.txt | grep -q 'no data'; then
  echo "ABORT: a metric family other than error_rate has no data. Fix queries.yaml first." >&2
  exit 1
fi

if [ "$CALIBRATE" = 1 ]; then
  step "settling ${SETTLE_SECONDS}s for a quiet baseline (no injections in this window)"
  sleep "$SETTLE_SECONDS" 9>&-

  step "calibrating thresholds from that baseline and writing them to monitors.yaml"
  $PY datadog/monitors/apply.py calibrate --hours 1 --apply
else
  step "CALIBRATE=0: keeping the thresholds already in monitors.yaml"
fi

step "applying monitors, webhook and dashboard"
$PY datadog/monitors/apply.py apply

step "letting monitors settle ${MONITOR_SETTLE_SECONDS}s"
sleep "$MONITOR_SETTLE_SECONDS" 9>&-

step "starting the RCA service (poller: no tunnel needed)"
mkdir -p results
$PY -m app.cli poll >results/rca-service.log 2>&1 9>&- &
POLLER=$!
trap 'kill "$POLLER" 2>/dev/null || true' EXIT
sleep 10 9>&-
kill -0 "$POLLER" 2>/dev/null || { echo "ABORT: poller died, see results/rca-service.log" >&2; exit 1; }

step "campaign: $CONFIG"
$PY experiments/live_eval.py --config "$CONFIG" 9>&-

step "done. reports in results/incidents/, scores in results/eval/"
