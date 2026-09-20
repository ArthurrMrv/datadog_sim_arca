"""The adapter -> PRISM data contract, enforced in code (Section 6).

Verified against `RCAEval/e2e/prism.py`; `docs/verified.md` lists each rule's evidence. Checking
it here rather than in PRISM means an infra change (a renamed metric, a missing tag, a stalled
Agent) fails with a sentence naming the problem instead of a confusing ranking.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.adapter.naming import family_of, service_of
from app.config import WindowConfig

# Copied from PRISM (`INTERNAL_PROPERTIES` / `EXTERNAL_PROPERTIES`) and asserted against it in
# tests/test_contract.py, so a change on that side is caught here rather than silently
# unclassifying half
# the metrics.
INTERNAL_PROPERTIES = frozenset({"cpu", "mem", "memory", "disk", "diskio", "socket", "sockets"})
EXTERNAL_PROPERTIES = frozenset(
    {"latency", "lat", "error", "errors", "duration", "rt", "workload"}
)


def property_class(family: str) -> str | None:
    """PRISM's rule: the first token of the property, split on `_`/`-`, decides its class."""
    root = family.replace("-", "_").split("_")[0].lower()
    if root in INTERNAL_PROPERTIES:
        return "internal"
    if root in EXTERNAL_PROPERTIES:
        return "external"
    return None


class ContractError(ValueError):
    """The frame cannot be handed to PRISM."""


@dataclass
class ContractReport:
    """What the frame looks like once it satisfies the contract."""

    n_rows: int
    n_columns: int
    services: list[str]
    pre_points: int
    post_points: int
    step_seconds: int
    unclassified: list[str] = field(default_factory=list)
    services_without_internal: list[str] = field(default_factory=list)
    services_without_external: list[str] = field(default_factory=list)
    nan_fraction: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "n_rows": self.n_rows,
            "n_columns": self.n_columns,
            "services": self.services,
            "pre_points": self.pre_points,
            "post_points": self.post_points,
            "step_seconds": self.step_seconds,
            "unclassified": self.unclassified,
            "services_without_internal": self.services_without_internal,
            "services_without_external": self.services_without_external,
            "nan_fraction": self.nan_fraction,
        }


def validate(frame: pd.DataFrame, anomaly_time: int, cfg: WindowConfig) -> ContractReport:
    """Raise `ContractError` unless `frame` is valid PRISM input; otherwise describe it.

    Warnings (a service missing one property class, unclassified families) are reported rather
    than raised: PRISM handles both, scoring the missing class as 0, and a run that still ranks
    the right service is more useful than a refusal.
    """
    if "time" not in frame.columns:
        raise ContractError("no 'time' column: PRISM splits the windows on it")
    time = frame["time"]
    if not pd.api.types.is_integer_dtype(time):
        raise ContractError(f"'time' must be integer unix seconds, got dtype {time.dtype}")
    if not time.is_monotonic_increasing or time.duplicated().any():
        raise ContractError("'time' must be strictly increasing")

    metrics = [c for c in frame.columns if c != "time"]
    if not metrics:
        raise ContractError("no metric columns: the Datadog queries returned nothing")
    bad = [c for c in metrics if "_" not in c or not service_of(c)]
    if bad:
        raise ContractError(f"columns must be '{{service}}_{{family}}': {bad[:5]}")
    non_numeric = [c for c in metrics if not pd.api.types.is_numeric_dtype(frame[c])]
    if non_numeric:
        raise ContractError(f"non-numeric metric columns: {non_numeric[:5]}")

    steps = time.diff().dropna().unique()
    if len(steps) > 1:
        raise ContractError(f"'time' is not on a regular grid, step values seen: {sorted(steps)}")
    step = int(steps[0]) if len(steps) else cfg.step_seconds

    pre = int((time < anomaly_time).sum())
    post = int((time >= anomaly_time).sum())
    if pre < cfg.min_pre_points or post < cfg.min_post_points:
        raise ContractError(
            f"too few points around anomaly_time={anomaly_time}: {pre} before "
            f"(need {cfg.min_pre_points}), {post} after (need {cfg.min_post_points})"
        )

    all_nan = [c for c in metrics if frame[c].isna().all()]
    if all_nan:
        raise ContractError(f"columns with no observation at all: {all_nan[:5]}")

    by_service: dict[str, set[str | None]] = {}
    unclassified = []
    for name in metrics:
        klass = property_class(family_of(name))
        by_service.setdefault(service_of(name), set()).add(klass)
        if klass is None:
            unclassified.append(name)

    return ContractReport(
        n_rows=len(frame),
        n_columns=len(metrics),
        services=sorted(by_service),
        pre_points=pre,
        post_points=post,
        step_seconds=step,
        unclassified=sorted(unclassified),
        services_without_internal=sorted(s for s, k in by_service.items() if "internal" not in k),
        services_without_external=sorted(s for s, k in by_service.items() if "external" not in k),
        nan_fraction={c: round(float(frame[c].isna().mean()), 4) for c in metrics
                      if frame[c].isna().any()},
    )
