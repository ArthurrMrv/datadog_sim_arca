"""Campaign scoring (Phase 7): ground truth in, AC@k and latencies out."""

from __future__ import annotations

from app.evaluation import Fault, load_ground_truth, match, score, unmatched


def fault(fault_type="cpu", service="cartservice", t_start=1_000_000, duration=300) -> Fault:
    return Fault(f"{fault_type}-{t_start}", fault_type, service, t_start, t_start + duration)


def incident(
    incident_id: str, anomaly: int, ranking: list[str], trigger: int | None = None
) -> dict:
    return {
        "incident_id": incident_id,
        "timeline": {"t_anomaly": anomaly, "t_trigger": trigger or anomaly + 90,
                     "pull_at": anomaly + 210},
        "rankings": [{
            "variant": "prism",
            "ranking": [{"service": s, "position": i} for i, s in enumerate(ranking, 1)],
        }],
    }


def test_a_fault_is_matched_to_the_incident_it_caused():
    cases = match([fault()], [incident("i1", 1_000_100, ["cartservice", "frontend"])])

    assert cases[0].detected
    assert cases[0].hit_at(1)
    assert cases[0].time_to_detect == 190


def test_a_fault_with_no_alert_is_a_missed_detection():
    cases = match([fault()], [])

    assert not cases[0].detected
    assert score(cases)["overall"]["missed_detections"] == 1


def test_an_incident_with_no_fault_is_a_false_alarm():
    """Measurable only because the campaign deliberately includes fault-free periods (Phase 7.3)."""
    incidents = [incident("noise", 2_000_000, ["adservice"])]
    cases = match([fault()], incidents)

    assert unmatched(incidents, cases) == ["noise"]
    assert score(cases, unmatched(incidents, cases))["overall"]["false_alarms"] == 1


def test_one_incident_is_never_credited_to_two_faults():
    faults = [fault(t_start=1_000_000), fault(t_start=1_000_000 + 900)]
    cases = match(faults, [incident("i1", 1_000_100, ["cartservice"])])

    assert [c.incident_id for c in cases] == ["i1", None]


def test_accuracy_at_k_and_avg5():
    cases = match(
        [fault(service="cartservice"), fault(service="adservice", t_start=2_000_000)],
        [
            incident("i1", 1_000_100, ["cartservice", "frontend", "adservice"]),
            incident("i2", 2_000_100, ["frontend", "cartservice", "adservice"]),
        ],
    )

    result = score(cases)["overall"]

    assert result["AC@1"] == 0.5   # only the first ranking puts the target first
    assert result["AC@3"] == 1.0
    assert result["Avg@5"] == 0.8  # mean of AC@1..AC@5 = 0.5, 0.5, 1, 1, 1


def test_results_are_broken_down_per_fault_type():
    """Performance differs a lot between a CPU fault and a delay; one average hides that."""
    cases = match(
        [fault("cpu", t_start=1_000_000), fault("delay", t_start=2_000_000)],
        [
            incident("i1", 1_000_100, ["cartservice"]),
            incident("i2", 2_000_100, ["frontend", "cartservice"]),
        ],
    )

    per_type = score(cases)["per_fault_type"]

    assert per_type["cpu"]["AC@1"] == 1.0
    assert per_type["delay"]["AC@1"] == 0.0


def test_ground_truth_round_trips(tmp_path):
    import json

    path = tmp_path / "ground_truth.jsonl"
    path.write_text(json.dumps({
        "fault_id": "cpu-1", "fault_type": "cpu", "target_service": "cartservice",
        "params": {"workers": "2"}, "t_start": 1_000_000, "t_end": 1_000_300,
    }) + "\n")

    assert load_ground_truth(path) == [
        Fault("cpu-1", "cpu", "cartservice", 1_000_000, 1_000_300, {"workers": "2"})
    ]


def test_cooldown_outlasts_the_baseline_window():
    """A campaign's own pacing must not put the previous fault inside the next one's baseline.

    inject.py starts the next fault at `t_start + duration + cooldown`, and that incident's
    baseline reaches `baseline_seconds` back from there. If cooldown is shorter, the reference
    window contains the tail of the previous fault -- inflating the mean and std for exactly the
    services it touched, which suppresses the next fault's scores. At cooldown=300 with a 600s
    baseline, half of every reference window was the previous fault at full strength.
    """
    import pathlib

    import yaml
    from app.config import WindowConfig

    root = pathlib.Path(__file__).resolve().parents[2]
    recovery_margin = 120
    for path in sorted((root / "experiments" / "configs").glob("*.yaml")):
        config = yaml.safe_load(path.read_text())
        if not config.get("scorable", True):
            continue  # a plumbing check, exempt by declaring itself unscorable
        assert config["cooldown_seconds"] >= WindowConfig().baseline_seconds + recovery_margin, (
            f"{path.name}: cooldown {config['cooldown_seconds']}s does not clear a "
            f"{WindowConfig().baseline_seconds}s baseline plus {recovery_margin}s of recovery"
        )
