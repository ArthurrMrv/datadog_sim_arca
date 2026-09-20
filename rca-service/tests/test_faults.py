"""Fault templates (Phase 4): every one renders to a valid, correctly targeted experiment.

A template that renders wrong is expensive to find later: the injection silently hits nothing, the
monitor never fires, and the run is scored as a missed detection that PRISM never had a chance at.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _inject_module():
    spec = importlib.util.spec_from_file_location("chaos_inject", ROOT / "chaos" / "inject.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["chaos_inject"] = module
    spec.loader.exec_module(module)
    return module


inject = _inject_module()

# The RCAEval fault families, which is what makes live results comparable with the benchmark ones.
EXPECTED_FAULTS = {"cpu", "mem", "delay", "loss", "disk", "kill", "code"}


def test_every_rcaeval_fault_family_has_a_template():
    assert set(inject.DEFAULTS) == EXPECTED_FAULTS
    for fault in EXPECTED_FAULTS:
        assert (ROOT / "chaos" / "faults" / f"{fault}.yaml").exists()


@pytest.mark.parametrize("fault", sorted(EXPECTED_FAULTS))
def test_template_renders_and_targets_the_service(fault):
    _, manifest = inject.render(fault, "cartservice", 300, {})
    spec = yaml.safe_load(manifest)

    assert spec["apiVersion"].startswith("chaos-mesh.org/")
    assert spec["metadata"]["namespace"] == "shop"
    assert spec["spec"]["selector"]["labelSelectors"]["app"] == "cartservice"


@pytest.mark.parametrize("fault", sorted(EXPECTED_FAULTS - {"kill"}))
def test_duration_is_applied(fault):
    """A fault without a duration would outlive its ground truth and poison the next run."""
    _, manifest = inject.render(fault, "cartservice", 180, {})

    assert yaml.safe_load(manifest)["spec"]["duration"] == "180s"


def test_overrides_reach_the_manifest():
    _, manifest = inject.render("delay", "frontend", 60, {"latency": "500ms"})

    assert yaml.safe_load(manifest)["spec"]["delay"]["latency"] == "500ms"


def test_fault_ids_are_unique():
    """Ground truth is keyed by fault id; a collision would merge two runs into one."""
    ids = {inject.render("cpu", "cartservice", 60, {})[0] for _ in range(20)}

    assert len(ids) == 20


def test_unknown_fault_is_refused():
    with pytest.raises(SystemExit, match="unknown fault"):
        inject.render("meltdown", "cartservice", 60, {})


def test_ground_truth_record_is_complete(tmp_path, monkeypatch):
    """Every field the evaluation needs must be written at injection time, in unix seconds."""
    monkeypatch.setattr(inject, "GROUND_TRUTH", tmp_path / "ground_truth.jsonl")
    record = inject.GroundTruth(
        "cpu-1", "cpu", "cartservice", {"workers": "2"}, 1_000_000, 1_000_300
    )

    inject.log_ground_truth(record)

    import json

    written = json.loads((tmp_path / "ground_truth.jsonl").read_text())
    assert written == {
        "fault_id": "cpu-1", "fault_type": "cpu", "target_service": "cartservice",
        "params": {"workers": "2"}, "t_start": 1_000_000, "t_end": 1_000_300,
    }
