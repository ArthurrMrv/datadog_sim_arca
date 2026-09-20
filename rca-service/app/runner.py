"""The only module that imports PRISM.

PRISM lives in `automated_root_cause_analysis` and is installed as a dependency, never vendored
here (D14), so the same code ranks RCAEval cases and live Datadog windows. It returns an *ordered
list of `"{component}_{witness property}"` strings and no scores* (verified, see
docs/verified.md), so the ranking carries positions and witnesses; the magnitudes in the report
come from `report.py`, computed from the same frame.
"""

from __future__ import annotations

import logging
import pathlib
from dataclasses import dataclass
from types import ModuleType

import pandas as pd

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


def _prism_module() -> ModuleType:
    """Import PRISM, by the cheapest route that works.

    1. A top-level `prism` package — the clean entry point Phase 0 asks the PRISM repo for.
       Nothing else is needed once it exists.
    2. `RCAEval.e2e.prism`, the research path.
    3. `RCAEval/e2e/prism.py` loaded directly. `RCAEval.e2e.__init__` imports every RCA method it
       ships, and outside Python 3.10/3.12/3.14 it does so unguarded (torch, matplotlib,
       causal-learn). PRISM itself needs only numpy, pandas and `RCAEval.io.time_series`, so
       loading the module file keeps the service's dependencies to what it actually uses. The file
       is the same code either way.
    """
    try:
        import prism  # type: ignore

        if hasattr(prism, "prism") or hasattr(prism, "rank"):
            return prism
    except ImportError:
        pass
    try:
        from RCAEval.e2e import prism as rcaeval_prism

        return rcaeval_prism
    except ImportError as exc:
        log.debug("RCAEval.e2e unavailable (%s); loading prism.py directly", exc)
    return _load_module_file()


def _load_module_file() -> ModuleType:
    import importlib.util
    import sys

    spec = importlib.util.find_spec("RCAEval.e2e")
    locations = list(getattr(spec, "submodule_search_locations", None) or []) if spec else []
    if not locations:
        raise ImportError(
            "PRISM not found. Install it with: pip install -e ../automated_root_cause_analysis"
        )
    path = pathlib.Path(locations[0]) / "prism.py"
    module_spec = importlib.util.spec_from_file_location("rca_sim_prism", path)
    module = importlib.util.module_from_spec(module_spec)
    sys.modules["rca_sim_prism"] = module
    module_spec.loader.exec_module(module)
    return module


def available_variants() -> list[str]:
    module = _prism_module()
    return sorted(
        name for name in dir(module)
        if name.startswith("prism") and callable(getattr(module, name))
    )


def rank(frame: pd.DataFrame, anomaly_time: int, variant: str = "prism", **params) -> Ranking:
    """Run one PRISM variant on a contract-valid frame.

    `variant` is any callable PRISM exports (`prism`, `prismv2`, `prism_conjunctive`, ...), so the
    ablations of the offline study can be run on live incidents unchanged.
    """
    module = _prism_module()
    if not hasattr(module, variant):
        raise ValueError(f"unknown PRISM variant {variant!r}; available: {available_variants()}")
    result = getattr(module, variant)(
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
