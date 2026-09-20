"""The vendored PRISM behaves exactly like its source.

A copy breaks silently: same file, different numbers. Upstream ships `_demo()` — every claim of the
paper under its stated condition, each gap resolution, and all 120 documented configurations — so
running it here is the cheapest possible guarantee that the copy and its preprocessing are faithful.
"""

from __future__ import annotations

import pandas as pd
from app import prism as prism_module
from app.prism.prism import _demo, _propagation_case
from app.prism.time_series import preprocess


def test_upstream_demo_passes():
    _demo()


def test_hyperparameters_are_the_papers():
    """The defaults are Table 5's footnote; a drift here silently changes every published number."""
    assert prism_module.SCORERS == ("zscore", "iqr")
    assert prism_module.POOLINGS == ("max", "mean", "sum")
    assert prism_module.COMBINERS == (
        "additive", "conjunctive", "internal", "external", "marginal"
    )
    assert prism_module.prism.__defaults__[-3:] == ("zscore", "max", "additive")


def test_every_documented_variant_is_exported():
    """So the offline study's ablations run on live incidents unchanged."""
    expected = {"prism", "prism_iqr", "prism_mean", "prism_sum", "prism_conjunctive",
                "prism_marginal", "prism_internal", "prism_external"}

    assert expected <= set(prism_module.__all__)
    assert {name.replace("prism", "prismv2", 1) for name in expected} <= set(prism_module.__all__)


def test_preprocess_drops_time_and_constants_and_scales_memory():
    """The three behaviours the adapter is built around (see CLAUDE.md, Data contract)."""
    frame = pd.DataFrame({
        "time": [1, 2, 3],
        "a_cpu": [1.0, 2.0, 3.0],
        "a_mem": [1e6, 2e6, 3e6],
        "a_flat": [7.0, 7.0, 7.0],
    })

    out = preprocess(frame, dataset="rca-sim")

    assert list(out.columns) == ["a_cpu", "a_mem"]
    assert list(out["a_mem"]) == [1.0, 2.0, 3.0]


def test_dataset_none_would_score_the_time_column():
    """Why app/runner.py always passes a dataset: without one, preprocessing does nothing at all."""
    assert "time" in preprocess(_propagation_case(external_amplification=1.0), dataset=None).columns
