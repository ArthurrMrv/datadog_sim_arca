"""Command line entry points used by the Makefile.

    python -m app.cli analyze --at 1726830000     # Phase 5: rank a past window, with no alert
    python -m app.cli replay  incident-id         # re-rank a stored incident with today's PRISM
    python -m app.cli freshness                   # how recent each metric family is (make status)
    python -m app.cli poll                        # fallback trigger loop (D16)
"""

from __future__ import annotations

import argparse
import logging
import sys

from app.adapter.datadog import DatadogBackend
from app.config import load_settings
from app.contract import validate
from app.pipeline import Pipeline
from app.report import build, to_markdown
from app.runner import rank_all
from app.store import Store
from app.windows import from_anomaly_time


def _pipeline(settings):
    return Pipeline(settings=settings, backend=DatadogBackend(settings),
                    store=Store(settings.incidents_dir))


def analyze(args) -> int:
    settings = load_settings()
    incident = _pipeline(settings).run_offline(args.at, args.id)
    print(incident.markdown_path.read_text())
    print(f"-> {incident.directory}")
    return 0


def replay(args) -> int:
    """Re-rank a stored incident from its parquet, without touching Datadog.

    This is how variants are compared fairly (Phase 7.4): identical input, only the method
    changes.
    """
    settings = load_settings()
    store = Store(settings.incidents_dir)
    incident = store.get(args.incident_id)
    if incident is None or not incident.frame_path.exists():
        print(f"no stored frame for {args.incident_id}", file=sys.stderr)
        return 1
    frame = incident.load_frame()
    windows = from_anomaly_time(
        args.at or incident.load_report()["timeline"]["t_anomaly"], settings.windows
    )
    variants = tuple(args.variant) if args.variant else settings.prism_variants
    contract = validate(frame, windows.t_anomaly, settings.windows)
    rankings = rank_all(frame, windows.t_anomaly, variants)
    report = build(incident.incident_id, frame, windows, rankings, contract, {}, settings)
    print(to_markdown(report))
    return 0


def freshness(args) -> int:
    import time

    settings = load_settings()
    # Printed because every credential failure looks the same from the outside: knowing which site
    # and key length resolved is what separates a wrong .env from a genuinely empty metric.
    print(f"site={settings.dd_site}  api_key={len(settings.dd_api_key)} chars  "
          f"token={len(settings.dd_access_token)} chars\n")
    now = int(time.time())
    for family, last in DatadogBackend(settings).freshness().items():
        age = f"{now - last}s ago" if last else "no data"
        print(f"{family:<14} {age}")
    return 0


def poll(args) -> int:
    from app.poller import Poller

    settings = load_settings()
    Poller(settings, _pipeline(settings)).run()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(prog="app.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("analyze", help="pull a window around a known anomaly time and rank it")
    p.add_argument("--at", type=int, required=True, help="anomaly time, UTC unix seconds")
    p.add_argument("--id", help="incident id to store under")
    p.set_defaults(func=analyze)

    p = sub.add_parser("replay", help="re-rank a stored incident")
    p.add_argument("incident_id")
    p.add_argument("--at", type=int, help="override the anomaly time")
    p.add_argument("--variant", action="append", help="PRISM variant (repeatable)")
    p.set_defaults(func=replay)

    p = sub.add_parser("freshness", help="age of the newest point per metric family")
    p.set_defaults(func=freshness)

    p = sub.add_parser("poll", help="trigger analyses by polling monitor states")
    p.set_defaults(func=poll)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
