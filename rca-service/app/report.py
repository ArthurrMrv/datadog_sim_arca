"""Incident reports: JSON for scoring, Markdown for reading (Phase 6.5).

PRISM returns an order and a witness metric but no magnitudes, so the numbers here come from the
same frame the ranking was computed from: baseline mean and standard deviation against the
post-window extreme, per metric. That is also all the optional LLM summary is given — it explains
PRISM's ranking rather than inventing a diagnosis of its own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from app.adapter.naming import service_of
from app.config import Settings
from app.contract import ContractReport, property_class
from app.runner import Ranking
from app.windows import Windows

TOP_K = 5


@dataclass(frozen=True)
class MetricDelta:
    metric: str
    baseline_mean: float
    post_peak: float
    deviation_sigma: float

    def as_dict(self) -> dict:
        return {
            "metric": self.metric,
            "baseline_mean": self.baseline_mean,
            "post_peak": self.post_peak,
            "deviation_sigma": self.deviation_sigma,
        }


def deltas_for(frame: pd.DataFrame, windows: Windows, service: str) -> list[MetricDelta]:
    """Per-metric deviation for one service, strongest first, in PRISM's own units."""
    pre = frame[frame["time"] < windows.t_anomaly]
    post = frame[frame["time"] >= windows.t_anomaly]
    out = []
    for name in frame.columns:
        if name == "time" or service_of(name) != service:
            continue
        mean = float(pre[name].mean())
        std = float(pre[name].std())
        peak = float(post[name].abs().max()) if post[name].notna().any() else float("nan")
        sigma = abs(peak - mean) / std if std and std > 0 else float("nan")
        out.append(MetricDelta(name, round(mean, 4), round(peak, 4), round(sigma, 2)))
    return sorted(out, key=_by_deviation)


def _by_deviation(delta: MetricDelta) -> float:
    """Strongest deviation first, metrics with no usable baseline last."""
    sigma = delta.deviation_sigma
    return 1.0 if math.isnan(sigma) else -sigma


def build(
    incident_id: str,
    frame: pd.DataFrame,
    windows: Windows,
    rankings: list[Ranking],
    contract: ContractReport,
    meta: dict,
    settings: Settings,
    alert: dict | None = None,
) -> dict:
    """The machine-readable report. `rankings[0]` is the primary variant."""
    primary = rankings[0]
    return {
        "incident_id": incident_id,
        "timeline": windows.as_dict(),
        "alert": alert or {},
        "primary_variant": primary.variant,
        "rankings": [r.as_dict() for r in rankings],
        "top_suspects": [
            {
                "position": item.position,
                "service": item.service,
                "witness_metric": item.witness_metric,
                "witness_class": property_class(item.witness_metric),
                "metrics": [d.as_dict() for d in deltas_for(frame, windows, item.service)],
                "dashboard": settings.dashboard_url(
                    item.service, windows.baseline_start, windows.post_end
                ),
            }
            for item in primary.items[:TOP_K]
        ],
        "data": contract.as_dict() | {"source": meta},
    }


def to_markdown(report: dict) -> str:
    """The same report, for a human reading it minutes after the alert."""
    timeline = report["timeline"]
    lines = [
        f"# Incident {report['incident_id']}",
        "",
        "| | UTC unix seconds |",
        "|---|---|",
        f"| anomaly estimated at | `{timeline['t_anomaly']}` |",
        f"| monitor triggered at | `{timeline['t_trigger']}` |",
        f"| baseline window | `{timeline['baseline_start']}` .. `{timeline['baseline_end']}` |",
        f"| post window | `{timeline['post_start']}` .. `{timeline['post_end']}` |",
        "",
    ]
    if report.get("alert"):
        alert = report["alert"]
        lines += [
            (f"Alert: **{alert.get('monitor_name', '?')}** "
             f"(scope `{alert.get('scope', '-')}`, monitor `{alert.get('monitor_id', '-')}`)."),
            "",
            "The alerting service is a *symptom*: what follows is where the anomaly started.",
            "",
        ]

    lines += [f"## Ranking ({report['primary_variant']})", ""]
    for suspect in report["top_suspects"]:
        witness = suspect["witness_metric"]
        klass = suspect["witness_class"] or "unclassified"
        lines.append(
            f"{suspect['position']}. **{suspect['service']}** "
            f"— witness `{witness}` ({klass}) — [dashboard]({suspect['dashboard']})"
        )
        for delta in suspect["metrics"][:4]:
            lines.append(
                f"   - `{delta['metric']}`: {delta['baseline_mean']} -> {delta['post_peak']} "
                f"({delta['deviation_sigma']} sigma)"
            )
    lines.append("")

    if len(report["rankings"]) > 1:
        lines += ["## Variants", ""]
        for ranking in report["rankings"]:
            top = ", ".join(r["service"] for r in ranking["ranking"][:3]) or "-"
            lines.append(f"- `{ranking['variant']}`: {top}")
        lines.append("")

    data = report["data"]
    lines += [
        "## Data",
        "",
        (f"- {data['n_rows']} rows x {data['n_columns']} metrics at {data['step_seconds']}s, "
         f"{data['pre_points']} points before / {data['post_points']} after the anomaly"),
        f"- services: {', '.join(data['services'])}",
    ]
    for key, label in (
        ("unclassified", "not scored by PRISM (no property class)"),
        ("services_without_internal", "no internal property"),
        ("services_without_external", "no external property"),
    ):
        if data.get(key):
            lines.append(f"- {label}: {', '.join(data[key])}")
    source = data.get("source", {})
    for key, label in (("dropped_constant", "dropped as constant"),
                       ("dropped_too_sparse", "dropped as too sparse"),
                       ("query_errors", "query errors")):
        if source.get(key):
            lines.append(f"- {label}: {source[key]}")
    if report.get("summary"):
        lines += ["", "## Summary", "", report["summary"]]
    return "\n".join(lines) + "\n"


def summarize(report: dict, settings: Settings) -> str | None:
    """Optional one-paragraph explanation of the ranking, from the ranking and deltas only.

    Returns None whenever it cannot be produced — the report is complete without it.
    """
    if not settings.anthropic_api_key:
        return None
    evidence = {
        "top_suspects": [
            {k: s[k] for k in ("position", "service", "witness_metric", "witness_class")}
            | {"metrics": s["metrics"][:4]}
            for s in report["top_suspects"]
        ],
        "alert": report.get("alert", {}),
    }
    try:
        import httpx

        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": settings.anthropic_api_key,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 400,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "You are given the output of a root cause analysis method "
                            "(PRISM) on a microservice incident: a ranking of services, the "
                            "metric that witnessed each one, and baseline-vs-incident "
                            "deviations. Explain in at most 120 words why the top-ranked "
                            "service is the likely origin and what the other suspects are "
                            "showing. Use only the evidence given; do not invent metrics or "
                            "causes.\n\n" + repr(evidence)
                        ),
                    }
                ],
            },
            timeout=30,
        )
        response.raise_for_status()
        return "".join(
            block.get("text", "") for block in response.json().get("content", [])
        ).strip() or None
    except Exception:
        return None
