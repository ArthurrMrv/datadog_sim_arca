# rca-sim: Implementation Plan

A local, continuously running microservice system that is monitored by Datadog, has faults injected on
demand, and is diagnosed automatically by PRISM when a Datadog alert fires.

This document is self-contained. It records every decision made so far and the reason behind it, so that
the whole system can be built from it alone. Wherever a detail depends on a tool version or on your
Datadog account, it is marked **VERIFY** and must be checked during implementation rather than assumed.
Checked items are recorded in `docs/verified.md`.

---

## 1. Context and goal

**Starting point.** A reproduction/extension study of PRISM (Pham, arXiv:2601.21359), a graph-free root
cause analysis method, on the RCAEval benchmark. That work lives in `automated_root_cause_analysis`, a
fork of RCAEval that adds a `prism` implementation and a `prismv2` variant, run through `main.py` /
`compare.py`.

**What RCAEval did, and what we copy from it.** RCAEval built its datasets by running real microservice
demos (Online Boutique, Sock Shop, Train Ticket) on Kubernetes, injecting faults (CPU, memory, disk,
delay, loss, socket, code-level), and recording the telemetry. We reuse that recipe for building a
system, not its recorded data.

**Goal.** Build a *live* running system, not a replay of benchmark metrics:

1. A realistic microservice application runs locally under continuous load.
2. The Datadog Agent collects its metrics.
3. Faults are injected on demand, and every injection is logged as ground truth.
4. A Datadog monitor detects the incident. Datadog, not us, determines the detection time.
5. The alert automatically triggers an RCA service. The service pulls the relevant window from the
   Datadog API, converts it into PRISM's input format, runs PRISM, and produces a readable ranked report.
6. Because every fault is logged, the whole loop can be scored (AC@k, time to detect), which extends the
   study from offline benchmarks to live telemetry.

---

## 2. Decision log

| # | Decision | Why | Alternatives rejected |
|---|---|---|---|
| D1 | **Real microservices (Online Boutique) on a local `kind` cluster**, not a pure synthetic simulator | Real services propagate faults realistically, so the RCA problem is genuine. A simulator would only encode *our* assumptions about propagation, making any PRISM evaluation circular. | Python process emitting fake metrics via DogStatsD (acceptable only as a plumbing test) |
| D2 | **Online Boutique** as the application | It is one of RCAEval's systems (results are comparable), has about 11 services in several languages, ships its own load generator, and has OpenTelemetry tracing hooks. It runs on a laptop with about 4 CPUs and 8 GB of RAM. | Sock Shop (older, less maintained); Train Ticket (40+ services, too heavy locally) |
| D3 | **Single-node `kind` cluster** | One node counts as one Datadog host, well within the student limit of 10. It is simple and reproducible. | minikube (fine too, but kind is lighter and scriptable); multi-node (consumes hosts, no benefit) |
| D4 | **Datadog as the telemetry backend and the detector** | This is the target of the project. Monitors give an independent detection time and trigger PRISM automatically. | Prometheus + Jaeger (what RCAEval used). Kept as a possible *second backend* behind the same adapter interface. |
| D5 | **Datadog student plan (GitHub Student Developer Pack): free Pro account, up to 10 hosts, 2 years** | Covers infrastructure metrics, monitors, and webhooks, which is everything the pipeline needs. | Paid trial. APM and Logs are probably *not* included (see D6). **VERIFY** in *Plan & Usage*. |
| D6 | **OpenTelemetry Collector with the `spanmetrics` connector** turns traces into per-service request, error, and latency metrics sent to Datadog as ordinary metrics | Latency and error signals are the strongest RCA signals (CPU/memory alone miss delay and code faults). Paid APM is not needed to get them. They are normal metrics, so **monitors, webhooks, and API queries all keep working**. | Datadog APM (probably not on the plan; its UI is not needed by the pipeline) |
| D7 | **Send traces to a local Jaeger (optional)** | Individual traces are useful for debugging, and Jaeger is free. | None |
| D8 | **5s metric collection**, narrow query windows, resampling in the adapter | The 15s default gives too few points per window for PRISM's statistics. At 5s the system is close to RCAEval-like resolution without overloading the Agent. Narrow windows make the API return native resolution instead of rollups. Resampling aligns series collected at different intervals. | 15s default (too coarse); 1s everywhere (heavy, diminishing returns). 1s/5s/15s becomes an *experiment* (Phase 8). |
| D9 | **Threshold monitors over a 1-minute window**, multi-alert by service | 1 minute is the shortest metric monitor window. End-to-end detection is about 1-2 minutes in practice (Agent flush + ingestion + roughly once-a-minute evaluation). Threshold monitors can use short windows, while anomaly monitors generally need longer ones. | 5-minute windows (slow); anomaly monitors (longer windows, used only as a later comparison) |
| D10 | **Noise control on short windows**: require full window, recovery thresholds, composite monitor (latency AND errors) for the main trigger | Short windows trigger more easily on noise, and every false alert wastes a PRISM run and pollutes the evaluation. | Longer windows (slower detection) |
| D11 | **Baseline window ends at `t_trigger - eval_window - ingestion_lag`**, not at `t_trigger` | The monitor fires *after* the anomaly has been present for its whole evaluation window. Ending the baseline at the trigger would leak fault data into the "normal" period and weaken PRISM. | Using `t_trigger` directly |
| D12 | **The RCA service waits about 2 minutes after the trigger before pulling data** | PRISM needs post-fault data as well. At 5s, 1 minute is only 12 points, so detection speed is not the bottleneck. | Querying immediately (too few post-fault points) |
| D13 | **No "benchmark metric mapping"; a thin adapter instead** | PRISM needs a `time x (service, metric)` matrix. Datadog queries grouped `by {kube_deployment}` already return one series per service. The adapter only renames, aligns, and pivots, and the output uses real Datadog metric names, so it is directly understandable. | Mapping Datadog metrics onto RCAEval's names (pointless for a live system) |
| D14 | **One new monorepo `rca-sim`; keep `automated_root_cause_analysis` separate as a pip dependency** | In one repo the whole pipeline is visible, and most bugs are at the boundaries (monitor tags vs adapter query vs PRISM's expected columns). The PRISM repo stays focused on research, and the *same* code runs on benchmarks and on live data. | Several repos (coordination overhead); copying PRISM into the new repo (two diverging copies) |
| D15 | **Chaos Mesh** for fault injection | Kubernetes-native, it covers the RCAEval fault families (CPU, memory, network delay/loss, pod kill, IO), and its experiments are declarative YAML that can be versioned. | Manual `kubectl exec stress` (not reproducible); LitmusChaos (heavier) |
| D16 | **Webhook delivery through a tunnel (`cloudflared`), with a polling fallback** | Datadog runs in the cloud and cannot reach `localhost`. A tunnel gives a public HTTPS URL for the webhook. A poller that checks monitor states through the API needs no inbound exposure, so it keeps working if the tunnel is down. | Exposing a port on the router (unsafe) |
| D17 | **Datadog configuration as code** (monitors, webhook, dashboard) | Makes the setup reproducible and reviewable, so thresholds can be versioned alongside experiments. | Clicking in the UI (not reproducible) |
| D18 | **Build in dependency order; parallelize only inside a stage; define the adapter contract early** | Each stage needs the previous one to be testable. A fixed data contract lets `infra/` and `rca-service/` evolve independently. | Building everything at once |

---

## 3. Architecture

```
                 +------------------------- kind cluster (1 node = 1 Datadog host) -----------------------+
 loadgenerator ->| Online Boutique (frontend, cart, checkout, currency, payment, shipping, email,         |
 (Locust)        |                  productcatalog, recommendation, ad, redis-cart)                       |
                 |        | OTLP traces                                                                   |
                 |        v                                                                               |
                 | OTel Collector --spanmetrics--> metrics (OTLP) --+       (traces) --> Jaeger (optional) |
                 |                                                 v                                      |
                 | Datadog Agent (DaemonSet, 5s collection, OTLP receiver on) <-- kubelet/container metrics|
                 |                                                                                        |
                 | Chaos Mesh -- injects faults --> target service; inject script logs ground truth (JSONL)|
                 +--------------------------------------+-------------------------------------------------+
                                                        v
                                              Datadog (student Pro)
                                     metrics + monitors (1-min threshold, multi-alert)
                                                        | alert (webhook)
                                                        v
                             cloudflared tunnel --> rca-service (FastAPI, runs locally)
                             (fallback: poller)         | 1. dedupe, keep "Triggered" only
                                                        | 2. compute windows (D11, D12), wait
                                                        | 3. Datadog API -> adapter -> DataFrame
                                                        | 4. prism / prismv2 (pip dependency)
                                                        | 5. report (JSON + Markdown, dashboard links)
                                                        v
                                              results/ + evaluation against ground truth
```

---

## 4. Repository layout

See `CLAUDE.md` for the short version. Makefile targets are the contract for "one command":

| Target | Does |
|---|---|
| `make up` | create the kind cluster, deploy Online Boutique, Datadog Agent, OTel Collector, Chaos Mesh |
| `make down` | delete the cluster |
| `make status` | pods, Agent status, Collector health, last metrics timestamp |
| `make monitors` | apply the Datadog monitors, webhook, and dashboard |
| `make rca` | run rca-service + tunnel (or poller) |
| `make inject FAULT=cpu SERVICE=cartservice DURATION=300` | one fault with ground-truth logging |
| `make analyze T=<unix> [SERVICE=...]` | offline: pull a window and run PRISM without any alert |
| `make eval CONFIG=experiments/configs/base.yaml` | full live evaluation campaign |

---

## 5. Implementation phases

Each phase ends with **acceptance criteria**. Do not start a phase until the previous one passes: each
phase is how the next one gets tested (D18).

### Phase 0: Prerequisites

1. Install Docker, `kind`, `kubectl`, `helm`, Python >= 3.11, `uv` or `pip`, `cloudflared`, optionally
   `terraform`.
2. Activate the Datadog student offer. Create an **API key** and an **Application key**. Note your
   **Datadog site**; every API URL and the Agent config depend on it. **VERIFY** in the account URL.
3. Check *Plan & Usage* for what is included: hosts, custom metric allowance, whether APM and Logs are
   available. **VERIFY**. This decides how strictly cardinality must be controlled (Phase 3).
4. Make `automated_root_cause_analysis` pip-installable and give PRISM a clean function entry point that
   does not depend on the benchmark loaders. Document the input format it expects. **This is the data
   contract (Section 6). Read it from the code, do not assume it.**

**Acceptance:** `pip install -e ../automated_root_cause_analysis` works in a fresh venv, and PRISM runs
on one RCAEval case with the same result as `main.py`.

### Phase 1: Cluster and application

1. `infra/kind-config.yaml`: single node, host port mapped only if the frontend should be reachable.
2. Deploy Online Boutique from a **pinned release** of `GoogleCloudPlatform/microservices-demo`.
3. A kustomize overlay that sets modest CPU/memory **requests and limits** on every service (without
   limits, CPU and memory stress faults have no visible effect on saturation), enables tracing towards
   the OTel Collector (**VERIFY** the env var names in the pinned release, and which services are
   instrumented), and sets the load generator to a steady, moderate number of users.
3. Label everything with the `shop` namespace and a consistent `env:rca-sim` tag.

**Acceptance:** all pods `Running` for 15 minutes without restarts, the frontend responds, the load
generator reports steady RPS and a near-zero error rate.

### Phase 2: Datadog Agent

1. Install the `datadog/datadog` Helm chart with `infra/datadog/values.yaml`: site, API key from a
   Kubernetes secret, `clusterName: rca-sim`, `tags: [env:rca-sim]`, `kubelet.tlsVerify: false` (kind's
   kubelet uses self-signed certificates), **OTLP receiver on**, **5s collection** (D8, **VERIFY** the
   keys for the chart version), and everything unneeded disabled.
2. Confirm that `kube_deployment`, `kube_namespace` and `pod_name` are present on container metrics.

**Acceptance:** Metrics Explorer shows container CPU and memory split by `kube_deployment` for all
Online Boutique services, at 5s resolution over a 10-minute window.

### Phase 3: OpenTelemetry Collector and spanmetrics

1. Deploy a pinned `opentelemetry-collector-contrib` with `infra/otel/collector.yaml`: `otlp` receiver,
   `spanmetrics` connector with dimensions limited to `service.name`, `span.kind`, `status.code`, a small
   explicit bucket set, a 5s flush interval; metrics exported to the Datadog Agent's OTLP endpoint,
   traces to Jaeger if D7 is enabled.
2. Find the resulting metric names in Datadog (**VERIFY**; the namespace prefix depends on the Collector
   version) and write them into `rca-service/app/adapter/queries.yaml`.
3. Count the custom metric series created and compare with the plan's allowance.

**Acceptance:** for every instrumented service, Datadog shows request rate, error rate and p95 latency
derived from spanmetrics at 5s, and the series count is within the allowance.

### Phase 4: Fault injection and ground truth

1. Install Chaos Mesh with Helm, configured for kind's containerd runtime (**VERIFY** the socket path).
2. `chaos/faults/` holds one template per fault family, mirroring RCAEval:

| Fault | Chaos Mesh kind | Expected signal |
|---|---|---|
| `cpu` | `StressChaos` (CPU workers) | CPU saturation/throttling, latency up |
| `mem` | `StressChaos` (memory) | memory up, possible OOM kill |
| `delay` | `NetworkChaos` (delay) | latency up on the target and its callers |
| `loss` | `NetworkChaos` (loss) | errors/retries, latency up |
| `disk` | `IOChaos` (latency/fault) | IO latency |
| `kill` | `PodChaos` (pod-kill) | restarts, errors during recovery |
| `code` (later) | error flags on a service, or `HTTPChaos` | error rate up with little resource change |

3. `chaos/inject.py FAULT SERVICE DURATION` checks health, applies the template, appends ground truth to
   `results/ground_truth.jsonl` (`fault_id, fault_type, target_service, params, t_start, t_end`, UTC unix
   seconds), deletes the experiment and waits out a cooldown so the next run starts from a clean baseline.
4. Check every fault visually on the dashboard: *does it actually move a metric?* A fault with no signal
   cannot be detected and would count as an unfair miss for PRISM.

**Acceptance:** each fault type produces a visible deviation on its target service, ground truth is
logged, and the system returns to baseline after cooldown.

### Phase 5: Adapter and offline PRISM (the most important phase)

Validate PRISM on Datadog data **before** any automation.

1. `adapter/base.py`: a `MetricsBackend` interface with `fetch(start, end, step) -> DataFrame`.
2. `adapter/queries.yaml`: the metric families, each a Datadog query grouped by service.
3. `adapter/datadog.py`: `datadog-api-client`, one query per family, short windows with an explicit
   rollup, renaming to `{service}_{family}`, resampling onto a common 5s grid (mean, short gaps
   forward-filled up to 2 steps, longer gaps left NaN and reported), constant and mostly-NaN columns
   dropped and logged.
4. `windows.py` (D11, D12), with every constant in config:

```
t_trigger       = alert time from Datadog
t_anomaly_est   = t_trigger - eval_window (60s) - ingestion_lag (~30s)
baseline        = [t_anomaly_est - 10 min, t_anomaly_est]
post            = [t_anomaly_est, t_anomaly_est + 3 min]
pull after      = post end + ingestion_lag
```

   The `anomaly_time` passed to PRISM is `t_anomaly_est`; in offline mode it can be the ground-truth
   `t_start`, which separates "PRISM error" from "detection-time error".
5. `make analyze T=<unix>` pulls the window, runs PRISM and prints the ranking.

**Acceptance:** for each fault type, `make analyze` with the ground-truth time produces a well-formed
DataFrame and PRISM puts the true service in the top 3 for most fault types. If not, investigate *here*.

### Phase 6: Detection, trigger, and the RCA service

1. **Monitors as code** (D17): `latency-p95-by-service`, `error-rate-by-service`, `restarts`,
   `cpu-saturation`, `mem-saturation`, and an optional **composite** (latency AND errors) as the stricter
   main trigger. Multi-alert by service, last 1 minute (D9), full window required, recovery thresholds
   (D10), thresholds calibrated from the baseline and recorded in the repo. Every monitor notifies
   `@webhook-rca`. Note that the alerting service is often a *symptom*, not the root cause.
2. **Webhook** definition with a JSON payload built from template variables (**VERIFY** the variable
   names and the timestamp unit) plus a shared secret header.
3. **Delivery** (D16): `cloudflared` tunnel to `localhost:8000/webhook`; fallback `poller.py` on monitor
   states. Both paths call the same function.
4. **rca-service (FastAPI)**: `POST /webhook` verifies the secret, keeps only *Triggered* transitions,
   deduplicates a burst of alerts into one incident, answers 200 immediately and queues the job; a worker
   computes windows, waits, pulls, runs PRISM (and variants) and writes the report. `GET /incidents`.
   One folder per incident with the raw DataFrame (parquet), the ranking, the report and the payload, so
   any incident can be re-analyzed offline with a new PRISM version.
5. **Report**: JSON and Markdown, with the timeline, per-service metric deltas, dashboard links, and an
   optional LLM summary generated *from the ranking and deltas only*.

**Acceptance:** `make inject FAULT=delay SERVICE=cartservice` -> a monitor fires within about 2 minutes ->
the service receives it -> about 3 minutes later a report appears in `results/incidents/`, with no manual
step.

### Phase 7: Live evaluation

`experiments/live_eval.py` with a YAML config: for each (fault type x target service x repetition) check
health, inject, wait for an incident and its report, cool down. Match incidents to ground truth by time.
Report **AC@1, AC@3, AC@5, Avg@5** (as in RCAEval), **time to detect** and **time to diagnosis**,
**missed detections** and **false alarms** (with fault-free periods deliberately included), each **per
fault type**. Re-run every PRISM variant on the *same* stored incident data. Compare PRISM using the
estimated anomaly time vs the ground-truth time: that gap is what the live setting costs.

### Phase 8: Granularity experiment

How does PRISM's accuracy change with metric resolution (1s -> 5s -> 15s)? Preferred method: collect at
the finest stable resolution once and **downsample in the adapter**, so only resolution varies. Fallback:
re-run campaigns with the Agent at each interval. Also report the effect on time to detect and on the
number of points in the post window.

---

## 6. Data contract: adapter -> PRISM

Verified against `RCAEval/e2e/prism.py` and `RCAEval/io/time_series.py`; see `CLAUDE.md` and
`docs/verified.md`. Do not change it without updating both sides.

---

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| APM/Logs not on the student plan | Spanmetrics (D6); nothing in the pipeline depends on APM |
| Custom metric allowance exceeded by spanmetrics | Minimal dimensions and buckets, count series early |
| 5s collection not honored by some checks | Verify in the Metrics Explorer; the adapter resamples anyway and reports actual resolution |
| API returns rolled-up points | Keep windows short and request the rollup explicitly |
| Webhook cannot reach the laptop | Tunnel, with the poller fallback (D16) |
| Alert noise / false positives | Calibrated thresholds, full window, recovery thresholds, composite monitor (D10); measured in Phase 7 |
| One fault -> many alerts | Incident deduplication window in rca-service |
| Fault gives no visible signal | Visual check per fault in Phase 4; resource limits set in Phase 1 |
| Laptop resource limits | One node, trimmed Agent features, moderate load, cooldowns between faults |
| Clock mismatch between injection log and Datadog | All timestamps in UTC unix seconds; host clock synced |
| Secrets committed | `.env` + Kubernetes secrets, `.env.example` only in git |

---

## 8. Working method

1. `CLAUDE.md` stays short and is the entry point: architecture, data contract, commands, conventions.
2. One phase at a time, each ending with its acceptance criteria *run*, not just written.
3. Parallelize only inside a phase (Agent vs Collector; fault templates vs monitors-as-code; adapter vs
   FastAPI skeleton once the contract is fixed).
4. Every **VERIFY** item is checked against the live system or the pinned version's docs and then recorded
   in `docs/verified.md`, so it is not re-guessed later.
5. Keep PRISM changes in `automated_root_cause_analysis`, never patched inside `rca-sim`.

---

## 9. Build order at a glance

| Phase | Output | Depends on | Parallel with |
|---|---|---|---|
| 0 | Datadog keys, PRISM dependency + contract | - | - |
| 1 | kind + Online Boutique + load | 0 | - |
| 2 | Datadog Agent at 5s | 1 | 3 |
| 3 | OTel Collector + spanmetrics | 1 | 2 |
| 4 | Chaos Mesh faults + ground truth | 2, 3 | 6.1 |
| 5 | Adapter + offline PRISM on Datadog data | 4 | 6.4 |
| 6 | Monitors, webhook, tunnel/poller, rca-service, reports | 5 | - |
| 7 | Live evaluation campaign | 6 | - |
| 8 | Granularity experiment | 7 | - |
