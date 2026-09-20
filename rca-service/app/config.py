"""Configuration: env for secrets and endpoints, YAML for everything a run can vary.

No threshold, window length or metric name is hardcoded anywhere else (see CLAUDE.md,
Conventions).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
QUERIES_PATH = Path(__file__).resolve().parent / "adapter" / "queries.yaml"


@dataclass(frozen=True)
class WindowConfig:
    """Window geometry around an alert (D11, D12).

    The monitor fires only after the anomaly has filled its whole evaluation window, and Datadog
    needs a moment to ingest the points it evaluated. Both delays sit between the trigger and the
    moment the anomaly actually started, so the baseline has to end that far before the trigger —
    ending it at the trigger would put fault data in the "normal" reference and weaken every score
    computed from it.
    """

    eval_window_seconds: int = 60
    ingestion_lag_seconds: int = 30
    baseline_seconds: int = 600
    post_seconds: int = 180
    step_seconds: int = 5
    min_pre_points: int = 30
    min_post_points: int = 12


@dataclass(frozen=True)
class Settings:
    dd_site: str = "datadoghq.eu"
    dd_api_key: str = ""
    dd_access_token: str = ""
    webhook_secret: str = ""
    host: str = "0.0.0.0"
    port: int = 8000
    results_dir: Path = ROOT / "results"
    namespace: str = "shop"
    # One fault usually trips several monitors; alerts closer together than this are one incident.
    dedupe_seconds: int = 300
    prism_variants: tuple[str, ...] = ("prism", "prismv2")
    poll_interval_seconds: int = 15
    anthropic_api_key: str = ""
    dashboard_id: str = ""
    windows: WindowConfig = field(default_factory=WindowConfig)

    @property
    def incidents_dir(self) -> Path:
        return self.results_dir / "incidents"

    @property
    def ground_truth_path(self) -> Path:
        return self.results_dir / "ground_truth.jsonl"

    def dashboard_url(self, service: str, start: int, end: int) -> str:
        """Deep link to the data behind a suspect, scoped to that service and the window.

        With `RCA_DASHBOARD_ID` set (printed by `make monitors`) this points at the rca-sim
        dashboard; without it, at the Metrics Explorer, which needs no id and always resolves.
        """
        window = f"from_ts={start * 1000}&to_ts={end * 1000}&live=false"
        if self.dashboard_id:
            return (f"https://app.{self.dd_site}/dashboard/{self.dashboard_id}"
                    f"?tpl_var_service={service}&{window}")
        return (f"https://app.{self.dd_site}/metric/explorer"
                f"?exp_metric=container.cpu.usage&exp_scope=kube_deployment%3A{service}&{window}")


def load_settings(env_file: Path | None = None) -> Settings:
    """Read settings from the environment, loading `.env` first if present."""
    # `.env` wins over anything already exported, which is what `up.sh` does too
    # (`set -a && . ./.env`). With the conventional override=False, a stale `DD_SITE` left in the
    # shell by an earlier `source .env` silently beat the file the README tells you to edit: the
    # Agent got the corrected site and every API read got 401, with nothing pointing at why.
    load_dotenv(env_file or ROOT / ".env", override=True)
    variants = tuple(
        v.strip() for v in os.getenv("RCA_PRISM_VARIANTS", "prism,prismv2").split(",") if v.strip()
    )
    return Settings(
        dd_site=os.getenv("DD_SITE", "datadoghq.eu"),
        dd_api_key=os.getenv("DD_API_KEY", ""),
        # A Service Access Token, scoped to exactly what this pipeline calls (see README). It is
        # sent in the dd-application-key header, so an old-style application key still works.
        dd_access_token=os.getenv("DD_ACCESS_TOKEN") or os.getenv("DD_APP_KEY", ""),
        webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
        host=os.getenv("RCA_HOST", "0.0.0.0"),
        port=int(os.getenv("RCA_PORT", "8000")),
        results_dir=Path(os.getenv("RCA_RESULTS_DIR", str(ROOT / "results"))).resolve(),
        prism_variants=variants,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        dashboard_id=os.getenv("RCA_DASHBOARD_ID", ""),
    )
