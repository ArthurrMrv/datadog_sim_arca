"""Alert -> windows -> Datadog -> PRISM -> report. The webhook and the poller both call this.

Having exactly one path from a trigger to a report is what keeps the fallback honest: if the
tunnel dies and the poller takes over, the analysis is not a different analysis (D16).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from app.adapter.base import MetricsBackend
from app.alerts import Alert
from app.config import Settings
from app.contract import validate
from app.report import build, summarize, to_markdown
from app.runner import rank_all
from app.store import Incident, Store
from app.windows import Windows, from_anomaly_time, from_trigger

log = logging.getLogger(__name__)


@dataclass
class Pipeline:
    settings: Settings
    backend: MetricsBackend
    store: Store
    # Injected in tests, so the wait for the post window can be asserted without waiting for it.
    sleep = staticmethod(time.sleep)
    now = staticmethod(lambda: int(time.time()))

    def run_alert(self, alert: Alert, incident_id: str) -> Incident:
        """Full live path: wait for the post window to exist, then analyse."""
        windows = from_trigger(alert.t_trigger, self.settings.windows)
        incident = self.store.open(incident_id)
        self.store.save_alert(incident, alert.as_dict())
        wait = max(0, windows.pull_at - self.now())
        if wait:
            log.info("incident %s: waiting %ss for the post window", incident_id, wait)
            self.sleep(wait)
        return self.analyze(incident, windows, alert=alert.as_dict())

    def run_offline(self, anomaly_time: int, incident_id: str | None = None) -> Incident:
        """Offline path (`make analyze`): the anomaly time is known, nothing is waited for.

        Running a stored incident both ways is what separates a PRISM error from a detection-time
        error.
        """
        windows = from_anomaly_time(anomaly_time, self.settings.windows)
        return self.analyze(self.store.open(incident_id or f"offline-{anomaly_time}"), windows)

    def analyze(self, incident: Incident, windows: Windows, alert: dict | None = None) -> Incident:
        try:
            fetched = self.backend.fetch(
                windows.fetch_start, windows.fetch_end, windows.step_seconds
            )
            self.store.save_frame(incident, fetched.frame, fetched.meta)
            contract = validate(fetched.frame, windows.t_anomaly, self.settings.windows)
            rankings = rank_all(fetched.frame, windows.t_anomaly, self.settings.prism_variants)
            report = build(
                incident.incident_id, fetched.frame, windows, rankings, contract,
                fetched.meta, self.settings, alert,
            )
            summary = summarize(report, self.settings)
            if summary:
                report["summary"] = summary
            self.store.save_report(incident, report, to_markdown(report))
            log.info(
                "incident %s: top suspects %s",
                incident.incident_id, rankings[0].top(3),
            )
        except Exception as exc:
            # The raw frame is already on disk, so a failure here stays re-analysable offline.
            log.exception("incident %s failed", incident.incident_id)
            self.store.save_error(incident, f"{type(exc).__name__}: {exc}")
            raise
        return incident
