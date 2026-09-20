"""Alert parsing, deduplication and the webhook endpoint."""

from __future__ import annotations

from app.alerts import Alert, Deduper, from_webhook
from fastapi.testclient import TestClient


def payload(**overrides) -> dict:
    return {
        "monitor_id": "123",
        "monitor_name": "rca-sim latency p95 by service",
        "transition": "Triggered",
        "event_ts": "1726830000",
        "scope": "service:cartservice",
        "tags": "rca-sim,env:rca-sim",
    } | overrides


def test_webhook_payload_is_parsed():
    alert = from_webhook(payload())

    assert alert.is_trigger
    assert alert.t_trigger == 1_726_830_000
    assert alert.tags == ("rca-sim", "env:rca-sim")


def test_milliseconds_are_converted():
    """Datadog sends some event timestamps in milliseconds; a 1000x shift is unrecoverable."""
    assert from_webhook(payload(event_ts=1_726_830_000_000)).t_trigger == 1_726_830_000


def test_recovery_is_not_a_trigger():
    assert not from_webhook(payload(transition="Recovered")).is_trigger


def test_unrendered_template_variable_does_not_become_1970():
    """If the webhook template is wrong, fall back to now rather than analysing the epoch."""
    assert from_webhook(payload(event_ts="$LAST_UPDATED_EPOCH")).t_trigger > 1_700_000_000


def test_one_fault_many_monitors_is_one_incident():
    """A CPU fault trips latency, errors and saturation monitors: one incident, not three."""
    deduper = Deduper(dedupe_seconds=300)
    burst = [
        Alert("1", "latency", "Triggered", 1_726_830_000, "service:frontend"),
        Alert("2", "errors", "Triggered", 1_726_830_020, "service:cartservice"),
        Alert("3", "cpu", "Triggered", 1_726_830_045, "kube_deployment:cartservice"),
    ]

    accepted = [deduper.accept(alert) for alert in burst]

    assert accepted[0] is not None
    assert accepted[1:] == [None, None]


def test_a_later_fault_is_a_new_incident():
    deduper = Deduper(dedupe_seconds=300)
    first = deduper.accept(Alert("1", "latency", "Triggered", 1_726_830_000))
    second = deduper.accept(Alert("1", "latency", "Triggered", 1_726_830_000 + 601))

    assert first and second and first != second


def _client(monkeypatch, secret="s3cret"):
    from app import main

    monkeypatch.setenv("WEBHOOK_SECRET", secret)
    monkeypatch.setenv("DD_API_KEY", "")
    return TestClient(main.app)


def test_webhook_rejects_a_wrong_secret(monkeypatch):
    with _client(monkeypatch) as client:
        assert client.post("/webhook", json=payload(), headers={"X-RCA-Secret": "no"}).status_code \
            == 401


def test_webhook_accepts_queues_and_deduplicates(monkeypatch):
    """The handler must answer immediately: the analysis deliberately waits minutes (D12)."""
    from app import main

    queued = []
    monkeypatch.setattr(main, "_analyze", lambda incident_id, alert: queued.append(incident_id))

    with _client(monkeypatch) as client:
        headers = {"X-RCA-Secret": "s3cret"}
        first = client.post("/webhook", json=payload(), headers=headers).json()
        second = client.post("/webhook", json=payload(monitor_id="9"), headers=headers).json()
        ignored = client.post(
            "/webhook", json=payload(transition="Recovered"), headers=headers
        ).json()

    assert first["status"] == "accepted"
    assert second["status"] == "deduplicated"
    assert ignored["status"] == "ignored"
    assert queued == [first["incident_id"]]


def test_health_reports_configuration(monkeypatch):
    with _client(monkeypatch) as client:
        body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["datadog_configured"] is False
