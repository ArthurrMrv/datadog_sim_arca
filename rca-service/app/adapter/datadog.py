"""Datadog implementation of `MetricsBackend` (Phase 5.3).

The adapter is deliberately thin (D13): a Datadog query grouped `by {kube_deployment}` already
returns one series per service, so there is nothing to map onto benchmark metric names. What is
left is renaming to
`{service}_{family}`, putting every series on one grid, and saying out loud what was dropped.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.adapter.base import FetchResult, MetricsBackend, QueryConfig, QuerySpec, load_queries
from app.adapter.naming import column, normalize_service, service_of
from app.config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawSeries:
    """One Datadog series: a family, the service it belongs to, and its points."""

    family: str
    service: str
    points: tuple[tuple[int, float], ...]  # (UTC unix seconds, value)


def grid(start: int, end: int, step: int) -> np.ndarray:
    """The common time grid: step-aligned, so series collected independently line up exactly."""
    first = (start // step) * step
    return np.arange(first, end + 1, step, dtype=np.int64)


def align(points: Iterable[tuple[int, float]], times: np.ndarray, step: int,
          ffill_limit: int) -> pd.Series:
    """Put one series on `times`: mean per bucket, short gaps carried forward, long ones NaN."""
    pairs = [(int(ts // step) * step, float(value)) for ts, value in points if value is not None]
    if not pairs:
        return pd.Series(np.nan, index=times, dtype=float)
    raw = pd.Series([v for _, v in pairs], index=[t for t, _ in pairs], dtype=float)
    bucketed = raw.groupby(level=0).mean()
    return bucketed.reindex(times).ffill(limit=ffill_limit)


def to_frame(
    series: Iterable[RawSeries], start: int, end: int, step: int, cfg: QueryConfig
) -> tuple[pd.DataFrame, dict]:
    """Pivot raw series into PRISM's `time x {service}_{family}` frame.

    Pure function: every test of the contract runs through here, with no Datadog account involved.
    """
    times = grid(start, end, step)
    columns: dict[str, pd.Series] = {}
    duplicates = []
    for item in series:
        name = column(item.service, item.family)
        if name in columns:
            duplicates.append(name)
            continue
        columns[name] = align(item.points, times, step, cfg.ffill_limit_steps)

    zeroed = _fill_absent_with_zero(columns, times, cfg)
    frame = pd.DataFrame({"time": times} | {k: columns[k] for k in sorted(columns)})
    frame.index = range(len(frame))

    metrics = [c for c in frame.columns if c != "time"]
    too_sparse = [c for c in metrics if frame[c].isna().mean() > cfg.max_nan_fraction]
    kept = [c for c in metrics if c not in too_sparse]
    # A column that never moves carries no evidence; PRISM's `preprocess` drops it too, so
    # dropping it
    # here only makes the report honest about what was actually available.
    constant = [c for c in kept if frame[c].nunique(dropna=True) <= 1]
    frame = frame.drop(columns=too_sparse + constant)

    meta = {
        "step_seconds": step,
        "window": {"start": int(times[0]), "end": int(times[-1])},
        "n_rows": len(frame),
        "families": [f.name for f in cfg.families],
        "queries": {f.name: f.query for f in cfg.families},
        "dropped_too_sparse": sorted(too_sparse),
        "dropped_constant": sorted(constant),
        "duplicate_series": sorted(set(duplicates)),
        "zero_filled": sorted(zeroed),
        "gaps": {c: round(float(frame[c].isna().mean()), 4)
                 for c in frame.columns if c != "time" and frame[c].isna().any()},
    }
    if too_sparse or constant:
        log.info("dropped %d sparse and %d constant columns", len(too_sparse), len(constant))
    return frame, meta


def _fill_absent_with_zero(
    columns: dict[str, pd.Series], times: np.ndarray, cfg: QueryConfig
) -> list[str]:
    """Add all-zero columns for `absent_means_zero` families the backend returned no series for.

    Mutates `columns` and returns the names added. A column that stays zero throughout is then
    dropped as constant, which is correct -- it carried no information. One that is zero in the
    baseline and non-zero after the fault survives, and that is exactly the case PRISM's zero-scale
    handling is built for.
    """
    services = {service_of(name) for name in columns}
    added = []
    for spec in cfg.families:
        if not spec.absent_means_zero:
            continue
        for service in services:
            name = column(service, spec.name)
            if name not in columns:
                columns[name] = pd.Series(0.0, index=times, dtype=float)
                added.append(name)
    return added


class DatadogBackend(MetricsBackend):
    """Reads metrics through the Datadog timeseries query API."""

    def __init__(self, settings: Settings, queries: QueryConfig | None = None) -> None:
        if not (settings.dd_api_key and settings.dd_access_token):
            raise RuntimeError("DD_API_KEY and DD_ACCESS_TOKEN are required to query Datadog")
        self._settings = settings
        self._queries = queries or load_queries()

    @property
    def queries(self) -> QueryConfig:
        return self._queries

    def fetch(self, start: int, end: int, step: int) -> FetchResult:
        series: list[RawSeries] = []
        errors: dict[str, str] = {}
        for spec in self._queries.families:
            try:
                series.extend(self._query(spec, start, end, step))
            except Exception as exc:  # one broken family must not lose the whole incident
                log.warning("family %s failed: %s", spec.name, exc)
                errors[spec.name] = str(exc)
        frame, meta = to_frame(series, start, end, step, self._queries)
        meta["backend"] = "datadog"
        meta["site"] = self._settings.dd_site
        meta["query_errors"] = errors
        return FetchResult(frame=frame, meta=meta)

    def freshness(self) -> dict[str, int | None]:
        """Newest timestamp per family over the last 10 minutes, for `make status`.

        The cheapest way to tell "the pipeline is broken" from "the Agent stopped reporting".
        """
        import time as _time

        now = int(_time.time())
        out: dict[str, int | None] = {}
        for spec in self._queries.families:
            try:
                series = self._query(spec, now - 600, now, self._queries.step_seconds)
            except Exception as exc:
                log.warning("family %s failed: %s", spec.name, exc)
                out[spec.name] = None
                continue
            stamps = [ts for item in series for ts, _ in item.points]
            out[spec.name] = max(stamps) if stamps else None
        return out

    # -- Datadog specifics ---------------------------------------------------------------------

    def _query(self, spec: QuerySpec, start: int, end: int, step: int) -> list[RawSeries]:
        response = self._api().query_metrics(
            _from=start, to=end, query=with_rollup(spec, step)
        )
        if getattr(response, "status", "ok") == "error":
            raise RuntimeError(getattr(response, "error", "unknown Datadog query error"))
        return list(parse_series(spec, getattr(response, "series", None) or []))

    def _api(self):
        # Imported lazily so the rest of the package (and its tests) need no Datadog client
        # installed.
        from datadog_api_client import ApiClient, Configuration
        from datadog_api_client.v1.api.metrics_api import MetricsApi

        configuration = Configuration()
        configuration.api_key["apiKeyAuth"] = self._settings.dd_api_key
        configuration.api_key["appKeyAuth"] = self._settings.dd_access_token
        configuration.server_variables["site"] = self._settings.dd_site
        return MetricsApi(ApiClient(configuration))


def with_rollup(spec: QuerySpec, step: int) -> str:
    """Ask for the rollup explicitly, so Datadog cannot silently coarsen the window (D8)."""
    if ".rollup(" in spec.query:
        return spec.query
    return f"{spec.query}.rollup({spec.rollup}, {step})"


def parse_series(spec: QuerySpec, series: Iterable) -> Iterable[RawSeries]:
    """Turn API series into `RawSeries`, taking the service from the family's group-by tag.

    Datadog reports points as `[epoch_milliseconds, value]`; everything downstream is seconds.
    """
    for item in series:
        service = _service_from_tags(_as_list(_get(item, "tag_set")), spec.group_by) \
            or _service_from_scope(_get(item, "scope"), spec.group_by)
        if not service:
            log.warning("family %s: series without a %s tag, skipped", spec.name, spec.group_by)
            continue
        pairs = (_point(point) for point in _as_list(_get(item, "pointlist")))
        yield RawSeries(
            family=spec.name, service=service, points=tuple(p for p in pairs if p is not None)
        )


def _point(point) -> tuple[int, float] | None:
    """One point as (UTC unix seconds, value), or None if it carries no value.

    The client wraps each point in a `Point` model that is neither sized nor indexable -- only
    `.value` reaches the underlying `[epoch_milliseconds, value]` pair. A fixture of plain lists
    hides that entirely, so `tests/test_adapter.py` now feeds real `Point` objects.
    """
    pair = getattr(point, "value", point)
    if pair is None or len(pair) < 2 or pair[1] is None:
        return None
    return int(pair[0]) // 1000, float(pair[1])


def _get(item, attribute: str):
    return item.get(attribute) if isinstance(item, dict) else getattr(item, attribute, None)


def _as_list(value) -> list:
    return list(value) if value else []


def _service_from_tags(tags: list[str], group_by: str) -> str | None:
    prefix = f"{group_by}:"
    for tag in tags:
        if str(tag).startswith(prefix):
            return normalize_service(str(tag)[len(prefix):])
    return None


def _service_from_scope(scope, group_by: str) -> str | None:
    if not scope:
        return None
    return _service_from_tags(str(scope).split(","), group_by)
