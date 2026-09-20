# rca-sim

A live root cause analysis loop: Online Boutique runs on a local `kind` cluster under steady load,
Datadog collects its metrics, Chaos Mesh injects faults on demand, a Datadog monitor detects the
incident, and PRISM — the graph-free RCA method from
[`automated_root_cause_analysis`](https://github.com/ArthurrMrv/automated_root_cause_analysis) — ranks
the root cause from the window Datadog itself provides.

Every injection is logged as ground truth, so the whole loop is scorable: AC@k as in RCAEval, plus two
numbers the offline benchmark cannot give — how long detection takes, and what it costs not to know the
injection time.

- **Why each piece is the way it is:** `IMPLEMENTATION_PLAN.md` (decision log D1–D18, phases, risks).
- **How to work in this repo:** `CLAUDE.md` (architecture, data contract, conventions).
- **What has been checked against the live system:** `docs/verified.md`.

## Quick start

```bash
cp .env.example .env         # Datadog site, API key, app key, webhook secret
make venv                    # installs rca-service and PRISM (editable, PRISM_REPO=../…)
make test                    # 63 unit tests; no cluster and no Datadog account needed
make up                      # cluster + app + Agent + Collector + Chaos Mesh  (Phases 1–4)
make calibrate               # thresholds from a quiet hour, then edit datadog/monitors/monitors.yaml
make tunnel                  # public URL for the webhook (or skip it and use MODE=poll)
make monitors URL=https://<tunnel>/webhook
make rca                     # the RCA service
make inject FAULT=delay SERVICE=cartservice DURATION=300
```

About three minutes after the monitor fires, a report appears in `results/incidents/<id>/`. Then
`make eval` runs a whole campaign and `make sweep` re-ranks every stored incident at 1s / 5s / 15s
resolution.

`make help` lists every target.

## Status

The code and configuration for all eight phases are in place and the offline half is verified: the data
contract, the adapter, the window arithmetic, the webhook path and the scoring are covered by tests that
run PRISM for real on a Datadog-shaped payload. Everything that needs a cluster or a Datadog account —
metric names, tags, 5s collection, spanmetrics naming, monitor thresholds, webhook template variables —
is marked `VERIFY` in the code and tracked in `docs/verified.md`.
