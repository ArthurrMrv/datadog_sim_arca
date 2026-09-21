# rca-sim

Online Boutique runs on a local `kind` cluster under steady load. Datadog collects its metrics. Chaos
Mesh injects faults and logs each one as ground truth. When a Datadog monitor fires, it calls
`rca-service`, which pulls that window from the Datadog API, converts it into PRISM's input format, and
writes a ranked root-cause report.

**Why this exists.** PRISM (Pham, arXiv:2601.21359) was evaluated offline on RCAEval's recorded
datasets, where the injection time is known, the metrics are pre-cleaned and the resolution is fixed.
None of that holds in production. This repo puts the same PRISM code behind a real monitoring stack so
the method can be scored where detection is noisy and the data is whatever the Agent happened to
collect — and, because every injection is logged, scored *numerically* (AC@k, time to detect), not
demonstrated.

Decisions and their rationale: `IMPLEMENTATION_PLAN.md` (D1–D18). Facts checked against the live
system, the Datadog API or a pinned version: `docs/verified.md` — **read it before re-deriving
anything**, it exists so the same guess is not made twice.

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

Why it is shaped this way:

- **Real services, not a simulator.** A simulator would encode our own assumptions about how faults
  propagate, so evaluating a propagation-aware method on it would be circular.
- **Datadog is the detector, not just the store.** The monitor decides *when* an incident starts, so
  detection time is measured rather than assumed. PRISM then has to work from an estimated anomaly
  time, which is the part the offline benchmark never tests.
- **spanmetrics instead of APM.** Latency and error rate are the signals that carry delay and code
  faults; CPU and memory alone miss them. The student plan has no APM, but spanmetrics turns traces
  into *ordinary metrics*, so monitors, webhooks and the query API all keep working.
- **One kind node** = one Datadog host, which keeps the plan's host budget untouched.
- **5s collection.** A 1-minute window holds four points at the 15s default — far too few for PRISM's
  per-property statistics. The adapter resamples onto that 5s grid so families collected
  independently line up exactly.

## PRISM lives in `app/prism/`

Both files are copies from `ArthurrMrv/automated_root_cause_analysis`: `prism.py` from
`RCAEval/e2e/prism.py` with a single import line repointed, and `time_series.py` from `RCAEval/io/`
untouched. **Do not edit either.** Logic, hyperparameters and architecture are the published ground
truth; a local "improvement" would mean the live numbers no longer compare with the offline study.
Fix PRISM upstream and re-copy the file.

Why vendored rather than installed: `RCAEval.e2e.__init__` imports every RCA method the fork ships and,
outside Python 3.10/3.12/3.14, does so unguarded — so `import RCAEval.e2e.prism` drags in torch,
matplotlib and causal-learn. PRISM needs numpy and pandas. `tests/test_prism.py` runs the upstream
`_demo()`, which asserts every claim in the paper and all 120 documented configurations, so the copy
cannot drift silently. `app/runner.py` is the only module that touches it.

## Data contract: adapter -> PRISM

Read out of the PRISM source, not assumed (`docs/verified.md` records the evidence for each line). It
is written down because every rule below fails *silently*: a violation produces a plausible ranking
computed from the wrong thing.

- `pandas.DataFrame`, one row per timestamp, on a regular `step_seconds` grid (default 5).
- Column **`time`**: integer **UTC unix seconds**. PRISM splits on `data["time"] < inject_time`, so
  `anomaly_time` is the same unit. No timezone objects, no milliseconds.
- One column per metric, named **`{service}_{property}`**. PRISM splits on the *first* underscore, so a
  service name containing one invents a shorter service (`redis-cart`, never `redis_cart`).
- The property's **first token** (split on `_`/`-`, lowercased) decides its class. A property in
  neither set is *dropped from the ranking* — no error, just absent evidence:
  - internal: `cpu`, `mem`, `memory`, `disk`, `diskio`, `socket`, `sockets`
  - external: `latency`, `lat`, `error`, `errors`, `duration`, `rt`, `workload`

  So families are `cpu`, `mem`, `latency_avg`, `error_rate`, `workload` — `net_rx` or `p95` would be
  collected, stored, charted, and never scored.
- Floats; NaN allowed and reported. PRISM's `preprocess` drops constant columns and any column all-NaN
  in a window.
- `_mem` columns are divided by 1e6 by `preprocess`, so the adapter reports memory in **bytes**.
- `dataset` must be non-`None`, otherwise `preprocess` is a no-op and `time` is itself scored as a
  property of a service called "time".
- At least `min_pre_points` before and `min_post_points` after `anomaly_time` (default 30 / 12), so a
  stalled Agent fails loudly instead of ranking from four points.

`app/contract.py` enforces all of it; `tests/test_contract.py` runs it on a recorded fixture.

## Commands

`make help` lists them. `make up` / `make down` / `make status`, `make monitors URL=...` for the
Datadog side, `make rca` (add `MODE=poll` to skip the tunnel), `make inject FAULT=cpu
SERVICE=cartservice DURATION=300`, `make analyze T=<unix>` to rank a past window with no alert
involved, `make replay ID=...` to re-rank a stored incident, `make eval`, `make sweep`, `make test`,
`make lint`.

After `make up`, run `make status`: it prints the age of the newest point per metric family. A family
reading "no data" means a query in `queries.yaml` does not match what the cluster actually emits, and
that is the failure mode to catch before a campaign, not during one.

Then run `make smoke` before `make overnight`. It is the same script end to end on one short
injection with the waits removed (~6 min), because the expensive failures here are not wrong answers
but a loop that never ran: a rejected manifest, a monitor that does not fire, a poller that dies.
Six hours is a bad time to find out. `make overnight`'s own waits are environment variables
(`SETTLE_SECONDS`, `MONITOR_SETTLE_SECONDS`, `CALIBRATE`) rather than constants for the same reason
— but the 3600s settle is not a warm-up, it is the hour `calibrate --hours 1` reads, so zeroing it
on a cluster that is still being injected into calibrates the thresholds above the faults.

A config carrying `scorable: false` (`smoke.yaml`) is exempt from the campaign pacing invariant and
is never scored: an AC@k off a smoke run would look exactly like a result.

**Five of the twelve services emit no external properties** — `cartservice`, `adservice`,
`shippingservice`, `redis-cart`, `loadgenerator` have never produced a span, because the demo's C#,
Java and Go leaves ignore `ENABLE_TRACING` (`docs/verified.md`). PRISM's discriminator needs both
classes, so those components rank low however large their CPU or memory anomaly is — measured, not
assumed: a cpu fault drove `cartservice_cpu` 23x and PRISM still ranked it 5th. `base.yaml` keeps
cartservice as a declared negative control, which is why scores are reported per service as well as
per fault type. Read that breakdown before the overall AC@k.

## Conventions

- Python >= 3.11, type hints on public functions. Nothing beyond FastAPI, pandas and the Datadog
  client — a research pipeline that is hard to install does not get re-run.
- **All timestamps are UTC unix seconds (int)**, everywhere, including ground truth and directory
  names. A single millisecond field would shift a window by 1000x and the ranking would still look
  reasonable.
- Thresholds, windows and metric names live in YAML (`app/adapter/queries.yaml`,
  `datadog/monitors/monitors.yaml`, `experiments/configs/*.yaml`), never in code, so a change to the
  detector shows up in the same diff as the experiment it affects.
- No secrets in git: `.env` and Kubernetes secrets only.
- Service names are normalized once, in `app/adapter/naming.py`, because `kube_deployment` and OTel's
  `service.name` must collapse to one key or a service's internal and external properties end up on
  two different components — which is precisely the distinction PRISM ranks on.
- Every unverified assumption about Datadog or a pinned version is marked `VERIFY(docs/verified.md)` in
  the file where it matters, and resolved there once checked.
