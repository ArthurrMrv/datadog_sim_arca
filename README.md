# rca-sim

A live root cause analysis loop: Online Boutique runs on a local `kind` cluster under steady load,
Datadog collects its metrics, Chaos Mesh injects faults on demand, a Datadog monitor detects the
incident, and PRISM — the graph-free RCA method from
[`automated_root_cause_analysis`](https://github.com/ArthurrMrv/automated_root_cause_analysis),
vendored unchanged in `rca-service/app/prism/` — ranks the root cause from the window Datadog itself
provides.

Every injection is logged as ground truth, so the whole loop is scorable: AC@k as in RCAEval, plus two
numbers the offline benchmark cannot give — how long detection takes, and what it costs not to know the
injection time.

- **Why each piece is the way it is:** `IMPLEMENTATION_PLAN.md` (decision log D1–D18, phases, risks).
- **How to work in this repo:** `CLAUDE.md` (architecture, data contract, conventions).
- **What has been checked against the live system:** `docs/verified.md`.

## Quick start

```bash
cp .env.example .env         # Datadog site, API key, app key, webhook secret
make venv                    # installs rca-service; PRISM is in-tree, numpy + pandas only
make test                    # 69 unit tests; no cluster and no Datadog account needed
make up                      # cluster + app + Agent + Collector + Chaos Mesh  (Phases 1–4)
make status                  # first thing after `up`: is every metric family arriving?
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

The code and configuration for all eight phases are in place and the offline half is verified: the
data contract, the adapter, the window arithmetic, the webhook path and the scoring are covered by
tests that run PRISM for real on a Datadog-shaped payload, and the vendored PRISM is checked against
upstream's own demo. Every container metric name and all five monitor definitions have been validated
against the Datadog API, and the spanmetrics configuration against the connector's documentation
(`docs/verified.md`). What is left open needs the cluster running: tag presence, 5s collection, the
demo's tracing env vars, and calibrated thresholds. `make status` is what surfaces them.
