#!/usr/bin/env bash
# Unattended: settle -> calibrate -> monitors -> RCA service -> campaign -> score.
#
# Every step that needed a human judgement call now has a defensible default, and the two that could
# silently ruin the run refuse instead: calibration will not apply a partial result, and the campaign
# will not start if a metric family is missing.
set -euo pipefail
cd "$(dirname "$0")/../.."

SETTLE_SECONDS=${SETTLE_SECONDS:-3600}
MONITOR_SETTLE_SECONDS=${MONITOR_SETTLE_SECONDS:-300}
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

step "settling ${SETTLE_SECONDS}s for a quiet baseline (no injections in this window)"
sleep "$SETTLE_SECONDS"

step "calibrating thresholds from that baseline and writing them to monitors.yaml"
$PY datadog/monitors/apply.py calibrate --hours 1 --apply

step "applying monitors, webhook and dashboard"
$PY datadog/monitors/apply.py apply

step "letting monitors settle ${MONITOR_SETTLE_SECONDS}s"
sleep "$MONITOR_SETTLE_SECONDS"

step "starting the RCA service (poller: no tunnel needed)"
mkdir -p results
$PY -m app.cli poll >results/rca-service.log 2>&1 &
POLLER=$!
trap 'kill "$POLLER" 2>/dev/null || true' EXIT
sleep 10
kill -0 "$POLLER" 2>/dev/null || { echo "ABORT: poller died, see results/rca-service.log" >&2; exit 1; }

step "campaign: $CONFIG"
$PY experiments/live_eval.py --config "$CONFIG"

step "done. reports in results/incidents/, scores in results/eval/"
