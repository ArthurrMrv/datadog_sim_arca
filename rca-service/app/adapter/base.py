"""The backend interface. Datadog is the first implementation; the pipeline knows nothing else.

Keeping the interface this thin is what would let a Prometheus backend (what RCAEval used) be
dropped in later and compared on the same incidents (D4).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from app.config import QUERIES_PATH


@dataclass(frozen=True)
class QuerySpec:
    """One metric family: a backend query plus the tag that separates services."""

    name: str
    query: str
    group_by: str
    rollup: str = "avg"


@dataclass(frozen=True)
class QueryConfig:
    step_seconds: int
    namespace: str
    max_nan_fraction: float
    ffill_limit_steps: int
    families: tuple[QuerySpec, ...]


@dataclass
class FetchResult:
    """A PRISM-shaped frame plus everything needed to explain or reproduce it."""

    frame: pd.DataFrame
    meta: dict = field(default_factory=dict)


def load_queries(path: Path | None = None) -> QueryConfig:
    raw = yaml.safe_load((path or QUERIES_PATH).read_text())
    return QueryConfig(
        step_seconds=int(raw["step_seconds"]),
        namespace=raw.get("namespace", "shop"),
        max_nan_fraction=float(raw.get("max_nan_fraction", 0.5)),
        ffill_limit_steps=int(raw.get("ffill_limit_steps", 2)),
        families=tuple(QuerySpec(**family) for family in raw["families"]),
    )


class MetricsBackend(ABC):
    """Pulls a window of telemetry and returns it in PRISM's input format."""

    @abstractmethod
    def fetch(self, start: int, end: int, step: int) -> FetchResult:
        """Return one frame covering `[start, end]` on a `step`-second grid (UTC unix seconds)."""
