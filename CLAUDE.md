# rca-sim

Online Boutique on a local `kind` cluster, monitored by Datadog, with faults injected by Chaos Mesh.
When a Datadog monitor fires, it calls `rca-service`, which pulls the window from the Datadog API,
converts it to PRISM's input format and writes a ranked root-cause report. Every injection is logged as
ground truth, so the whole loop is scorable (AC@k, time to detect).

Design decisions and their rationale: `IMPLEMENTATION_PLAN.md`. Facts checked against the live system or
a pinned version: `docs/verified.md` — check there before re-deriving anything.

## Architecture

```
loadgen -> Online Boutique -(OTLP traces)-> OTel Collector -(spanmetrics)-> Datadog Agent -> Datadog
                 ^                                                              ^
            Chaos Mesh                                                   kubelet/container metrics
          (ground truth)                                                          |
                                                          monitor fires (1-min threshold, by service)
                                                                                  v
                          results/incidents/<id>/  <-  PRISM  <-  adapter  <-  rca-service (webhook|poller)
```

One kind node = one Datadog host. Metrics are collected at 5s (D8); the adapter resamples onto that grid.

## Data contract: adapter -> PRISM

Verified against `RCAEval/e2e/prism.py` and `RCAEval/io/time_series.py` (see `docs/verified.md`).

- `pandas.DataFrame`, one row per timestamp, regular `step_seconds` grid (default 5).
- Column **`time`**: integer **UTC unix seconds**. PRISM splits on `data["time"] < inject_time`, so
  `anomaly_time` is in the same unit. No timezone objects, no milliseconds.
- One column per metric, named **`{service}_{property}`**. PRISM splits on the *first* underscore:
  `component = col.partition("_")[0]`, `property = the rest`. Service names must therefore contain no
  underscore (`redis-cart`, not `redis_cart`).
- The property's **first token** (split on `_` and `-`, lowercased) decides its class, and a property in
  neither set is unclassified and does not contribute to the ranking:
  - internal: `cpu`, `mem`, `memory`, `disk`, `diskio`, `socket`, `sockets`
  - external: `latency`, `lat`, `error`, `errors`, `duration`, `rt`, `workload`
  So families are named `cpu`, `mem`, `latency_p95`, `error_rate`, `workload` — not `net_rx` or `p95`.
- Values are floats; NaN is allowed and reported. Columns that are all-NaN in either window, or constant,
  are dropped by PRISM's `preprocess`.
- `_mem` columns are divided by 1e6 by `preprocess` (bytes -> MB), so the adapter reports memory in bytes.
- `dataset` must be passed non-`None` to PRISM, otherwise `preprocess` is a no-op and the `time` column
  itself is scored as a property.
- Minimum length: `min_pre_points` before and `min_post_points` after `anomaly_time` (config, default
  30 / 12). `app/contract.py` enforces all of the above; `tests/test_contract.py` runs it on a fixture.

## Commands

`make help` lists them. `make up` / `make down` / `make status` for the cluster, `make monitors` for the
Datadog side, `make rca` runs the service, `make inject FAULT=cpu SERVICE=cartservice DURATION=300`,
`make analyze T=<unix>` runs PRISM on a past window offline, `make eval` runs a campaign, `make test`
runs the unit tests (no cluster and no Datadog account needed).

## Conventions

- Python >= 3.11, type hints on public functions, no framework beyond FastAPI + pandas + the Datadog client.
- **All timestamps are UTC unix seconds (int)**, everywhere, including ground truth and file names.
- Configuration lives in YAML (`app/adapter/queries.yaml`, `experiments/configs/*.yaml`) or env
  (`.env`, see `.env.example`). Never hardcode thresholds, windows or metric names in code.
- No secrets in git: `.env` and Kubernetes secrets only.
- PRISM is a dependency (`RCAEval`), never vendored or patched here. `app/runner.py` is the only file
  that imports it.
- Service names are normalized once, in `app/adapter/naming.py`. Everything downstream uses that form.
