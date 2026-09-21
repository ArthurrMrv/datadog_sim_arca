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

## From an empty machine to a running loop

**0. Prerequisites.** Python >= 3.11 and Docker running with at least **4 CPUs / 6 GB RAM / 20 GB
disk** available to it — 6 CPUs / 8 GB is comfortable. On macOS and Windows that is the Docker Desktop
VM's own allocation (Settings -> Resources), not the host's; the default is often too small.

```bash
# macOS (Docker Desktop installed separately)
brew install kind kubectl helm cloudflared python@3.11
```
```bash
# Linux (Debian/Ubuntu)
sudo apt-get update && sudo apt-get install -y docker.io python3-venv curl
curl -fsSL https://kind.sigs.k8s.io/dl/v0.23.0/kind-linux-amd64 -o /tmp/kind && sudo install /tmp/kind /usr/local/bin/kind
curl -fsSL https://dl.k8s.io/release/v1.30.2/bin/linux/amd64/kubectl -o /tmp/kubectl && sudo install /tmp/kubectl /usr/local/bin/kubectl
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o /tmp/cf && sudo install /tmp/cf /usr/local/bin/cloudflared
```

**1. Get the repo.** Drop `-b ...` once the branch is merged into the default one.
```bash
git clone -b claude/rca-sim-implementation-t214kt https://github.com/ArthurrMrv/datadog_sim_arca.git && cd datadog_sim_arca
```

**2a. API key** — *Organization Settings -> API Keys -> New Key*. This is what the in-cluster Agent
uses to ship metrics; a token cannot replace it.

**2b. Service Access Token** — *Organization Settings -> Service Accounts*, create (or pick) a service
account, then *Access Tokens -> + New Token*. Set expiry to **Never** for a long campaign, and select
**exactly these six scopes** — nothing else is needed, and nothing else should be granted:

| Scope | Why this pipeline needs it |
|---|---|
| `timeseries_query` | the adapter pulls the incident window (`GET /api/v1/query`) |
| `monitors_read` | the poller reads monitor state; `make monitors` checks what already exists |
| `monitors_write` | `make monitors` creates and updates the five monitors |
| `create_webhooks` | `make monitors` registers the webhook that calls rca-service |
| `dashboards_read` | `make monitors` checks whether the dashboard exists before creating it |
| `dashboards_write` | `make monitors` creates and updates the dashboard |

Scope names are case-sensitive. To only *run* the loop against monitors that already exist
(`make rca`, `make analyze`, `make status`), `timeseries_query` and `monitors_read` are sufficient.

**2c. Fill in `.env`** — `DD_SITE` is the host part of your Datadog URL (`datadoghq.eu`,
`datadoghq.com`, `us5.datadoghq.com`, ...).
```bash
cp .env.example .env && ${EDITOR:-nano} .env
```

**3. Install the service.** PRISM is in-tree, so this pulls nothing heavier than pandas.
```bash
make venv
```

**4. Check the checkout before touching the cloud.** 69 tests, no cluster, no account.
```bash
make test
```

**5. Bring up the cluster** (~10 min: kind + Online Boutique + Agent + Collector + Chaos Mesh). Use
`PREPULL=1 make up` where the node cannot reach a registry — common in Codespaces and devcontainers —
or to reuse the host's image cache on a re-created cluster.
```bash
make up
```

**6. Let the baseline settle for 15 minutes.** Thresholds calibrated on a warming cluster are wrong.
```bash
kubectl -n shop get pods -w
```

**7. Gate: is every metric family arriving?** A family reading "no data" means its query does not match
what the cluster emits — fix `rca-service/app/adapter/queries.yaml` before going further.
```bash
make status
```

**8. Calibrate thresholds** on a quiet hour, then write the values into `datadog/monitors/monitors.yaml`
(the ones in git are placeholders).
```bash
make calibrate HOURS=1
```

**9. Expose the webhook** and leave it running; copy the `https://....trycloudflare.com` URL it prints.
```bash
make tunnel
```

**10. Create the monitors, webhook and dashboard.** Put the `RCA_DASHBOARD_ID` it prints into `.env`.
```bash
make monitors URL=https://<tunnel-url>/webhook
```

**11. Start the RCA service** in another terminal (`make rca MODE=poll` instead if you skipped step 9).
```bash
make rca
```

**12. Inject a fault.** A monitor fires in ~2 min; the report lands ~3 min later.
```bash
make inject FAULT=delay SERVICE=cartservice DURATION=300
```

**13. Read the result.**
```bash
cat results/incidents/*/report.md
```

`results/analysis.ipynb` reads whatever is in `results/` and plots what happened: detection latency
per fault type, AC@k against ground truth, the anatomy of a single incident, and how far the estimated
anomaly time fell from the real one. It runs before a campaign too, and says what is missing.

Then `make eval` runs a full campaign (~3.5 h at `repetitions: 1`, ~10.7 h as configured) and
`make sweep` re-ranks every stored incident at 1s / 5s / 15s. `make down` deletes the cluster;
`results/` survives. `make help` lists every target.

## Status

The code and configuration for all eight phases are in place and the offline half is verified: the
data contract, the adapter, the window arithmetic, the webhook path and the scoring are covered by
tests that run PRISM for real on a Datadog-shaped payload, and the vendored PRISM is checked against
upstream's own demo. Every container metric name and all five monitor definitions have been validated
against the Datadog API, and the spanmetrics configuration against the connector's documentation
(`docs/verified.md`). What is left open needs the cluster running: tag presence, 5s collection, the
demo's tracing env vars, and calibrated thresholds. `make status` is what surfaces them.
