"""Window computation around an alert (D11, D12).

The only non-obvious part is that the anomaly is estimated to have started *before* the trigger,
by the monitor's evaluation window plus the ingestion lag: a 1-minute threshold monitor cannot
fire until the condition has held for that whole minute, over points that reached Datadog a moment
earlier.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from app.config import WindowConfig


@dataclass(frozen=True)
class Windows:
    """All window bounds for one incident, in UTC unix seconds."""

    t_trigger: int
    t_anomaly: int
    baseline_start: int
    baseline_end: int
    post_start: int
    post_end: int
    pull_at: int
    step_seconds: int

    @property
    def fetch_start(self) -> int:
        return self.baseline_start

    @property
    def fetch_end(self) -> int:
        return self.post_end

    @property
    def wait_seconds(self) -> int:
        """How long to wait before the data for this window exists. Never negative."""
        return max(0, self.pull_at - self.t_trigger)

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


def from_trigger(t_trigger: int, cfg: WindowConfig) -> Windows:
    """Windows for a live alert: the anomaly time is *estimated* from the trigger (D11)."""
    t_anomaly = t_trigger - cfg.eval_window_seconds - cfg.ingestion_lag_seconds
    return _build(t_trigger, t_anomaly, cfg)


def from_anomaly_time(t_anomaly: int, cfg: WindowConfig) -> Windows:
    """Windows for offline analysis, where the anomaly time is known (a ground-truth `t_start`).

    Running the same incident both ways is what separates a PRISM error from a detection-time
    error (Phase 7.5).
    """
    return _build(t_anomaly, t_anomaly, cfg)


def _build(t_trigger: int, t_anomaly: int, cfg: WindowConfig) -> Windows:
    post_end = t_anomaly + cfg.post_seconds
    return Windows(
        t_trigger=t_trigger,
        t_anomaly=t_anomaly,
        baseline_start=t_anomaly - cfg.baseline_seconds,
        baseline_end=t_anomaly,
        post_start=t_anomaly,
        post_end=post_end,
        # The last points of the post window have to be ingested before they can be queried (D12).
        pull_at=post_end + cfg.ingestion_lag_seconds,
        step_seconds=cfg.step_seconds,
    )
