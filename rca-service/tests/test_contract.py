"""The adapter -> PRISM contract, checked against PRISM itself.

If these fail, the pipeline is producing frames PRISM cannot read — which shows up in production
as a ranking that quietly ignores half the metrics, not as an error.
"""

from __future__ import annotations

import pandas as pd
import pytest
from app import contract
from app.adapter.base import load_queries
from app.contract import ContractError, property_class, validate
from app.runner import _prism_module, rank
from conftest import ANOMALY_TIME


def test_property_classes_match_prism():
    """Our copy of PRISM's property classes must be PRISM's.

    A drift here is invisible: PRISM would silently leave every unrecognised family out of the
    ranking.
    """
    prism = _prism_module()

    assert contract.INTERNAL_PROPERTIES <= prism.INTERNAL_PROPERTIES
    assert contract.EXTERNAL_PROPERTIES <= prism.EXTERNAL_PROPERTIES
    assert all(property_class(p) == "internal" for p in contract.INTERNAL_PROPERTIES)
    assert all(property_class(p) == "external" for p in contract.EXTERNAL_PROPERTIES)


def test_every_configured_family_is_classified_or_declared():
    """Every family in queries.yaml either scores, or is knowingly kept for the report only."""
    report_only = {"restarts"}

    for family in load_queries().families:
        classified = property_class(family.name) is not None
        assert classified or family.name in report_only, (
            f"family {family.name!r} would be silently excluded from every ranking; "
            "rename it so its first token is one of PRISM's property classes"
        )


def test_fixture_frame_satisfies_the_contract(frame, window_config):
    report = validate(frame, ANOMALY_TIME, window_config)

    assert report.services == ["adservice", "cartservice", "frontend"]
    assert report.step_seconds == 5
    assert report.pre_points >= window_config.min_pre_points
    assert report.post_points >= window_config.min_post_points
    assert report.unclassified == []
    assert report.services_without_internal == []
    assert report.services_without_external == []


def test_prism_reads_the_frame_and_finds_the_root_cause(frame):
    """End to end over the contract: a Datadog-shaped payload ranked by the real PRISM.

    `frontend` has the *larger* latency anomaly but no internal one; `cartservice` has both.
    Getting `cartservice` first is the whole reason for the adapter's column naming.
    """
    ranking = rank(frame, ANOMALY_TIME, "prism")

    assert ranking.items[0].service == "cartservice"
    assert ranking.items[0].witness_metric in ("cpu", "latency_p95")
    assert ranking.diagnostics["n_unclassified"] == 0


@pytest.mark.parametrize("variant", ["prism", "prismv2", "prism_conjunctive"])
def test_variants_all_run_on_the_same_frame(frame, variant):
    assert rank(frame, ANOMALY_TIME, variant).items


def test_time_must_be_integer_seconds(frame, window_config):
    broken = frame.assign(time=pd.to_datetime(frame["time"], unit="s"))

    with pytest.raises(ContractError, match="integer unix seconds"):
        validate(broken, ANOMALY_TIME, window_config)


def test_irregular_grid_is_rejected(frame, window_config):
    broken = frame.drop(index=5).reset_index(drop=True)

    with pytest.raises(ContractError, match="regular grid"):
        validate(broken, ANOMALY_TIME, window_config)


def test_too_few_points_is_rejected(frame, window_config):
    """A stalled Agent must fail loudly, not produce a ranking from four points."""
    short = frame[frame["time"] >= ANOMALY_TIME - 20].reset_index(drop=True)

    with pytest.raises(ContractError, match="too few points"):
        validate(short, ANOMALY_TIME, window_config)


def test_service_names_never_contain_underscores(frame):
    """PRISM splits on the first underscore, so an underscore in a name invents a service."""
    for column in frame.columns:
        if column != "time":
            assert "_" not in column.partition("_")[0]
