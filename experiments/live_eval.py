#!/usr/bin/env python3
"""Live evaluation campaign: inject -> detect -> rank -> score (Phase 7).

    python experiments/live_eval.py --config experiments/configs/base.yaml
    python experiments/live_eval.py --score-only          # score what already happened

The campaign is scored from two independent records: `results/ground_truth.jsonl`, written at
injection time, and the incident reports written by rca-service. Nothing in the scoring path can
see the injection, which is what keeps the numbers honest.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
import time
from dataclasses import asdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "rca-service"))
sys.path.insert(0, str(ROOT / "chaos"))

from app.config import load_settings
from app.evaluation import load_ground_truth, match, score, unmatched
from app.store import Store


def load_incidents(store: Store) -> list[dict]:
    reports = []
    for incident in store.list():
        if incident.report_path.exists():
            try:
                reports.append(incident.load_report())
            except json.JSONDecodeError:
                continue
    return reports


def wait_for_incident(store: Store, after: int, timeout: int) -> dict | None:
    """Wait for a report whose incident started after `after`. None means a missed detection."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for report in load_incidents(store):
            if report["timeline"]["t_anomaly"] >= after - 60:
                return report
        time.sleep(10)
    return None


def run_campaign(config: dict, settings, dry_run: bool = False) -> None:
    import inject as chaos  # chaos/inject.py

    store = Store(settings.incidents_dir)
    plan = [
        (fault, service)
        for _, fault, service in itertools.product(
            range(config["repetitions"]), config["faults"], config["services"]
        )
    ]
    random.shuffle(plan)  # so a drift in the cluster does not line up with one fault type
    quiet = config.get("fault_free_periods", 0)

    per = config["duration_seconds"] + config["cooldown_seconds"]
    hours = (len(plan) * per + quiet * config.get("fault_free_seconds", 0)) / 3600
    print(f"{len(plan)} injections + {quiet} fault-free periods "
          f"= {hours:.1f} h of wall clock at {per // 60} min each")
    failures: list[tuple[str, str, str]] = []
    for index, (fault, service) in enumerate(plan, start=1):
        print(f"[{index}/{len(plan)}] {fault} -> {service}")
        if dry_run:
            continue
        started = int(time.time())
        try:
            chaos.inject(
                fault, service, config["duration_seconds"], cooldown=config["cooldown_seconds"]
            )
        except Exception as exc:
            # A fault type the cluster rejects is a gap in the experiment, not a reason to abandon
            # the other nineteen. It is recorded rather than swallowed: an injection that never
            # happened must not be mistaken later for one that happened and went undetected.
            print(f"   INJECTION FAILED: {exc}")
            failures.append((fault, service, str(exc)))
            continue
        report = wait_for_incident(store, started, config["incident_timeout_seconds"])
        print("   detected" if report else "   no alert (missed detection)")

        if quiet and index % max(1, len(plan) // max(quiet, 1)) == 0:
            print(f"   fault-free period: {config['fault_free_seconds']}s")
            time.sleep(config["fault_free_seconds"])

    if failures:
        print(f"\n{len(failures)} injections never ran:")
        for fault, service, reason in failures:
            print(f"  {fault} -> {service}: {reason.splitlines()[0][:120]}")


def score_campaign(config: dict, settings) -> dict:
    store = Store(settings.incidents_dir)
    faults = load_ground_truth(settings.ground_truth_path)
    incidents = load_incidents(store)
    cases = match(faults, incidents, config.get("match_slack_seconds", 120))
    result = score(cases, unmatched(incidents, cases))
    result["cases"] = [
        asdict(case.fault) | {
            "incident_id": case.incident_id,
            "top5": case.ranking[:5],
            "time_to_detect_s": case.time_to_detect,
        }
        for case in cases
    ]
    return result


def write_results(result: dict, settings) -> Path:
    directory = settings.results_dir / "eval" / str(int(time.time()))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "summary.json").write_text(json.dumps(result, indent=2))
    (directory / "summary.md").write_text(to_markdown(result))
    return directory


def to_markdown(result: dict) -> str:
    overall = result["overall"]
    lines = [
        "# Live evaluation",
        "",
        (f"{overall['n_faults']} faults, {overall['n_detected']} detected, "
         f"{overall['missed_detections']} missed, {overall['false_alarms']} false alarms."),
        "",
        "| group | n | AC@1 | AC@3 | AC@5 | Avg@5 | detect (s) | diagnose (s) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for name, group in [("overall", overall), *sorted(result["per_fault_type"].items())]:
        detect = (group["time_to_detect_s"] or {}).get("mean", "-")
        diagnose = (group["time_to_diagnosis_s"] or {}).get("mean", "-")
        lines.append(
            f"| {name} | {group['n_faults']} | {group['AC@1']} | {group['AC@3']} | "
            f"{group['AC@5']} | {group['Avg@5']} | {detect} | {diagnose} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(ROOT / "experiments" / "configs" / "base.yaml"))
    parser.add_argument(
        "--score-only", action="store_true", help="score without injecting anything"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    args = parser.parse_args(argv)

    config = yaml.safe_load(Path(args.config).read_text())
    settings = load_settings()
    if not args.score_only:
        run_campaign(config, settings, dry_run=args.dry_run)
    if args.dry_run:
        return 0
    result = score_campaign(config, settings)
    print(to_markdown(result))
    print(f"-> {write_results(result, settings)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
