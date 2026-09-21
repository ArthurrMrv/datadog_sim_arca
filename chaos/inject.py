#!/usr/bin/env python3
"""Inject one Chaos Mesh fault and log it as ground truth (Phase 4).

Ground truth is what turns this from a demo into a measurable experiment: without a logged
(fault_type, target_service, t_start, t_end) there is nothing to score a ranking against.

Usage:
    python chaos/inject.py cpu cartservice --duration 300
    python chaos/inject.py delay frontend --duration 180 --set latency=200ms
    python chaos/inject.py cpu cartservice --dry-run     # render only, change nothing
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from string import Template

ROOT = Path(__file__).resolve().parent.parent
FAULTS_DIR = ROOT / "chaos" / "faults"
GROUND_TRUTH = ROOT / "results" / "ground_truth.jsonl"
NAMESPACE = "shop"

# Defaults chosen to be clearly visible against the Phase 1 limits (300m CPU, 256Mi memory)
# without killing the service outright. Override any of them with --set key=value.
DEFAULTS: dict[str, dict[str, str]] = {
    "cpu": {"workers": "2", "load": "80"},
    "mem": {"workers": "1", "size": "100MB"},
    "delay": {"latency": "200ms", "jitter": "50ms"},
    "loss": {"loss": "20"},
    "disk": {"volume_path": "/tmp", "delay": "100ms", "percent": "80"},
    "kill": {},
    "code": {"port": "8080"},
}

# Cooldown after the fault ends, so the next injection starts from a clean baseline (Phase 4.3).
DEFAULT_COOLDOWN_SECONDS = 300


@dataclass(frozen=True)
class GroundTruth:
    """One injected fault, in UTC unix seconds."""

    fault_id: str
    fault_type: str
    target_service: str
    params: dict[str, str]
    t_start: int
    t_end: int


def render(
    fault_type: str, service: str, duration: int, overrides: dict[str, str]
) -> tuple[str, str]:
    """Render a fault template. Returns (fault_id, manifest)."""
    if fault_type not in DEFAULTS:
        raise SystemExit(f"unknown fault {fault_type!r}; known: {', '.join(sorted(DEFAULTS))}")
    params = DEFAULTS[fault_type] | overrides
    fault_id = f"{fault_type}-{service}-{uuid.uuid4().hex[:8]}"
    template = Template((FAULTS_DIR / f"{fault_type}.yaml").read_text())
    manifest = template.substitute(
        fault_id=fault_id, service=service, duration=duration, **params
    )
    return fault_id, manifest


def kubectl(*args: str, stdin: str | None = None) -> str:
    """Run kubectl, and on failure raise with what kubectl actually said.

    `capture_output=True` with `check=True` hides stderr inside the exception object, so a rejected
    manifest surfaced as a bare "returned non-zero exit status 1" -- the useful half of the message
    thrown away at the moment it was needed.
    """
    done = subprocess.run(["kubectl", *args], input=stdin, text=True, capture_output=True)
    if done.returncode != 0:
        raise RuntimeError(
            f"kubectl {' '.join(args)} failed ({done.returncode}): "
            f"{(done.stderr or done.stdout).strip()}"
        )
    return done.stdout


def assert_healthy() -> None:
    """Refuse to inject into a system that is not at baseline (Phase 4.3).

    A fault injected on top of an unfinished one produces ground truth naming a single service
    while two are broken, which silently corrupts every score computed from it.
    """
    pods = json.loads(kubectl("-n", NAMESPACE, "get", "pods", "-o", "json"))["items"]
    unready = [
        p["metadata"]["name"]
        for p in pods
        if not all(c.get("ready") for c in p.get("status", {}).get("containerStatuses", []))
    ]
    if unready:
        raise SystemExit(f"not at baseline, pods not ready: {', '.join(unready)}")
    active = _active_experiments()
    if active:
        raise SystemExit(f"a chaos experiment is still active: {', '.join(active)}")


CHAOS_KINDS = ("stresschaos", "networkchaos", "podchaos", "iochaos", "httpchaos")


def _active_experiments() -> list[str]:
    """Chaos Mesh objects still in the namespace (empty before Chaos Mesh is installed)."""
    try:
        out = kubectl("-n", NAMESPACE, "get", ",".join(CHAOS_KINDS), "-o", "json")
    except RuntimeError:
        return []  # Chaos Mesh not installed yet: nothing can be running
    return [item["metadata"]["name"] for item in json.loads(out)["items"]]


def log_ground_truth(record: GroundTruth) -> None:
    GROUND_TRUTH.parent.mkdir(parents=True, exist_ok=True)
    with GROUND_TRUTH.open("a") as handle:
        handle.write(json.dumps(asdict(record)) + "\n")


def inject(
    fault_type: str,
    service: str,
    duration: int,
    overrides: dict[str, str] | None = None,
    cooldown: int = DEFAULT_COOLDOWN_SECONDS,
    skip_health_check: bool = False,
) -> GroundTruth:
    """Apply a fault, log it, wait it out, remove it and cool down. Returns the ground truth."""
    fault_id, manifest = render(fault_type, service, duration, overrides or {})
    if not skip_health_check:
        assert_healthy()

    t_start = int(time.time())
    kubectl("apply", "-f", "-", stdin=manifest)
    record = GroundTruth(
        fault_id=fault_id,
        fault_type=fault_type,
        target_service=service,
        params=DEFAULTS[fault_type] | (overrides or {}),
        t_start=t_start,
        t_end=t_start + duration,
    )
    log_ground_truth(record)
    print(f"injected {fault_id} at t_start={t_start} (logged to {GROUND_TRUTH})")

    try:
        time.sleep(duration)
    finally:
        # Chaos Mesh removes the effect when the duration elapses; deleting the object as well
        # means a cancelled run never leaves the cluster faulted.
        subprocess.run(
            ["kubectl", "-n", NAMESPACE, "delete", "-f", "-", "--ignore-not-found"],
            input=manifest, text=True, capture_output=True, check=False,
        )
    print(f"fault ended at {record.t_end}; cooling down {cooldown}s")
    time.sleep(cooldown)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fault", choices=sorted(DEFAULTS))
    parser.add_argument("service")
    parser.add_argument(
        "--duration", type=int, default=300, help="seconds the fault stays active"
    )
    parser.add_argument("--cooldown", type=int, default=DEFAULT_COOLDOWN_SECONDS)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--dry-run", action="store_true", help="print the manifest and exit")
    parser.add_argument("--skip-health-check", action="store_true")
    args = parser.parse_args(argv)

    overrides = dict(pair.split("=", 1) for pair in args.set)
    if args.dry_run:
        print(render(args.fault, args.service, args.duration, overrides)[1])
        return 0
    inject(
        args.fault, args.service, args.duration, overrides,
        cooldown=args.cooldown, skip_health_check=args.skip_health_check,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
