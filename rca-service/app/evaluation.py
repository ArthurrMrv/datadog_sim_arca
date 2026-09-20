"""Scoring a live campaign against ground truth (Phase 7).

The metrics are RCAEval's, so a live number can be put next to the offline benchmark number for
the same method. Two things the benchmark cannot measure are added: **time to detect** (Datadog's,
not ours) and the cost of not knowing the injection time — the same incident scored from the
estimated anomaly time and from the ground-truth one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean

MAX_K = 5


@dataclass(frozen=True)
class Fault:
    """One injected fault, as written by `chaos/inject.py`."""

    fault_id: str
    fault_type: str
    target_service: str
    t_start: int
    t_end: int
    params: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Case:
    """A fault matched (or not) to the incident it produced."""

    fault: Fault
    incident_id: str | None
    ranking: list[str]
    t_trigger: int | None
    t_report: int | None

    @property
    def detected(self) -> bool:
        return self.incident_id is not None

    def hit_at(self, k: int) -> bool:
        return self.fault.target_service in self.ranking[:k]

    @property
    def time_to_detect(self) -> int | None:
        return self.t_trigger - self.fault.t_start if self.t_trigger else None

    @property
    def time_to_diagnosis(self) -> int | None:
        return self.t_report - self.fault.t_start if self.t_report else None


def load_ground_truth(path: Path) -> list[Fault]:
    if not path.exists():
        return []
    faults = []
    for line in path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            faults.append(Fault(**{k: record[k] for k in Fault.__annotations__ if k in record}))
    return faults


def match(faults: list[Fault], incidents: list[dict], slack_seconds: int = 120) -> list[Case]:
    """Attach each fault to the first incident whose anomaly time falls inside it.

    An incident that matches no fault is a **false alarm**, and a fault with no incident is a
    **missed detection**; both are reported, which is why fault-free periods belong in a campaign.
    """
    used: set[str] = set()
    cases = []
    for fault in sorted(faults, key=lambda f: f.t_start):
        chosen = None
        for incident in sorted(incidents, key=lambda i: i["timeline"]["t_anomaly"]):
            if incident["incident_id"] in used:
                continue
            anomaly = incident["timeline"]["t_anomaly"]
            if fault.t_start - slack_seconds <= anomaly <= fault.t_end + slack_seconds:
                chosen = incident
                used.add(incident["incident_id"])
                break
        cases.append(_case(fault, chosen))
    return cases


def unmatched(incidents: list[dict], cases: list[Case]) -> list[str]:
    matched = {c.incident_id for c in cases if c.incident_id}
    return [i["incident_id"] for i in incidents if i["incident_id"] not in matched]


def _case(fault: Fault, incident: dict | None) -> Case:
    if incident is None:
        return Case(fault, None, [], None, None)
    primary = incident["rankings"][0]["ranking"]
    return Case(
        fault=fault,
        incident_id=incident["incident_id"],
        ranking=[entry["service"] for entry in primary],
        t_trigger=incident["timeline"].get("t_trigger"),
        t_report=incident["timeline"].get("pull_at"),
    )


def score(cases: list[Case], false_alarms: list[str] | None = None) -> dict:
    """AC@k, Avg@5, detection and diagnosis latency — overall and per fault type."""
    return {
        "overall": _score_group(cases) | {"false_alarms": len(false_alarms or [])},
        "per_fault_type": {
            fault_type: _score_group([c for c in cases if c.fault.fault_type == fault_type])
            for fault_type in sorted({c.fault.fault_type for c in cases})
        },
        "false_alarm_incidents": list(false_alarms or []),
    }


def _score_group(cases: list[Case]) -> dict:
    detected = [c for c in cases if c.detected]
    accuracies = {f"AC@{k}": _ratio([c.hit_at(k) for c in cases]) for k in range(1, MAX_K + 1)}
    return {
        "n_faults": len(cases),
        "n_detected": len(detected),
        "missed_detections": len(cases) - len(detected),
        **accuracies,
        # Avg@5 over AC@1..AC@5, as in RCAEval.
        "Avg@5": round(mean(accuracies.values()), 4) if cases else 0.0,
        "time_to_detect_s": _stats([c.time_to_detect for c in detected]),
        "time_to_diagnosis_s": _stats([c.time_to_diagnosis for c in detected]),
    }


def _ratio(flags: list[bool]) -> float:
    return round(sum(flags) / len(flags), 4) if flags else 0.0


def _stats(values: list[int | None]) -> dict | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return {"mean": round(mean(clean), 1), "min": min(clean), "max": max(clean), "n": len(clean)}
