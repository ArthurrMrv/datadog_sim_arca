"""Fixtures: a recorded Datadog response and the frame it turns into.

Everything here runs without a cluster and without a Datadog account, which is the point: the
contract between infra and PRISM is exactly where the pipeline breaks, so it has to be checkable
offline.
"""

from __future__ import annotations

import math

import pytest
from app.adapter.base import QueryConfig, QuerySpec
from app.adapter.datadog import to_frame
from app.config import WindowConfig

ANOMALY_TIME = 1_726_830_000
STEP = 5
BASELINE = 600
POST = 180

SERVICES = ("cartservice", "frontend", "adservice")
FAMILIES = ("cpu", "mem", "latency_avg", "error_rate", "workload")


@pytest.fixture
def query_config() -> QueryConfig:
    return QueryConfig(
        step_seconds=STEP,
        namespace="shop",
        max_nan_fraction=0.5,
        ffill_limit_steps=2,
        families=tuple(
            QuerySpec(name=f, query=f"avg:{f}{{*}} by {{kube_deployment}}",
                      group_by="kube_deployment")
            for f in FAMILIES
        ),
    )


def _value(service: str, family: str, t: int) -> float:
    """A steady baseline, plus a fault in `cartservice` from ANOMALY_TIME on.

    cartservice is the root cause: an internal anomaly (cpu) *and* an external one (latency).
    frontend is downstream, so only its latency moves, and it moves *more* than the root cause's —
    the case a ranking on symptom size alone gets wrong, and the reason the internal/external
    split exists.

    The amplification stays inside the bound PRISM's additive score needs (its Proposition 3.10,
    and the `prism.py` demo that asserts the same thing at 1.7x and 6x): far beyond it, the
    paper's own caveat says the additive score follows the symptom. The point here is the
    plumbing, not the method's limits.

    error_rate stays flat at zero, as it does on a healthy system, so the adapter's
    constant-column drop is exercised on realistic data rather than on a special case.
    """
    baselines = {"cpu": 10.0, "mem": 2.0e8, "latency_avg": 0.05, "error_rate": 0.0,
                 "workload": 20.0}
    base = baselines[family]
    wobble = 1 + 0.01 * math.sin(t / 7.0)
    value = base * wobble
    if t < ANOMALY_TIME:
        return value
    if service == "cartservice":
        return value * {"cpu": 5, "latency_avg": 3}.get(family, 1)
    if service == "frontend" and family == "latency_avg":
        return value * 4
    return value


@pytest.fixture
def raw_response() -> list[dict]:
    """A Datadog v1 timeseries payload, in the shape the client returns (milliseconds, tag_set)."""
    start, end = ANOMALY_TIME - BASELINE, ANOMALY_TIME + POST
    return [
        {
            "metric": family,
            "scope": f"kube_deployment:{service}",
            "tag_set": [f"kube_deployment:{service}"],
            "pointlist": [
                [t * 1000, _value(service, family, t)] for t in range(start, end + 1, STEP)
            ],
        }
        for service in SERVICES
        for family in FAMILIES
    ]


@pytest.fixture
def frame(raw_response, query_config):
    from app.adapter.datadog import parse_series

    series = []
    for spec in query_config.families:
        series.extend(parse_series(spec, [s for s in raw_response if s["metric"] == spec.name]))
    built, _ = to_frame(series, ANOMALY_TIME - BASELINE, ANOMALY_TIME + POST, STEP, query_config)
    return built


@pytest.fixture
def window_config() -> WindowConfig:
    return WindowConfig()
