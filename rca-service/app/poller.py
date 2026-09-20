"""Fallback trigger: watch monitor states through the API instead of waiting to be called (D16).

A tunnel is one more thing that can be down, and a campaign that silently stops detecting anything
is worse than a slower detection. The poller needs no inbound exposure and feeds the same
pipeline, so the only difference is up to `poll_interval_seconds` of extra latency — which is
recorded, because the trigger time comes from Datadog's own `last_triggered_ts`, not from when we
noticed.
"""

from __future__ import annotations

import logging
import time

from app.alerts import Alert, Deduper
from app.config import Settings
from app.pipeline import Pipeline

log = logging.getLogger(__name__)

MONITOR_TAG = "rca-sim"
ALERT_STATES = {"Alert", "Warn"}


class Poller:
    def __init__(self, settings: Settings, pipeline: Pipeline, monitor_tag: str = MONITOR_TAG):
        self._settings = settings
        self._pipeline = pipeline
        self._tag = monitor_tag
        self._deduper = Deduper(settings.dedupe_seconds)
        self._state: dict[str, str] = {}

    def poll_once(self) -> list[Alert]:
        """Alerts for groups that entered an alerting state since the previous poll."""
        alerts = []
        for monitor in self._monitors():
            monitor_id = str(_get(monitor, "id"))
            name = str(_get(monitor, "name") or "")
            for group, status in _group_states(monitor):
                key = f"{monitor_id}:{group}"
                previous, self._state[key] = self._state.get(key), status.state
                if status.state in ALERT_STATES and previous not in ALERT_STATES:
                    alerts.append(
                        Alert(
                            monitor_id=monitor_id,
                            monitor_name=name,
                            transition="Triggered",
                            t_trigger=status.since or int(time.time()),
                            scope=group,
                            raw={"source": "poller", "state": status.state},
                        )
                    )
        return alerts

    def run(self) -> None:
        log.info("polling monitors tagged %s every %ss", self._tag,
                 self._settings.poll_interval_seconds)
        while True:
            try:
                for alert in self.poll_once():
                    incident_id = self._deduper.accept(alert)
                    if incident_id is None:
                        continue
                    log.info("incident %s from %s (%s)", incident_id, alert.monitor_name,
                             alert.scope)
                    self._pipeline.run_alert(alert, incident_id)
            except Exception:
                log.exception("poll failed; retrying")
            time.sleep(self._settings.poll_interval_seconds)

    def _monitors(self) -> list:
        from datadog_api_client import ApiClient, Configuration
        from datadog_api_client.v1.api.monitors_api import MonitorsApi

        configuration = Configuration()
        configuration.api_key["apiKeyAuth"] = self._settings.dd_api_key
        configuration.api_key["appKeyAuth"] = self._settings.dd_access_token
        configuration.server_variables["site"] = self._settings.dd_site
        return list(
            MonitorsApi(ApiClient(configuration)).list_monitors(
                monitor_tags=self._tag, group_states="all"
            )
        )


class _GroupStatus:
    __slots__ = ("since", "state")

    def __init__(self, state: str, since: int | None):
        self.state = state
        self.since = since


def _group_states(monitor) -> list[tuple[str, _GroupStatus]]:
    """Per-group state of a multi-alert monitor; falls back to the monitor's overall state."""
    state = _get(monitor, "state")
    groups = _get(state, "groups") if state is not None else None
    if not groups:
        overall = str(_get(monitor, "overall_state") or "Unknown")
        return [("*", _GroupStatus(overall, None))]
    out = []
    for name, group in dict(groups).items():
        out.append(
            (
                str(name),
                _GroupStatus(
                    str(_get(group, "status") or "Unknown"),
                    _as_int(_get(group, "last_triggered_ts")),
                ),
            )
        )
    return out


def _get(item, attribute: str):
    if isinstance(item, dict):
        return item.get(attribute)
    return getattr(item, attribute, None)


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
