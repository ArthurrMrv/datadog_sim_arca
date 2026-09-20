"""The only module that touches PRISM.

PRISM lives in `app/prism/`, copied verbatim from `automated_root_cause_analysis` (D14), so the
same code ranks RCAEval cases and live Datadog windows and this repo needs nothing but numpy and
pandas to run it. It returns an *ordered list of `"{component}_{witness property}"` strings and no
scores* (verified, see docs/verified.md), so the ranking carries positions and witnesses; the
magnitudes in the report come from `report.py`, computed from the same frame.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pandas as pd

from app import prism as prism_module
from app.adapter.naming import family_of, service_of

log = logging.getLogger(__name__)

# `preprocess` is a no-op when this is None, which would leave the `time` column in the frame to
# be scored
# as a property. The value itself only has to be a name PRISM has no special case for.
DATASET = "rca-sim"


@dataclass(frozen=True)
class RankedItem:
    position: int
    service: str
    witness_metric: str


@dataclass(frozen=True)
class Ranking:
    variant: str
    items: tuple[RankedItem, ...]
    diagnostics: dict

    def top(self, k: int) -> list[str]:
        return [item.service for item in self.items[:k]]

    def as_dict(self) -> dict:
        return {
            "variant": self.variant,
            "ranking": [
                {"position": i.position, "service": i.service, "witness_metric": i.witness_metric}
                for i in self.items
            ],
            "diagnostics": self.diagnostics,
        }


def available_variants() -> list[str]:
    """Every PRISM entry point, so the offline study's ablations can be run on live incidents."""
    return sorted(
        name for name in dir(prism_module)
        if name.startswith("prism") and callable(getattr(prism_module, name))
    )


def rank(frame: pd.DataFrame, anomaly_time: int, variant: str = "prism", **params) -> Ranking:
    """Run one PRISM variant on a contract-valid frame.

    `variant` is any callable PRISM exports (`prism`, `prismv2`, `prism_conjunctive`, ...), so the
    ablations of the offline study can be run on live incidents unchanged.
    """
    if not hasattr(prism_module, variant):
        raise ValueError(f"unknown PRISM variant {variant!r}; available: {available_variants()}")
    result = getattr(prism_module, variant)(
        frame, inject_time=anomaly_time, dataset=DATASET, **params
    )
    items = tuple(
        RankedItem(position=position, service=service_of(name), witness_metric=family_of(name))
        for position, name in enumerate(result["ranks"], start=1)
    )
    return Ranking(variant=variant, items=items, diagnostics=result.get("diagnostics", {}))


def rank_all(frame: pd.DataFrame, anomaly_time: int, variants: tuple[str, ...]) -> list[Ranking]:
    """Run several variants on the same frame, skipping (and logging) any that fail.

    Comparing variants only means something when they see identical input, which is why they are
    all run here rather than from separate pulls.
    """
    rankings = []
    for variant in variants:
        try:
            rankings.append(rank(frame, anomaly_time, variant))
        except Exception as exc:
            log.warning("variant %s failed: %s", variant, exc)
    if not rankings:
        raise RuntimeError(f"every PRISM variant failed on this window: {variants}")
    return rankings
