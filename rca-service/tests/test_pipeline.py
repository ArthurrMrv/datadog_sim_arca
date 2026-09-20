"""Alert to report, with a fake backend standing in for Datadog.

This is the Phase 6 acceptance path minus the cluster: everything between the trigger and the
written report is exercised, including the window arithmetic and the wait.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import pandas as pd
from app.adapter.base import FetchResult, MetricsBackend
from app.alerts import Alert
from app.config import Settings
from app.pipeline import Pipeline
from app.report import deltas_for, to_markdown
from app.store import Store
from app.windows import from_trigger
from conftest import ANOMALY_TIME


@dataclass
class FakeBackend(MetricsBackend):
    frame: pd.DataFrame
    calls: list = None

    def fetch(self, start: int, end: int, step: int) -> FetchResult:
        self.calls = (self.calls or []) + [(start, end, step)]
        window = self.frame[self.frame["time"].between(start, end)].reset_index(drop=True)
        return FetchResult(frame=window, meta={"backend": "fake"})


def _pipeline(tmp_path, frame) -> tuple[Pipeline, FakeBackend, list[int]]:
    settings = Settings(results_dir=tmp_path, prism_variants=("prism", "prism_conjunctive"))
    backend = FakeBackend(frame)
    pipeline = Pipeline(settings=settings, backend=backend, store=Store(settings.incidents_dir))
    slept: list[int] = []
    pipeline.sleep = slept.append
    return pipeline, backend, slept


def test_alert_produces_a_stored_report(tmp_path, frame):
    trigger = ANOMALY_TIME + 90  # the monitor fires after its window has filled
    pipeline, backend, slept = _pipeline(tmp_path, frame)
    pipeline.now = lambda: trigger  # the alert has just arrived
    alert = Alert("1", "rca-sim latency p95 by service", "Triggered", trigger, "service:frontend")

    incident = pipeline.run_alert(alert, "incident-1")
    report = incident.load_report()

    # The post window has to be waited for, not queried immediately (D12).
    assert slept == [from_trigger(trigger, pipeline.settings.windows).wait_seconds]
    assert backend.calls[0][0] == from_trigger(trigger, pipeline.settings.windows).baseline_start
    assert report["top_suspects"][0]["service"] == "cartservice"
    assert report["alert"]["monitor_name"] == "rca-sim latency p95 by service"
    # The raw frame is kept, so the incident can be re-ranked later with a newer PRISM (Phase 7.4).
    assert incident.frame_path.exists()
    assert incident.markdown_path.exists()


def test_every_configured_variant_is_run_on_the_same_window(tmp_path, frame):
    pipeline, _, _ = _pipeline(tmp_path, frame)

    report = pipeline.run_offline(ANOMALY_TIME, "offline-1").load_report()

    assert [r["variant"] for r in report["rankings"]] == ["prism", "prism_conjunctive"]


def test_a_failed_analysis_keeps_the_raw_data(tmp_path, frame):
    """A broken window must stay re-analysable offline rather than vanish with the error."""
    truncated = frame[frame["time"] >= ANOMALY_TIME].reset_index(drop=True)
    pipeline, _, _ = _pipeline(tmp_path, truncated)

    with contextlib.suppress(Exception):
        pipeline.run_offline(ANOMALY_TIME, "offline-broken")

    directory = tmp_path / "incidents" / "offline-broken"
    assert (directory / "error.txt").exists()
    assert "too few points" in (directory / "error.txt").read_text()
    assert (directory / "metrics.parquet").exists()


def test_report_quantifies_what_prism_only_ordered(tmp_path, frame):
    """PRISM returns no scores, so the report's numbers must come from the frame itself."""
    windows = from_trigger(ANOMALY_TIME + 90, Settings().windows)

    deltas = deltas_for(frame, windows, "cartservice")

    assert deltas[0].metric.startswith("cartservice_")
    assert deltas[0].deviation_sigma > 3
    assert deltas[0].post_peak > deltas[0].baseline_mean


def test_markdown_names_the_suspects_and_the_timeline(tmp_path, frame):
    pipeline, _, _ = _pipeline(tmp_path, frame)

    markdown = to_markdown(pipeline.run_offline(ANOMALY_TIME, "offline-md").load_report())

    assert "# Incident offline-md" in markdown
    assert "cartservice" in markdown
    assert str(ANOMALY_TIME) in markdown
