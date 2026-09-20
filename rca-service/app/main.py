"""rca-service: receives Datadog alerts and answers with a root cause report (Phase 6.4).

The webhook must answer immediately — Datadog retries slow endpoints, and the analysis
deliberately waits minutes for the post-fault window (D12) — so the HTTP handler only validates,
deduplicates and queues.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from app.adapter.datadog import DatadogBackend
from app.alerts import Deduper, from_webhook
from app.config import Settings, load_settings
from app.pipeline import Pipeline
from app.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("rca-service")


def build_pipeline(settings: Settings) -> Pipeline:
    return Pipeline(
        settings=settings,
        backend=DatadogBackend(settings),
        store=Store(settings.incidents_dir),
    )


@asynccontextmanager
async def lifespan(application: FastAPI):
    settings = load_settings()
    application.state.settings = settings
    application.state.store = Store(settings.incidents_dir)
    application.state.deduper = Deduper(settings.dedupe_seconds)
    # Built lazily, so the service starts (and answers /health) without Datadog keys.
    application.state.pipeline = None
    log.info("results in %s, dedupe window %ss", settings.incidents_dir, settings.dedupe_seconds)
    yield


app = FastAPI(title="rca-sim RCA service", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    settings: Settings = app.state.settings
    return {
        "status": "ok",
        "site": settings.dd_site,
        "datadog_configured": bool(settings.dd_api_key and settings.dd_access_token),
        "variants": list(settings.prism_variants),
    }


@app.post("/webhook")
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_rca_secret: str = Header(default=""),
) -> dict:
    settings: Settings = app.state.settings
    if settings.webhook_secret and not hmac.compare_digest(x_rca_secret, settings.webhook_secret):
        raise HTTPException(status_code=401, detail="bad secret")

    alert = from_webhook(await request.json())
    if not alert.is_trigger:
        return {"status": "ignored", "reason": f"transition={alert.transition!r}"}
    incident_id = app.state.deduper.accept(alert)
    if incident_id is None:
        return {"status": "deduplicated", "monitor": alert.monitor_name}

    background.add_task(_analyze, incident_id, alert)
    log.info("incident %s queued from %s", incident_id, alert.monitor_name)
    return {"status": "accepted", "incident_id": incident_id}


async def _analyze(incident_id: str, alert) -> None:
    """Run the blocking pipeline off the event loop."""
    if app.state.pipeline is None:
        app.state.pipeline = build_pipeline(app.state.settings)
    try:
        await asyncio.to_thread(app.state.pipeline.run_alert, alert, incident_id)
    except Exception:
        log.exception("incident %s failed", incident_id)


@app.get("/incidents")
def incidents() -> dict:
    return {
        "incidents": [
            {"incident_id": i.incident_id, "has_report": i.report_path.exists()}
            for i in app.state.store.list()
        ]
    }


@app.get("/incidents/{incident_id}")
def incident(incident_id: str) -> dict:
    found = app.state.store.get(incident_id)
    if found is None or not found.report_path.exists():
        raise HTTPException(status_code=404, detail="no report for this incident")
    return found.load_report()
