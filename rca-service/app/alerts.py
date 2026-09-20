"""Alerts and incident deduplication.

One fault usually trips several monitors (latency on the target, latency on its callers, errors,
CPU), so every burst has to collapse into one incident: otherwise a single injected fault produces
four PRISM runs and four rows in the evaluation, and the scores stop meaning anything.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Alert:
    """A monitor transition, normalized out of a webhook payload or a poller observation."""

    monitor_id: str
    monitor_name: str
    transition: str
    t_trigger: int
    scope: str = ""
    tags: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict, compare=False)

    @property
    def is_trigger(self) -> bool:
        """Only a transition *into* alert starts an analysis; recoveries and re-notifies do not."""
        return self.transition.strip().lower() in {"triggered", "alert", "re-triggered", "warn"}

    def as_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if k != "raw"} | {"raw": self.raw}


def from_webhook(payload: dict) -> Alert:
    """Parse the payload defined in `datadog/monitors/webhook.py`.

    VERIFY(docs/verified.md V7): Datadog's webhook template variables and the unit of the event
    date. The payload we define sends `event_ts` in seconds; anything that looks like milliseconds
    is converted, so a wrong template variable degrades to a ~1970 timestamp rather than a
    silently shifted window.
    """
    return Alert(
        monitor_id=str(payload.get("monitor_id", "")),
        monitor_name=str(payload.get("monitor_name", "")),
        transition=str(payload.get("transition", "")),
        t_trigger=_seconds(payload.get("event_ts")),
        scope=str(payload.get("scope", "")),
        tags=tuple(_tags(payload.get("tags"))),
        raw=payload,
    )


def _seconds(value) -> int:
    if value in (None, "", "$LAST_UPDATED_EPOCH"):
        return int(time.time())
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return int(time.time())
    # Datadog sends some event timestamps in milliseconds; 1e11 seconds is year 5138, so anything
    # above
    # it is unambiguously milliseconds.
    return number // 1000 if number > 10**11 else number


def _tags(value) -> list[str]:
    if isinstance(value, str):
        return [t.strip() for t in value.split(",") if t.strip()]
    return [str(t) for t in (value or [])]


def incident_id(alert: Alert, dedupe_seconds: int) -> str:
    """Stable id shared by every alert of the same burst.

    Bucketing the trigger time is what makes the id identical for alerts that arrive seconds apart
    without needing any state; the scope is deliberately *not* part of it, because the whole point
    is that latency on `frontend` and CPU on `cartservice` are one incident.
    """
    bucket = alert.t_trigger // max(dedupe_seconds, 1)
    digest = hashlib.sha1(f"{bucket}".encode()).hexdigest()[:6]
    return f"{alert.t_trigger}-{digest}"


class Deduper:
    """Remembers recent incidents so a burst of alerts starts exactly one analysis."""

    def __init__(self, dedupe_seconds: int) -> None:
        self._window = dedupe_seconds
        self._seen: dict[str, int] = {}

    def accept(self, alert: Alert) -> str | None:
        """Return the incident id if this alert opens a new incident, else None."""
        now = alert.t_trigger
        self._seen = {k: t for k, t in self._seen.items() if now - t <= self._window}
        for t in self._seen.values():
            if abs(now - t) <= self._window:
                return None
        key = incident_id(alert, self._window)
        self._seen[key] = now
        return key
