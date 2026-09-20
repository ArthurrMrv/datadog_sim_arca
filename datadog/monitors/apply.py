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
    if not (settings.dd_api_key and settings.dd_app_key):
        raise SystemExit("DD_API_KEY and DD_APP_KEY are required (see .env.example)")
    configuration = Configuration()
    configuration.api_key["apiKeyAuth"] = settings.dd_api_key
    configuration.api_key["appKeyAuth"] = settings.dd_app_key
    configuration.server_variables["site"] = settings.dd_site
    return ApiClient(configuration), settings


def webhook_payload_template() -> str:
    """The JSON Datadog posts to rca-service.

    VERIFY(docs/verified.md V7): these are Datadog webhook template variables; confirm the names
    and the unit of the event date against the Webhooks integration docs before trusting a trigger
    time. The service tolerates seconds or milliseconds, but a *wrong variable* would silently
    shift every window.
    """
    return json.dumps(
        {
            "monitor_id": "$ALERT_ID",
            "monitor_name": "$ALERT_TITLE",
            "transition": "$ALERT_TRANSITION",
            "event_ts": "$LAST_UPDATED_EPOCH",
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
    """Suggest thresholds from a quiet baseline instead of guessing round numbers (Phase 6.1).

    Run it only on an hour with no injection: the point is to place the threshold just above the
    noise the system produces when nothing is wrong.
    """
    sys.path.insert(0, str(ROOT / "rca-service"))
    import numpy as np
    from app.adapter.base import load_queries
    from app.adapter.datadog import DatadogBackend

    settings = load_settings()
    queries = load_queries()
    backend = DatadogBackend(settings, queries)
    now = int(time.time())
    frame = backend.fetch(now - args.hours * 3600, now, queries.step_seconds).frame
    spec = yaml.safe_load(DEFINITIONS.read_text())
    percentile = spec["calibration"]["percentile"]
    margin = spec["calibration"]["margin"]

    print(f"baseline of {args.hours}h, {len(frame)} rows; threshold = {margin} x p{percentile}\n")
    for family in ("latency_p95", "error_rate", "cpu", "mem"):
        columns = [c for c in frame.columns if c.endswith(f"_{family}")]
        if not columns:
            print(f"{family:<12} no data")
            continue
        values = frame[columns].to_numpy(dtype=float).ravel()
        values = values[~np.isnan(values)]
        p = float(np.percentile(values, percentile)) if values.size else float("nan")
        print(f"{family:<12} p{percentile}={p:.4g}  suggested threshold={margin * p:.4g}")
    print("\nWrite the values you keep into datadog/monitors/monitors.yaml (V8).")
    return 0


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

    p = sub.add_parser("calibrate", help="suggest thresholds from a quiet baseline")
    p.add_argument("--hours", type=int, default=1)
    p.set_defaults(func=calibrate)

    p = sub.add_parser("delete", help="remove every rca-sim monitor")
    p.set_defaults(func=delete_all)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
