"""Incident persistence: one folder per incident under `results/incidents/`.

The raw frame is stored alongside the report so any incident can be re-analysed offline with a
later PRISM version (Phase 6.4, Phase 7.4) — which is the only way to compare variants on
identical input once the live window is gone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class Incident:
    incident_id: str
    directory: Path

    @property
    def frame_path(self) -> Path:
        return self.directory / "metrics.parquet"

    @property
    def report_path(self) -> Path:
        return self.directory / "report.json"

    @property
    def markdown_path(self) -> Path:
        return self.directory / "report.md"

    def load_frame(self) -> pd.DataFrame:
        return pd.read_parquet(self.frame_path)

    def load_report(self) -> dict:
        return json.loads(self.report_path.read_text())


class Store:
    def __init__(self, incidents_dir: Path) -> None:
        self._root = incidents_dir

    def open(self, incident_id: str) -> Incident:
        directory = self._root / incident_id
        directory.mkdir(parents=True, exist_ok=True)
        return Incident(incident_id, directory)

    def save_alert(self, incident: Incident, alert: dict) -> None:
        _write_json(incident.directory / "alert.json", alert)

    def save_frame(self, incident: Incident, frame: pd.DataFrame, meta: dict) -> None:
        frame.to_parquet(incident.frame_path, index=False)
        _write_json(incident.directory / "source.json", meta)

    def save_report(self, incident: Incident, report: dict, markdown: str) -> None:
        _write_json(incident.report_path, report)
        incident.markdown_path.write_text(markdown)

    def save_error(self, incident: Incident, message: str) -> None:
        (incident.directory / "error.txt").write_text(message + "\n")

    def list(self) -> list[Incident]:
        if not self._root.exists():
            return []
        return [
            Incident(path.name, path)
            for path in sorted(self._root.iterdir(), reverse=True)
            if path.is_dir()
        ]

    def get(self, incident_id: str) -> Incident | None:
        directory = self._root / incident_id
        return Incident(incident_id, directory) if directory.is_dir() else None


def _write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str))
