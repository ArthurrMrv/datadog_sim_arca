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
| A `frontend-external` Service **already exists**, as type `LoadBalancer` | Listing one under `resources` is a kustomize duplicate-id error that stops `make up` at its first step, so `frontend-nodeport.yaml` is a **patch**, not a resource. The patch is still needed: kind has no load balancer, so as `LoadBalancer` it sits Pending and `localhost:8080` never answers. `NodePort` 30080 is the port `kind-config.yaml` maps; `targetPort` and the port name are inherited, since the strategic merge keys ports on `port` |
| `kustomize build infra/online-boutique` renders 36 objects | Verified: all in namespace `shop`, all labelled `env:rca-sim`, tracing env on the 10 instrumented services only, `loadgenerator` at USERS=10/RATE=5, upstream memory limits intact |
| Most services cap at 128Mi | The `mem` fault default dropped from 220Mi to 100Mi. 220Mi against a 128Mi limit is an instant OOM kill, which is the `kill` fault, not memory pressure |

## Datadog credentials — read from the API spec, 2026-09-20

Scopes taken from Datadog's own OpenAPI spec (`datadog-api-client-python`,
`.generator/schemas/v1/openapi.yaml`), which declares the `AuthZ` scope per operation. Each endpoint's
`security` list is a set of *alternatives*, so one scope per call is enough.

| Call | Endpoint | Scope |
|---|---|---|
| adapter pulls a window | `GET /api/v1/query` | `timeseries_query` |
| poller / apply.py list | `GET /api/v1/monitor` | `monitors_read` |
| apply.py create/update/delete monitor | `POST`/`PUT`/`DELETE /api/v1/monitor` | `monitors_write` (`monitors_draft_write` is the alternative, and is not enough for live monitors) |
| apply.py webhook | `POST /api/v1/integration/webhooks/configuration/webhooks` | `create_webhooks` |
| apply.py list dashboards | `GET /api/v1/dashboard` | `dashboards_read` |
| apply.py create/update dashboard | `POST`/`PUT /api/v1/dashboard` | `dashboards_write` |

Notes:
- `PUT .../webhooks/{name}` declares **no** `AuthZ` scope and inherits the spec's global
  `apiKeyAuth + appKeyAuth`. `create_webhooks` is the scope Datadog maps webhook management to, so it
  is the one to grant; if a token is refused when *updating* an existing webhook, delete the webhook
  and let `make monitors` recreate it.
- Querying does **not** need `metrics_read` — that scope is for custom-metric *metadata*, not data.
- A Service Access Token authenticates either as `Authorization: Bearer <token>` or in the
  `dd-application-key` header. The repo uses the latter, which is what `datadog-api-client` already
  sends as `appKeyAuth`, so switching from an application key needed no client change.
- The Agent still needs a real **API key**: a token does not replace it for metric intake.

## Datadog Agent OTLP — verified on a running cluster, 2026-09-20

| Fact | Evidence |
|---|---|
| The Agent Service is named after the Helm release, i.e. **`datadog`**, so the in-cluster address is `datadog.datadog.svc.cluster.local` | `kubectl -n datadog get svc` -> `datadog  ClusterIP  8125/UDP,8126/TCP,4317/TCP,4318/TCP`. `datadog-agent...` (the earlier guess) does not exist and failed as `name resolver error: produced zero addresses`, so no trace metric ever left the Collector |
| The DaemonSet also exposes OTLP gRPC on a **hostPort**: `containerPort 4317, hostPort 4317, name otlpgrpcport` | `kubectl -n datadog get ds datadog -o jsonpath=...`. A `status.hostIP` downward-API reference is therefore a valid alternative target |
| **V4 (OTLP half) closed**: the receiver keys in `infra/datadog/values.yaml` do take effect for chart 3.70.4 | `agent status` reports `OTLP / Status: Enabled / Collector status: Running`, and `feature_otlp_enabled: true` |
| An exporter with no resolvable target is not harmless | With `DEPLOY_JAEGER=0` (the default) the `otlp/jaeger` exporter filled its queue (`sending queue is full`), and those errors propagate back to the receiver, making the instrumented services retry their exports. Jaeger is no longer in the default traces pipeline; enabling D7 means adding the exporter *and* `DEPLOY_JAEGER=1` |
| `agent status` section headers are at column 0 between `=====` rules | So `grep '^  Forwarder'` matches nothing; use `grep -A5 'API Keys status'` or `sed -n '/^Forwarder/,/^Endpoints/p'` |

## Credential precedence — diagnosed on a live setup, 2026-09-20

`up.sh` reads `.env` with `set -a && . ./.env`, so the file wins over anything already exported.
`load_settings()` used `load_dotenv(override=False)`, the conventional precedence, so an exported
variable won instead. The two disagreed, and the failure mode was silent: after correcting `DD_SITE` in
`.env`, a shell that had earlier run `source .env` still held the old site, so `up.sh` gave the Agent
the right one while every API read returned `401 {"errors":["Unauthorized"]}`. `curl` with freshly
sourced values returned 200, which made it look like a client bug.

Both now let `.env` win, and `app.cli freshness` prints the resolved site and credential lengths first,
because every credential failure looks identical from the outside.

Reference points established while diagnosing it, on site `us5.datadoghq.com`:

- API key is 32 characters; the Service Access Token was 65. `GET /api/v1/validate` with the API key
  alone returns 200, and `GET /api/v1/query` returns 200 **both** with `DD-APPLICATION-KEY: <token>`
  and with `Authorization: Bearer <token>` — so a SAT in the application-key header does work on v1,
  which is what the client sends.
- Datadog distinguishes the two credentials: `403 {"errors":["Forbidden"]}` is the API key,
  `401 {"errors":["Unauthorized"]}` is the application key / token.

## Two live-path defects the fixtures could not catch, 2026-09-20

**`Point` is not a list.** `MetricsApi.query_metrics` returns each point as a `Point` model that
supports neither `len()` nor indexing; only `.value` reaches the `[epoch_milliseconds, value]` pair.
`parse_series` indexed it directly, so every container family failed with
`object of type 'Point' has no len()` while the recorded-fixture tests passed. `tests/test_adapter.py`
now feeds real `Point` objects.

**`p95:` on a distribution needs percentile aggregation enabled per metric.** Without it the query
fails with `configuration error :: type: missing_aggregation :: aggregations: AGG_AVG/AGG_P95`. The
latency family is therefore `latency_avg` using `avg:`, which works on any distribution; for these
faults the mean moves enormously anyway (a 200ms injected delay against a ~5ms baseline). To use p95,
enable `include_percentiles` on `traces.span.metrics.duration` and rename the family to `latency_p95` —
the first token is what PRISM classifies, so either name is external.

**The Agent's OTLP receiver binds to localhost by default.** Once the Service name was right, the
Collector still got `connection refused` on the ClusterIP: the Service resolved and the port was
published, but nothing outside the Agent's network namespace could connect.
`DD_OTLP_CONFIG_RECEIVER_PROTOCOLS_GRPC_ENDPOINT=0.0.0.0:4317` is now set explicitly on the Agent.

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
