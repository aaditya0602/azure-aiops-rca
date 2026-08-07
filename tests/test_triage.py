"""End-to-end triage orchestration, with the model replaced by a stub.

A stub rather than a cassette because these tests are about MY code -- evidence
assembly, JSON extraction, gate integration, postmortem rendering, detection-rule
emission -- not about what a model says. Cassettes (recorded from a real provider)
are the right tool for the latter and require a key.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.providers import Provider, prompt_key  # noqa: E402
from agent.runbooks import load_all  # noqa: E402
from agent.triage import triage  # noqa: E402
from analyzer.features import Incident, extract  # noqa: E402
from analyzer.model import Topology, read_spans  # noqa: E402
from analyzer.rca import rank_all  # noqa: E402
from harness import synth  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SMALL = str(REPO / "topology" / "small.yaml")


class StubProvider(Provider):
    name = "stub"

    def __init__(self, payload: str):
        self.payload = payload
        self.calls = 0
        self.last_user = ""

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        self.last_user = user
        return self.payload


@pytest.fixture(scope="module")
def incident_fixture(tmp_path_factory):
    """A ledger latency fault, localized. Reused across the triage tests."""
    tmp = tmp_path_factory.mktemp("triage")
    topo = Topology.load(SMALL)
    faults = [synth.Fault("latency", "ledger", 60_000.0, 120_000.0, 8.0, 0)]
    sim = synth.Simulator(topo, seed=11)
    spans: list[dict] = []
    for i in range(1600):
        sim.simulate("gateway", f"t{i:06x}", None, i * 100.0, faults, spans)
    spans.sort(key=lambda s: s["start_ms"])
    synth.write_spans(tmp / "s.jsonl", spans)

    feat = extract(read_spans(tmp / "s.jsonl"), bucket_ms=5000.0)
    inc = Incident(b_start=feat.bucket_of(65_000.0), b_end=feat.bucket_of(115_000.0),
                   t_start_ms=65_000.0, t_end_ms=115_000.0, peak_bucket=14)
    ranking = rank_all(feat, inc, topo.entrypoints)["attributed"]
    assert ranking.top(1) == ["ledger"], "fixture precondition"
    return feat, inc, ranking, load_all()


def test_evidence_only_contains_measured_numbers(incident_fixture):
    feat, inc, ranking, books = incident_fixture
    stub = StubProvider(json.dumps({
        "root_cause": "ledger", "confidence": "high", "reasoning": "ledger self time up",
        "runbook_id": None, "actions": []}))
    res = triage(stub, feat, inc, ranking, books)

    assert stub.calls == 1
    sig = res.evidence["signals"]
    assert sig and sig[0]["service"] == "ledger"
    # Every number shown to the model is one the analyzer used.
    for row in sig:
        assert set(row) >= {"self_ms", "self_ms_baseline", "self_severity_log2",
                            "own_error_rate", "requests"}
    # The candidate list is passed through verbatim.
    assert res.candidates == ranking.top(4)
    assert "ledger" in stub.last_user


def test_fenced_json_is_extracted(incident_fixture):
    feat, inc, ranking, books = incident_fixture
    stub = StubProvider(
        "Here is my analysis.\n```json\n"
        '{"root_cause": "ledger", "confidence": "medium", "reasoning": "r",\n'
        ' "runbook_id": null, "actions": []}\n```\nHope that helps.")')
    res = triage(stub, feat, inc, ranking, books)
    assert res.parse_error is None
    assert res.proposal["root_cause"] == "ledger"


def test_unparseable_response_degrades_to_diagnosis_not_crash(incident_fixture):
    feat, inc, ranking, books = incident_fixture
    res = triage(StubProvider("I cannot help with that."), feat, inc, ranking, books)
    assert res.parse_error is not None
    assert res.verdict["diagnosis_only"] is True
    assert res.verdict["allowed_actions"] == []
    assert "postmortem" in res.to_dict()


def test_unsafe_proposal_is_refused_and_recorded_in_postmortem(incident_fixture):
    feat, inc, ranking, books = incident_fixture
    stub = StubProvider(json.dumps({
        "root_cause": "ledger", "confidence": "high",
        "reasoning": "failover now",
        "runbook_id": "rb-ledger-latency-v2",
        "actions": [{"verb": "failover", "target": "ledger",
                     "preconditions_checked": ["replica_lag_under_10s"]}]}))
    res = triage(stub, feat, inc, ranking, books)

    assert res.verdict["allowed_actions"] == []
    assert res.verdict["refusals"][0]["reason"] == "unmet_preconditions"
    assert res.verdict["diagnosis_only"] is True
    assert "REFUSED" in res.postmortem


def test_satisfied_proposal_is_approved(incident_fixture):
    feat, inc, ranking, books = incident_fixture
    stub = StubProvider(json.dumps({
        "root_cause": "ledger", "confidence": "high", "reasoning": "ok",
        "runbook_id": "rb-ledger-latency-v2",
        "actions": [{"verb": "failover", "target": "ledger",
                     "preconditions_checked": [
                         "replica_lag_under_10s", "no_active_schema_migration",
                         "primary_confirmed_unhealthy"]}]}))
    res = triage(stub, feat, inc, ranking, books)
    assert len(res.verdict["allowed_actions"]) == 1
    assert "APPROVED" in res.postmortem


def test_detection_rule_targets_the_signal_that_moved(incident_fixture):
    feat, inc, ranking, books = incident_fixture
    stub = StubProvider(json.dumps({
        "root_cause": "ledger", "confidence": "high", "reasoning": "r",
        "runbook_id": None, "actions": []}))
    res = triage(stub, feat, inc, ranking, books)

    rule = res.detection_rule
    assert rule["service"] == "ledger"
    # A latency fault must produce a latency rule, above the measured baseline.
    assert rule["signal"] in ("lat_self", "lat_incl")
    assert rule["threshold"] > rule["baseline"] > 0
    assert rule["observed_severity_log2"] > 1.0


def test_prompt_key_is_stable_and_prompt_sensitive():
    a = prompt_key("m", "sys", "user")
    assert a == prompt_key("m", "sys", "user")
    assert a != prompt_key("m", "sys", "user2")
    assert a != prompt_key("m2", "sys", "user")
