"""Metrics backends: pull a window of telemetry and return PRISM-shaped frames."""

from app.adapter.base import FetchResult, MetricsBackend, QuerySpec, load_queries

__all__ = ["FetchResult", "MetricsBackend", "QuerySpec", "load_queries"]
