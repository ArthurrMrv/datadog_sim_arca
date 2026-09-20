#!/usr/bin/env python3
"""Does metric resolution change PRISM's accuracy? (Phase 8)

    python experiments/granularity_sweep.py --steps 5 15 30

Every stored incident is re-ranked after downsampling its frame, so all resolutions come from the
*same* incidents and only the resolution varies. Re-running campaigns at different Agent intervals
would be more realistic but would also change the incidents themselves, making the comparison
unreadable — that is the fallback, not the method.

The number the benchmark cannot give is here: how many post-fault points a resolution leaves in the
window a live monitor defines.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rca-service"))

from app.config import load_settings
from app.evaluation import MAX_K, Fault, load_ground_truth
from app.runner import rank
from app.store import Store


def downsample(frame: pd.DataFrame, step: int) -> pd.DataFrame:
    """Aggregate onto a coarser grid, exactly as a slower Agent would have delivered it."""
    bucketed = frame.assign(time=(frame["time"] // step) * step)
    out = bucketed.groupby("time", as_index=False).mean(numeric_only=True)
    return out.astype({"time": "int64"})


def fault_for(incident: dict, faults: list[Fault], slack: int = 120) -> Fault | None:
    anomaly = incident["timeline"]["t_anomaly"]
    for fault in faults:
        if fault.t_start - slack <= anomaly <= fault.t_end + slack:
            return fault
    return None


def sweep(steps: list[int], variant: str, settings) -> dict:
    store = Store(settings.incidents_dir)
    faults = load_ground_truth(settings.ground_truth_path)
    rows = []
    for incident in store.list():
        if not (incident.report_path.exists() and incident.frame_path.exists()):
            continue
        report = incident.load_report()
        truth = fault_for(report, faults)
        if truth is None:
            continue  # only faults with ground truth can be scored
        frame = incident.load_frame()
        anomaly = report["timeline"]["t_anomaly"]
        for step in steps:
            coarse = downsample(frame, step)
            post = int((coarse["time"] >= anomaly).sum())
            try:
                ranking = rank(coarse, anomaly, variant).top(MAX_K)
            except Exception as exc:
                rows.append({"incident": incident.incident_id, "step": step,
                             "fault_type": truth.fault_type, "error": str(exc)})
                continue
            rows.append({
                "incident": incident.incident_id,
                "step": step,
                "fault_type": truth.fault_type,
                "target": truth.target_service,
                "post_points": post,
                "top5": ranking,
                **{f"hit@{k}": truth.target_service in ranking[:k] for k in (1, 3, 5)},
            })
    return {"variant": variant, "rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict]) -> dict:
    scored = [r for r in rows if "error" not in r]
    steps = sorted({r["step"] for r in scored})
    out = {}
    for step in steps:
        group = [r for r in scored if r["step"] == step]
        out[str(step)] = {
            "n": len(group),
            **{f"AC@{k}": round(sum(r[f"hit@{k}"] for r in group) / len(group), 4)
               for k in (1, 3, 5)},
            "mean_post_points": round(sum(r["post_points"] for r in group) / len(group), 1),
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steps", type=int, nargs="+", default=[1, 5, 15])
    parser.add_argument("--variant", default="prism")
    args = parser.parse_args(argv)

    settings = load_settings()
    result = sweep(args.steps, args.variant, settings)
    if not result["rows"]:
        print("no stored incident has ground truth yet; run a campaign first")
        return 1

    print(f"{'step':>5} {'n':>4} {'AC@1':>6} {'AC@3':>6} {'AC@5':>6} {'post points':>12}")
    for step, row in result["summary"].items():
        print(f"{step:>5} {row['n']:>4} {row['AC@1']:>6} {row['AC@3']:>6} {row['AC@5']:>6} "
              f"{row['mean_post_points']:>12}")

    directory = settings.results_dir / "eval" / f"granularity-{int(time.time())}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sweep.json").write_text(json.dumps(result, indent=2))
    print(f"-> {directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
