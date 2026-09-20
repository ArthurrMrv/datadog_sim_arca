"""PRISM, vendored so this repo runs it with no dependency beyond numpy and pandas.

Both files are copies from `ArthurrMrv/automated_root_cause_analysis`: `prism.py` from
`RCAEval/e2e/prism.py` with one import line repointed, `time_series.py` from `RCAEval/io/` untouched.
Nothing about the logic, the hyperparameters or the architecture is changed here, so a ranking
produced on live Datadog data is the ranking the offline study would produce on the same frame.
`app/runner.py` is the only module that uses either.
"""

from app.prism.prism import (
    COMBINERS,
    EXTERNAL_PROPERTIES,
    INTERNAL_PROPERTIES,
    POOLINGS,
    SCORERS,
    prism,
    prism_conjunctive,
    prism_external,
    prism_internal,
    prism_iqr,
    prism_marginal,
    prism_mean,
    prism_sum,
    prismv2,
    prismv2_conjunctive,
    prismv2_external,
    prismv2_internal,
    prismv2_iqr,
    prismv2_marginal,
    prismv2_mean,
    prismv2_sum,
)

__all__ = [
    "COMBINERS",
    "EXTERNAL_PROPERTIES",
    "INTERNAL_PROPERTIES",
    "POOLINGS",
    "SCORERS",
    "prism",
    "prism_conjunctive",
    "prism_external",
    "prism_internal",
    "prism_iqr",
    "prism_marginal",
    "prism_mean",
    "prism_sum",
    "prismv2",
    "prismv2_conjunctive",
    "prismv2_external",
    "prismv2_internal",
    "prismv2_iqr",
    "prismv2_marginal",
    "prismv2_mean",
    "prismv2_sum",
]
