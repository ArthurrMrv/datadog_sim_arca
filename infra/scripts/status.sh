#!/usr/bin/env bash
# Phase 1-3 health at a glance: pods, Agent checks, Collector, and how fresh Datadog's data is.
set -euo pipefail
cd "$(dirname "$0")/../.."

echo "== pods"
kubectl get pods -A --field-selector=status.phase!=Running 2>/dev/null | tail -n +1
kubectl -n shop get pods -o wide

echo; echo "== restarts in shop (non-zero means an unfinished fault or a real problem)"
kubectl -n shop get pods \
  -o custom-columns='POD:.metadata.name,RESTARTS:.status.containerStatuses[0].restartCount'

echo; echo "== Datadog Agent checks"
agent=$(kubectl -n datadog get pods -l app.kubernetes.io/component=agent -o name | head -1)
[[ -n "$agent" ]] && kubectl -n datadog exec "$agent" -c agent -- agent status 2>/dev/null \
  | grep -A3 -E '^\s+(kubelet|container|otlp)' || echo "agent not ready"

echo; echo "== OTel Collector"
kubectl -n observability get deployment otel-collector 2>/dev/null || echo "not deployed"
kubectl -n observability logs deployment/otel-collector --tail=5 2>/dev/null || true

echo; echo "== freshness of the metrics the adapter reads"
.venv/bin/python -m app.cli freshness \
  || echo "(needs the venv and .env with DD_API_KEY/DD_ACCESS_TOKEN: run make venv)"
