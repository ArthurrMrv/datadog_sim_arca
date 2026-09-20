# Verified facts

Every **VERIFY** item from `IMPLEMENTATION_PLAN.md` ends up here, with how it was checked and when.
Anything still `OPEN` needs a live cluster or a Datadog account and is marked in the code with
`# VERIFY(docs/verified.md)`.

## Verified from source

### PRISM input format (Phase 0, Section 6) — verified 2026-09-20
Read from `automated_root_cause_analysis` @ `RCAEval/e2e/prism.py` and `RCAEval/io/time_series.py`.

| Fact | Evidence |
|---|---|
| Entry point is `prism(data, inject_time=..., dataset=...)`, returning `{"node_names", "ranks", "diagnostics"}` | `prism()` signature and return |
| `prismv2` is the same module under different names (`prismv2 = prism` at the end of `prism.py`); RCAEval registers both | end of `prism.py`, `RCAEval/e2e/__init__.py` |
| `ranks` are strings `"{component}_{witness property}"`, best first. **No scores are returned** | `_rank_components`, `prism()` return |
| Time column is literally `"time"` and is compared to `inject_time` with `<`, so both are plain numbers | `is_normal = (data["time"] < inject_time)` |
| Unit is UTC unix seconds | `main.py:325` reads `inject_time.txt` as `int` and compares it with the dataset's `time` column |
| Component = text before the **first** underscore; property = the rest | `_split_property` uses `str.partition("_")` |
| Property class from the **first** token of the property (split on `_`/`-`, lowercased) | `_property_class` |
| internal = cpu, mem, memory, disk, diskio, socket, sockets | `INTERNAL_PROPERTIES` |
| external = latency, lat, latency-90, error, errors, duration, rt, workload | `EXTERNAL_PROPERTIES` |
| Unclassified properties are excluded from scoring and only appended after the ranked components | `prism()`, `extra` |
| `dataset=None` makes `preprocess` a **no-op** — `time` is not even dropped | `preprocess()` first branch |
| With `dataset` set, `preprocess` drops constant columns, drops `time`, and divides every `*_mem` column by 1e6 | `preprocess`, `convert_mem_mb` |
| Both windows must be non-empty over commonly observed columns, else `ValueError` | `prism()` guard |
| A single NaN does not poison a pooled score | `_POOL_FUNCS` are the `nan*` variants |
| Defaults are `scorer="zscore"`, `pooling="max"`, `combine="additive"` | `prism()` signature |

Consequences, applied in `app/adapter/queries.yaml`:
- families are named `cpu`, `mem`, `latency_p95`, `error_rate`, `workload` so their first token classifies;
- `restarts` is kept for the report but PRISM will list it as unclassified (there is no matching class);
- memory is reported in bytes, since `preprocess` does the MB conversion;
- service names are normalized with `-`, never `_` (`app/adapter/naming.py`).

### PRISM packaging (Phase 0) — verified 2026-09-20
`automated_root_cause_analysis/setup.py` declares `RCAEval` with `install_requires=[]` and the real
dependency list under the `default` extra. `pip install -e ../automated_root_cause_analysis` therefore
installs the package but **no dependencies**; `rca-service` declares `pandas`/`numpy` itself.
`RCAEval/e2e/__init__.py` imports every RCA method the fork ships. It guards those imports only on
Python 3.10, 3.12 and 3.14 (`is_py310() or is_py312() or is_py314()`); on **3.11 it takes the unguarded
branch**, so `import RCAEval.e2e.prism` pulls in matplotlib, torch and causal-learn. `prism.py` itself
needs only numpy, pandas and `RCAEval.io.time_series`. `app/runner.py` therefore tries, in order: a
top-level `prism` package (the clean entry point Phase 0 asks for, once that repo has it),
`RCAEval.e2e.prism`, then the module file loaded directly. Verified working on Python 3.11 with only
`rca-service`'s own dependencies installed.

## OPEN — needs the live system

| # | Item | Where it is used |
|---|---|---|
| V1 | Datadog site and whether APM/Logs are on the student plan; custom metric allowance | `.env`, Phase 3 cardinality budget |
| V2 | Online Boutique tracing env vars in the pinned release (`ENABLE_TRACING`, `COLLECTOR_SERVICE_ADDR`) and which services are actually instrumented | `infra/online-boutique/` |
| V3 | Container metric names and that `kube_deployment` is present | `app/adapter/queries.yaml` |
| V4 | Helm keys that really apply 5s collection for the chart version, confirmed in the Metrics Explorer | `infra/datadog/values.yaml` |
| V5 | spanmetrics metric names and namespace prefix for the pinned Collector version | `app/adapter/queries.yaml` |
| V6 | Chaos Mesh containerd socket path on kind for the chart version | `infra/scripts/up.sh` |
| V7 | Datadog webhook template variables and the unit of `$LAST_UPDATED`/event date | `datadog/monitors/webhook.py`, `app/alerts.py` |
| V8 | Calibrated monitor thresholds from one hour of steady baseline | `datadog/monitors/monitors.yaml` |
