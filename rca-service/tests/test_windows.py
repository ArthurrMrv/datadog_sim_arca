from app.config import WindowConfig
from app.windows import from_anomaly_time, from_trigger


def test_baseline_ends_before_the_trigger(window_config: WindowConfig):
    """D11: the monitor fires only after the anomaly filled its evaluation window.

    A baseline ending at the trigger would contain a full minute of fault data and quietly inflate
    the reference mean, which is the one mistake that weakens every score at once.
    """
    windows = from_trigger(1_000_000, window_config)
    lag = window_config.eval_window_seconds + window_config.ingestion_lag_seconds

    assert windows.t_anomaly == 1_000_000 - lag
    assert windows.baseline_end == windows.t_anomaly
    assert windows.baseline_end < windows.t_trigger
    assert windows.baseline_start == windows.t_anomaly - window_config.baseline_seconds


def test_pull_waits_for_the_post_window(window_config: WindowConfig):
    """D12: the last points of the post window must be ingested before they can be queried."""
    windows = from_trigger(1_000_000, window_config)

    assert windows.pull_at == windows.post_end + window_config.ingestion_lag_seconds
    assert windows.pull_at > windows.t_trigger
    assert 100 <= windows.wait_seconds <= 300


def test_offline_windows_are_centred_on_the_known_time(window_config: WindowConfig):
    """Offline, the injection time is known, so nothing is estimated and nothing is waited for."""
    windows = from_anomaly_time(1_000_000, window_config)

    assert windows.t_anomaly == windows.t_trigger == 1_000_000
    assert windows.fetch_start == 1_000_000 - window_config.baseline_seconds
    assert windows.fetch_end == 1_000_000 + window_config.post_seconds


def test_enough_points_for_prism(window_config: WindowConfig):
    """The window geometry must satisfy the contract's minimums at the configured step."""
    windows = from_trigger(1_000_000, window_config)
    step = window_config.step_seconds

    assert (windows.baseline_end - windows.baseline_start) // step >= window_config.min_pre_points
    assert (windows.post_end - windows.post_start) // step >= window_config.min_post_points
