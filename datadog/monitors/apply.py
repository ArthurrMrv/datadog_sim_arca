#!/usr/bin/env python3
"""Apply the Datadog side of the pipeline: webhook, monitors, dashboard (Phase 6.1-6.2, D17).

    python datadog/monitors/apply.py apply   --url https://<tunnel>/webhook
    python datadog/monitors/apply.py calibrate --hours 1
    python datadog/monitors/apply.py delete

Everything is created by name and updated in place, so re-running it after changing a threshold
does not leave a second copy of a monitor behind quietly evaluating the old one.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rca-service"))

from app.config import load_settings

DEFINITIONS = Path(__file__).with_name("monitors.yaml")
DASHBOARD = ROOT / "datadog" / "dashboard.json"
WEBHOOK_NAME = "rca"  # referenced as @webhook-rca in every monitor message


def _client():
    from datadog_api_client import ApiClient, Configuration

    settings = load_settings()
    if not (settings.dd_api_key and settings.dd_access_token):
        raise SystemExit("DD_API_KEY and DD_ACCESS_TOKEN are required (see .env.example)")
    configuration = Configuration()
    configuration.api_key["apiKeyAuth"] = settings.dd_api_key
    configuration.api_key["appKeyAuth"] = settings.dd_access_token
    configuration.server_variables["site"] = settings.dd_site
    return ApiClient(configuration), settings


def webhook_payload_template() -> str:
    """The JSON Datadog posts to rca-service.

    Verified against the Webhooks integration docs (docs/verified.md): `$DATE` is when the event
    happened, in **milliseconds**, which `app.alerts._seconds` converts. `$LAST_UPDATED_EPOCH` --
    used here originally -- does not exist, so `event_ts` arrived as that literal string and the
    trigger time silently became "whenever the service happened to receive the webhook", biasing
    every time-to-detect measurement.
    """
    return json.dumps(
        {
            "monitor_id": "$ALERT_ID",
            "monitor_name": "$ALERT_TITLE",
            "transition": "$ALERT_TRANSITION",
            "event_ts": "$DATE",
            "scope": "$ALERT_SCOPE",
            "tags": "$TAGS",
            "event_url": "$LINK",
        }
    )


def apply_webhook(client, url: str, secret: str) -> None:
    from datadog_api_client.v1.api.webhooks_integration_api import (
        WebhooksIntegrationApi,
    )
    from datadog_api_client.v1.model.webhooks_integration import WebhooksIntegration
    from datadog_api_client.v1.model.webhooks_integration_update_request import (
        WebhooksIntegrationUpdateRequest,
    )

    api = WebhooksIntegrationApi(client)
    body = dict(
        name=WEBHOOK_NAME,
        url=url,
        payload=webhook_payload_template(),
        custom_headers=json.dumps({"X-RCA-Secret": secret}),
        encode_as="json",
    )
    try:
        api.update_webhooks_integration(WEBHOOK_NAME, WebhooksIntegrationUpdateRequest(**body))
        print(f"webhook '{WEBHOOK_NAME}' updated -> {url}")
    except Exception:
        api.create_webhooks_integration(WebhooksIntegration(**body))
        print(f"webhook '{WEBHOOK_NAME}' created -> {url}")


def apply_monitors(client, spec: dict) -> dict[str, int]:
    from datadog_api_client.v1.api.monitors_api import MonitorsApi
    from datadog_api_client.v1.model.monitor import Monitor
    from datadog_api_client.v1.model.monitor_options import MonitorOptions
    from datadog_api_client.v1.model.monitor_thresholds import MonitorThresholds
    from datadog_api_client.v1.model.monitor_type import MonitorType
    from datadog_api_client.v1.model.monitor_update_request import MonitorUpdateRequest

    api = MonitorsApi(client)
    existing = {m.name: m.id for m in api.list_monitors(monitor_tags="rca-sim")}
    ids: dict[str, int] = {}

    for definition in spec["monitors"]:
        options = MonitorOptions(
            thresholds=MonitorThresholds(**definition["thresholds"]),
            # A window that is not yet complete is the main source of false alerts on 1-minute
            # evaluations, and a recovery threshold below the alert one stops the flapping
            # that would otherwise open a new incident every evaluation (D10).
            require_full_window=True,
            notify_no_data=False,
            renotify_interval=0,
            # Let the last points arrive before evaluating them.
            evaluation_delay=30,
        )
        body = dict(
            name=definition["name"],
            type=MonitorType(definition["type"]),
            query=definition["query"],
            message=f"{definition['message']}\n{spec['notify']}",
            tags=list(spec["tags"]),
            options=options,
        )
        # Datadog validates more than the query parses: thresholds must agree with the
        # comparison in the query, for one. Checking first means a bad definition fails before
        # any monitor is created, rather than halfway through the set.
        api.validate_monitor(Monitor(**body))
        if definition["name"] in existing:
            monitor_id = existing[definition["name"]]
            api.update_monitor(monitor_id, MonitorUpdateRequest(**body))
        else:
            monitor_id = api.create_monitor(Monitor(**body)).id
        ids[definition["id_key"]] = monitor_id
        print(f"monitor {monitor_id}: {definition['name']}")

    composite = spec.get("composite", {})
    if composite.get("enabled"):
        query = " && ".join(str(ids[key]) for key in composite["of"])
        body = dict(
            name=composite["name"],
            type=MonitorType("composite"),
            query=query,
            message=f"{composite['message']}\n{spec['notify']}",
            tags=list(spec["tags"]),
        )
        if composite["name"] in existing:
            api.update_monitor(existing[composite["name"]], MonitorUpdateRequest(**body))
        else:
            api.create_monitor(Monitor(**body))
        print(f"composite monitor: {query}")
    return ids


def apply_dashboard(client) -> None:
    from datadog_api_client.v1.api.dashboards_api import DashboardsApi
    from datadog_api_client.v1.model.dashboard import Dashboard

    if not DASHBOARD.exists():
        return
    api = DashboardsApi(client)
    payload = json.loads(DASHBOARD.read_text())
    title = payload["title"]
    for summary in api.list_dashboards().dashboards or []:
        if summary.title == title:
            api.update_dashboard(summary.id, Dashboard(**payload))
            print(f"dashboard updated: {title}\n  RCA_DASHBOARD_ID={summary.id}")
            return
    created = api.create_dashboard(Dashboard(**payload))
    # Reports link to this dashboard once the id is in .env; without it they link to the Metrics
    # Explorer, which needs no id.
    print(f"dashboard created: {created.url}\n  RCA_DASHBOARD_ID={created.id}")


def calibrate(args) -> int:
    """Derive the noise-sensitive thresholds from a quiet baseline (Phase 6.1).

    Only two monitors need calibrating, and each must be calibrated against *the quantity it
    compares* -- the earlier version took the p99 of raw CPU nanocores and of an error rate in
    events/s, while those monitors compare ratios, so three of its four numbers were meaningless.

    - latency: absolute seconds, so p99 of `latency_avg` directly.
    - error ratio: errors divided by requests, so p99 of `error_rate / workload`.

    The saturation monitors are deliberately left alone: "90% of the container's limit" is a
    definition of saturated, not a noise threshold, and `> 0` restarts needs no statistics either.

    Run it on an hour with no injection; `--apply` writes the result into monitors.yaml.
    """
    sys.path.insert(0, str(ROOT / "rca-service"))
    import numpy as np
    from app.adapter.base import load_queries
    from app.adapter.datadog import DatadogBackend

    settings = load_settings()
    queries = load_queries()
    frame = DatadogBackend(settings, queries).fetch(
        int(time.time()) - args.hours * 3600, int(time.time()), queries.step_seconds
    ).frame
    spec = yaml.safe_load(DEFINITIONS.read_text())
    percentile, margin = spec["calibration"]["percentile"], spec["calibration"]["margin"]

    def p99(values) -> float:
        values = np.asarray(values, dtype=float).ravel()
        values = values[np.isfinite(values)]
        return float(np.percentile(values, percentile)) if values.size else float("nan")

    latency = p99(frame[[c for c in frame.columns if c.endswith("_latency_avg")]].to_numpy())
    ratios = [
        frame[f"{svc}_error_rate"].to_numpy() / np.where(
            frame[f"{svc}_workload"].to_numpy() > 0, frame[f"{svc}_workload"].to_numpy(), np.nan
        )
        for svc in {c.rsplit("_", 2)[0] for c in frame.columns if c.endswith("_workload")}
        if f"{svc}_error_rate" in frame.columns
    ]
    # No error column at all means no errors in the window, which is a zero ratio, not missing data.
    error_ratio = p99(np.concatenate(ratios)) if ratios else 0.0

    print(f"baseline of {args.hours}h, {len(frame)} rows; threshold = {margin} x p{percentile}\n")
    suggested = {}
    for key, value, floor in (("latency", latency, 0.05), ("errors", error_ratio, 0.01)):
        if not np.isfinite(value):
            print(f"{key:<8} NO DATA -- cannot calibrate; leave the placeholder and investigate")
            continue
        # A floor matters on a baseline this quiet: p99 of a near-zero error ratio is ~0, and a
        # threshold of 0 would alert on the first stray request.
        critical = round(max(margin * value, floor), 4)
        suggested[key] = critical
        print(f"{key:<8} p{percentile}={value:.4g}  critical={critical}")
    print("\nsaturation and restart monitors need no calibration: 90% of limit and > 0 are "
          "definitions, not noise levels.")

    if not args.apply:
        print("\nre-run with --apply to write these into datadog/monitors/monitors.yaml")
        return 0
    if len(suggested) < 2:
        print("\nrefusing to apply a partial calibration")
        return 1
    _write_thresholds(suggested)
    return 0


def _write_thresholds(suggested: dict[str, float]) -> None:
    """Rewrite a calibrated monitor's threshold, in BOTH places it appears.

    Datadog rejects a monitor whose `options.thresholds.critical` differs from the comparison at
    the end of its query -- "Alert threshold (0.05) does not match that used in the query (0.5)" --
    so the two must move together. Writing only the thresholds line left the file in a state that
    could never be applied, and it surfaced an hour later at monitor-creation time.

    Edited line by line rather than via yaml.dump, which would strip every comment in the file, and
    the comments are what make a threshold change reviewable.
    """
    # The trailing group allows the closing quote of a one-line query: without it the anchor never
    # matched a quoted query and only the thresholds line moved -- which is the original bug.
    comparison = re.compile(r"(>\s*)\d+(?:\.\d+)?(\s*\"?\s*)$")
    lines = DEFINITIONS.read_text().splitlines()
    current = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("- name:"):
            current = None  # a new monitor block begins before its id_key is seen
        elif stripped.startswith("id_key:"):
            current = stripped.split(":", 1)[1].strip()
        if current not in suggested:
            continue
        critical = suggested[current]
        if stripped.startswith("thresholds:"):
            indent = line[: len(line) - len(line.lstrip())]
            lines[i] = (
                f"{indent}thresholds: {{critical: {critical}, "
                f"warning: {round(critical * 0.6, 4)}, "
                f"critical_recovery: {round(critical * 0.5, 4)}}}"
            )
            print(f"wrote {current}: critical={critical}")
        else:
            # The comparison lives on the query's last line, folded or not. `query: >-` ends
            # in `>-`, not a number, so the indicator is never mistaken for a comparison.
            updated = comparison.sub(rf"\g<1>{critical}\g<2>", line)
            if updated != line:
                lines[i] = updated
    DEFINITIONS.write_text("\n".join(lines) + "\n")


def apply_all(args) -> int:
    client, settings = _client()
    spec = yaml.safe_load(DEFINITIONS.read_text())
    url = args.url or os.getenv("RCA_WEBHOOK_URL", "")
    if url:
        apply_webhook(client, url, settings.webhook_secret)
    else:
        print("no --url given: monitors will notify a webhook that does not exist yet.\n"
              "The poller (make rca MODE=poll) works without it (D16).")
    apply_monitors(client, spec)
    apply_dashboard(client)
    return 0


def delete_all(args) -> int:
    from datadog_api_client.v1.api.monitors_api import MonitorsApi

    client, _ = _client()
    api = MonitorsApi(client)
    for monitor in api.list_monitors(monitor_tags="rca-sim"):
        api.delete_monitor(monitor.id)
        print(f"deleted {monitor.id}: {monitor.name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("apply", help="create or update webhook, monitors and dashboard")
    p.add_argument("--url", help="public URL of rca-service /webhook (from cloudflared)")
    p.set_defaults(func=apply_all)

    p = sub.add_parser("calibrate", help="derive thresholds from a quiet baseline")
    p.add_argument("--hours", type=int, default=1)
    p.add_argument("--apply", action="store_true", help="write them into monitors.yaml")
    p.set_defaults(func=calibrate)

    p = sub.add_parser("delete", help="remove every rca-sim monitor")
    p.set_defaults(func=delete_all)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
