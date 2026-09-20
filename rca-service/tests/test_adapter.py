"""The adapter: Datadog series in, one aligned frame out."""

from __future__ import annotations

import numpy as np
from app.adapter.base import QuerySpec
from app.adapter.datadog import RawSeries, align, grid, parse_series, to_frame, with_rollup
from conftest import BASELINE, POST, STEP

SPEC = QuerySpec(name="cpu", query="avg:container.cpu.usage{*} by {kube_deployment}",
                 group_by="kube_deployment")


def test_rollup_is_requested_explicitly():
    """D8: without an explicit rollup Datadog may return coarser points than the Agent collected."""
    assert with_rollup(SPEC, 5).endswith(".rollup(avg, 5)")


def test_existing_rollup_is_left_alone():
    spec = QuerySpec(name="x", query="avg:m{*}.rollup(max, 10)", group_by="kube_deployment")

    assert with_rollup(spec, 5) == spec.query


def test_points_are_seconds_and_services_are_normalized():
    """Datadog reports milliseconds; everything downstream is UTC unix seconds."""
    series = list(parse_series(SPEC, [
        {"tag_set": ["kube_deployment:redis_cart"], "pointlist": [[1_726_830_000_000, 1.5]]}
    ]))

    assert series[0].service == "redis-cart"
    assert series[0].points == ((1_726_830_000, 1.5),)


def test_real_client_points_are_parsed():
    """The client returns `Point` models, not lists.

    A fixture of plain lists passed `parse_series` happily while the live path raised
    `object of type 'Point' has no len()` on every family. This feeds the real model.
    """
    from datadog_api_client.v1.model.point import Point

    series = {
        "tag_set": ["kube_deployment:cartservice"],
        "pointlist": [Point([1_726_830_000_000.0, 1.5]), Point([1_726_830_005_000.0, 2.5])],
    }

    parsed = list(parse_series(SPEC, [series]))

    assert parsed[0].service == "cartservice"
    assert parsed[0].points == ((1_726_830_000, 1.5), (1_726_830_005, 2.5))


def test_series_without_the_group_tag_is_skipped():
    """A missing tag means the query is wrong; inventing a service name would hide that."""
    assert list(parse_series(SPEC, [{"tag_set": ["env:rca-sim"], "pointlist": []}])) == []


def test_series_are_aligned_onto_a_shared_grid():
    """Families collected independently must land on identical timestamps, or nothing lines up."""
    times = grid(1000, 1030, 10)
    aligned = align([(1003, 1.0), (1007, 3.0), (1021, 5.0)], times, 10, ffill_limit=2)

    assert list(times) == [1000, 1010, 1020, 1030]
    assert aligned.loc[1000] == 2.0   # both points of the bucket, averaged
    assert aligned.loc[1010] == 2.0   # short gap carried forward
    assert aligned.loc[1020] == 5.0


def test_long_gaps_stay_nan():
    """A long gap is missing data, not a flat line: filling it would invent a stable baseline."""
    times = grid(1000, 1060, 10)
    aligned = align([(1000, 1.0), (1060, 2.0)], times, 10, ffill_limit=2)

    assert aligned.isna().sum() == 3


def test_constant_and_sparse_columns_are_dropped_and_reported(query_config):
    times = list(range(1000, 1100, STEP))
    series = [
        RawSeries("cpu", "cartservice", tuple((t, 1.0 + t % 3) for t in times)),
        RawSeries("cpu", "adservice", tuple((t, 7.0) for t in times)),          # constant
        RawSeries("mem", "adservice", ((1000, 1.0), (1005, 2.0))),              # far too sparse
    ]

    frame, meta = to_frame(series, 1000, 1095, STEP, query_config)

    assert "cartservice_cpu" in frame.columns
    assert meta["dropped_constant"] == ["adservice_cpu"]
    assert meta["dropped_too_sparse"] == ["adservice_mem"]


def test_frame_is_prism_shaped(frame):
    assert frame.columns[0] == "time"
    assert frame["time"].dtype == np.int64
    assert len(frame) == (BASELINE + POST) // STEP + 1
    assert "cartservice_latency_avg" in frame.columns
    # Flat at zero on a healthy system, so it carries no information and is dropped.
    assert not [c for c in frame.columns if c.endswith("_error_rate")]


def test_metadata_records_how_the_frame_was_built(frame, query_config):
    series = [RawSeries("cpu", "cartservice", ((1000, 1.0), (1005, 2.0)))]
    _, meta = to_frame(series, 1000, 1005, STEP, query_config)

    assert meta["window"] == {"start": 1000, "end": 1005}
    assert meta["step_seconds"] == STEP
    assert "cpu" in meta["queries"]
