# Verified facts

Every **VERIFY** item from `IMPLEMENTATION_PLAN.md` ends up here with how it was checked and when, so
the same guess is not made twice. Anything still `OPEN` needs a running cluster and is marked
`VERIFY(docs/verified.md)` in the file where it matters.

## PRISM input format (Phase 0, Section 6) — read from source, 2026-09-20

From `ArthurrMrv/automated_root_cause_analysis`: `RCAEval/e2e/prism.py` and
`RCAEval/io/time_series.py`, both now vendored verbatim in `rca-service/app/prism/`.

| Fact | Evidence |
|---|---|
| Entry point `prism(data, inject_time=..., dataset=...)` -> `{"node_names", "ranks", "diagnostics"}` | `prism()` signature and return |
| `prismv2` is the same function under another name; both are exported | end of `prism.py` |
| `ranks` are `"{component}_{witness property}"`, best first. **No scores are returned** | `_rank_components`, `prism()` return |
| Time column is literally `"time"`, compared with `<`, so both sides are plain numbers | `is_normal = (data["time"] < inject_time)` |
| Unit is UTC unix seconds | `main.py:325` reads `inject_time.txt` as `int` and compares it with the dataset's `time` column |
| Component = text before the **first** underscore | `_split_property` uses `str.partition("_")` |
| Property class from the **first** token of the property, split on `_`/`-` | `_property_class` |
| internal = cpu, mem, memory, disk, diskio, socket, sockets | `INTERNAL_PROPERTIES` |
| external = latency, lat, latency-90, error, errors, duration, rt, workload | `EXTERNAL_PROPERTIES` |
| Unclassified properties are excluded from scoring, appended after the ranked components | `prism()`, `extra` |
| `dataset=None` makes `preprocess` a **no-op** — `time` is not even dropped | `preprocess()` first branch |
| With `dataset` set: constant columns dropped, `time` dropped, every `*_mem` column divided by 1e6 | `preprocess`, `convert_mem_mb` |
| Both windows must be non-empty over commonly observed columns, else `ValueError` | `prism()` guard |
| A single NaN does not poison a pooled score | `_POOL_FUNCS` are the `nan*` variants |
| Defaults are `scorer="zscore"`, `pooling="max"`, `combine="additive"` (Table 5) | `prism()` signature |

Applied in `app/adapter/queries.yaml`: families named so their first token classifies; `restarts` kept
for the report only (no matching class); memory reported in bytes; service names use `-`, never `_`.

## PRISM packaging — decided 2026-09-20

`RCAEval/e2e/__init__.py` imports every RCA method the fork ships and guards those imports only on
Python 3.10, 3.12 and 3.14 (`is_py310() or is_py312() or is_py314()`). **On 3.11 it takes the unguarded
branch**, so `import RCAEval.e2e.prism` pulls in matplotlib, torch and causal-learn. PRISM itself needs
numpy, pandas and `RCAEval.io.time_series`.

Resolved by vendoring both files into `rca-service/app/prism/` (D14, amended). The copy is proven
faithful by `tests/test_prism.py::test_upstream_demo_passes`, which runs upstream's own `_demo()`:
every claim of the paper under its stated condition, each gap resolution, and all 120 documented
configurations of scorer x pooling x combiner x time aggregation.

## Datadog — checked against the account and the API, 2026-09-20

Checked through the Datadog MCP server against the org this session is connected to.

| Item | Result |
|---|---|
| Org state | Empty: no hosts, and the only metrics present are Datadog's own `datadog.*` usage metrics. Nothing has ever reported, so metric *names* were checked against Datadog's catalog and *values* remain unverified until `make up` runs. |
| `container.cpu.usage` | Exists. gauge, unit **nanocore**, `container` integration |
| `container.memory.usage` | Exists. gauge, unit **byte** — confirms reporting memory in bytes and letting `preprocess` do the MB conversion |
| `container.cpu.limit` | Exists. gauge, nanocore — the denominator of the CPU saturation monitor |
| `container.memory.limit` | Exists. gauge, byte |
| `kubernetes.containers.restarts` | Exists. gauge, `kubernetes` integration |
| All five monitor definitions | `validate_monitor_definition` returns `is_valid: true` for latency, error ratio, restarts, CPU saturation and memory saturation, including the multi-alert `by {service}` grouping, the `a/b` ratio queries and the `change(max(last_5m),last_5m)` restart query |

## spanmetrics — checked against the connector README, 2026-09-20

| Fact | Consequence |
|---|---|
| Metric names are `traces.span.metrics.calls` and `traces.span.metrics.duration` | `namespace` is set explicitly in `collector.yaml` to that same default, so a version bump cannot move the names the adapter queries |
| Every metric already carries `service.name`, `span.name`, `span.kind`, `status.code`, `collector.instance.id` | Those are **not** declared as `dimensions` (they are defaults). `span.name` and `collector.instance.id` are `exclude_dimensions`: `span.name` is one series per operation per service per status per bucket, which is the cardinality risk the plan flagged, and it buys nothing since the adapter groups by service |
| `status.code` values are `Unset` / `Ok` / `Error` | The error query filters `status.code:error`, not `STATUS_CODE_ERROR` (which was a guess and matches nothing) |
| Duration unit is `ms` today, moving to `s` behind a feature gate | `histogram.unit: s` is pinned. Unpinned, a Collector upgrade would turn the 0.5s latency threshold into 0.5ms and the monitor would fire permanently |
| An OTLP Sum maps to one Datadog COUNT of the same name; only multi-mappings get suffixes | The calls queries use `traces.span.metrics.calls`, not `...calls.count` |
| An OTLP Histogram becomes a Datadog **distribution** in the Agent's `distributions` mode, which is what `p95:` needs | `DD_OTLP_CONFIG_METRICS_HISTOGRAMS_MODE=distributions` is set explicitly on the Agent. In counters mode the duration metric arrives as `.count`/`.sum`/`.bucket` and every latency query returns nothing |
| Datadog counts are deltas | `aggregation_temporality: AGGREGATION_TEMPORALITY_DELTA` on the connector, so the Agent does not diff cumulative series and emit a spike on every Collector restart |

`tests/test_contract.py::test_monitors_and_adapter_read_the_same_metrics` pins the result: a metric a
monitor alerts on must be one the adapter pulls, or the incident is explained from telemetry nobody
alerted on.

## Online Boutique v0.10.2 — checked against the tagged tree, 2026-09-20

| Fact | Consequence |
|---|---|
| The release has **no `kubernetes-manifests.yaml` asset**; the manifest lives at `release/kubernetes-manifests.yaml` in the tagged tree | `kustomization.yaml` points at `raw.githubusercontent.com/.../v0.10.2/release/kubernetes-manifests.yaml`. The release-asset URL 404s, which fails `make up` at the first step |
| 12 Deployments, one container each: `server` everywhere except `redis-cart` (`redis`) and `loadgenerator` (`main`) | The tracing patch targets `server` on `(frontend\|.*service)`, which is right |
| Every container **already has requests and limits**, tuned per runtime: 128Mi for the Go services, 300Mi for `adservice` (JVM), 450Mi for `recommendationservice` (Python), 512Mi for `loadgenerator` | Phase 1.3's uniform-resource patch was **removed**: flattening everything to 256Mi OOM-kills those two on startup. Upstream totals are 1570m / 1368Mi requested, 2825m / 2542Mi limited |
| Most services cap at 128Mi | The `mem` fault default dropped from 220Mi to 100Mi. 220Mi against a 128Mi limit is an instant OOM kill, which is the `kill` fault, not memory pressure |

## OPEN — needs the running cluster

`make status` prints the age of the newest point per family; a family reading "no data" is how each of
these shows up.

| # | Item | Where |
|---|---|---|
| V1 | Datadog site and whether APM/Logs are on the student plan; custom metric allowance vs the series spanmetrics actually creates | `.env`, Phase 3 |
| V2 | Online Boutique tracing env vars in release v0.10.2 (`ENABLE_TRACING`, `COLLECTOR_SERVICE_ADDR`) and which services are instrumented | `infra/online-boutique/kustomization.yaml` |
| V3b | That `kube_deployment` and `kube_namespace` are actually present on the container metrics above | `app/adapter/queries.yaml` |
| V4 | That the Helm keys used really apply 5s collection for the pinned chart version, confirmed in the Metrics Explorer | `infra/datadog/values.yaml` |
| V5b | That Datadog lowercases the OTel tag values, so `span.kind:server` and `status.code:error` match | `app/adapter/queries.yaml` |
| V6 | Chaos Mesh containerd socket path on kind for the pinned chart | `infra/scripts/up.sh` |
| V7 | Datadog webhook template variable names and the unit of the event date | `datadog/monitors/apply.py`, `app/alerts.py` |
| V8 | Monitor thresholds calibrated from an hour of steady baseline (`make calibrate`) | `datadog/monitors/monitors.yaml` |
