"""Tests for the action-safety gate.

Inputs here are CRAFTED, not recorded from a model. That is deliberate: the point
is to prove the gate refuses specific dangerous shapes, and crafting the exact
shape is the only reliable way to cover them. Recorded cassettes cover what a real
model actually says; these cover what happens when it says something unsafe.

Every case is a refusal the gate must make, because refusal is the only safe
default when the alternative is an irreversible production action.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.gate import review  # noqa: E402
from agent.runbooks import load_all  # noqa: E402

RUNBOOKS = load_all()
KNOWN = ["gateway", "orders", "payments", "inventory", "recommender",
         "ledger", "cache"]
CANDIDATES = ["ledger", "payments", "orders", "gateway"]


def _review(proposal):
    return review(proposal, candidates=CANDIDATES, known_services=KNOWN,
                  runbooks=RUNBOOKS)


def test_current_runbooks_load_and_supersede():
    ids = {b.id for b in RUNBOOKS}
    assert "rb-ledger-latency-v2" in ids
    assert "rb-payments-errors-v3" in ids
    # v1/v2 are superseded and must not be offered as current.
    assert "rb-ledger-latency-v1" not in ids
    assert "rb-payments-errors-v2" not in ids


def test_hallucinated_service_is_refused():
    v = _review({"root_cause": "billing-service-v2", "actions": [
        {"verb": "restart", "target": "billing-service-v2"}]})
    assert not v.ok and v.diagnosis_only
    assert any("not a service present" in e for e in v.errors)


def test_root_cause_outside_rca_candidates_is_refused():
    """The model does not get to pick its own root cause."""
    v = _review({"root_cause": "recommender", "actions": [
        {"verb": "restart", "target": "recommender"}]})
    assert not v.ok and v.diagnosis_only
    assert any("not among the RCA candidates" in e for e in v.errors)


def test_irreversible_action_without_runbook_is_refused():
    v = _review({"root_cause": "ledger", "runbook_id": None, "actions": [
        {"verb": "failover", "target": "ledger"}]})
    assert v.refusals and v.refusals[0]["reason"] == "no_runbook"
    assert v.diagnosis_only


def test_superseded_runbook_is_refused():
    """A stale runbook winning retrieval is a real failure mode, so citing one is
    refused rather than silently honoured."""
    v = _review({"root_cause": "ledger", "runbook_id": "rb-ledger-latency-v1",
                 "actions": [{"verb": "failover", "target": "ledger",
                              "preconditions_checked": [
                                  "replica_lag_under_10s",
                                  "no_active_schema_migration",
                                  "primary_confirmed_unhealthy"]}]})
    assert v.refusals and v.refusals[0]["reason"] == "superseded_runbook"


def test_unknown_runbook_is_refused():
    v = _review({"root_cause": "ledger", "runbook_id": "rb-does-not-exist",
                 "actions": [{"verb": "failover", "target": "ledger"}]})
    assert v.refusals and v.refusals[0]["reason"] == "unknown_runbook"


def test_action_not_listed_in_runbook_is_refused():
    """rb-payments-errors-v3 explicitly forbids restarting payments: in-flight
    authorizations are not idempotent."""
    v = _review({"root_cause": "payments", "runbook_id": "rb-payments-errors-v3",
                 "actions": [{"verb": "restart", "target": "payments",
                              "preconditions_checked": ["recent_deploy_within_1h"]}]})
    assert v.refusals and v.refusals[0]["reason"] == "not_authorised"
    assert v.diagnosis_only


def test_missing_precondition_is_refused_and_names_it():
    v = _review({"root_cause": "ledger", "runbook_id": "rb-ledger-latency-v2",
                 "actions": [{"verb": "failover", "target": "ledger",
                              "preconditions_checked": ["replica_lag_under_10s"]}]})
    assert v.refusals
    r = v.refusals[0]
    assert r["reason"] == "unmet_preconditions"
    assert "no_active_schema_migration" in r["detail"]
    assert "primary_confirmed_unhealthy" in r["detail"]


def test_fully_satisfied_action_is_allowed():
    v = _review({"root_cause": "ledger", "runbook_id": "rb-ledger-latency-v2",
                 "actions": [{"verb": "failover", "target": "ledger",
                              "preconditions_checked": [
                                  "replica_lag_under_10s",
                                  "no_active_schema_migration",
                                  "primary_confirmed_unhealthy"]}]})
    assert v.ok, v.to_dict()
    assert len(v.allowed_actions) == 1
    assert not v.diagnosis_only


def test_advisory_actions_need_no_runbook():
    v = _review({"root_cause": "ledger", "actions": [
        {"verb": "investigate", "target": "ledger"},
        {"verb": "notify", "target": "oncall"}]})
    assert v.ok
    assert len(v.allowed_actions) == 2


def test_mixed_proposal_allows_safe_and_refuses_unsafe():
    v = _review({"root_cause": "ledger", "runbook_id": "rb-ledger-latency-v2",
                 "actions": [
                     {"verb": "investigate", "target": "ledger"},
                     {"verb": "failover", "target": "ledger",
                      "preconditions_checked": ["replica_lag_under_10s"]},
                 ]})
    assert len(v.allowed_actions) == 1
    assert v.allowed_actions[0]["verb"] == "investigate"
    assert len(v.refusals) == 1
    assert v.refusals[0]["reason"] == "unmet_preconditions"


def test_no_actions_means_diagnosis_only():
    v = _review({"root_cause": "ledger", "actions": []})
    assert v.diagnosis_only
    assert v.ok


def test_malformed_action_is_refused_not_crashed():
    v = _review({"root_cause": "ledger", "runbook_id": "rb-ledger-latency-v2",
                 "actions": [{"target": "ledger"}, {"verb": "failover"}]})
    assert len(v.refusals) == 2
    assert all(r["reason"] == "malformed" for r in v.refusals)


def test_empty_proposal_does_not_crash():
    v = _review({})
    assert v.diagnosis_only
    assert v.errors
