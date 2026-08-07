"""Action-safety gate.

The agent proposes; this decides. Every check here exists because an LLM will
cheerfully produce a fluent, wrong, irreversible instruction, and the cost of
"restart payments" is not symmetric with the cost of refusing it.

Five checks, in order of how badly they fail:

  1. GROUNDING     the named root cause must be a service that actually exists in
                   the telemetry-derived graph. Catches hallucinated service names.
  2. CANDIDACY     the named root cause must be in the RCA's ranked candidates.
                   The agent explains evidence; it does not get to invent its own.
  3. RUNBOOK       a cited runbook must exist, and must be the current revision --
                   a superseded runbook winning retrieval is a real failure mode.
  4. AUTHORISATION the proposed action must be listed as allowed in that runbook
                   for that target. Anything unlisted is refused by default.
  5. PRECONDITIONS every precondition the runbook requires must be explicitly
                   claimed as checked. Missing one refuses the action.

Refusal is the safe default everywhere. An action that fails any check is dropped
and the proposal degrades to diagnosis only, which is always safe to show a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from agent.runbooks import Runbook, by_id

# Verbs that change production state and cannot be trivially undone. These are the
# ones that require a runbook and preconditions; anything else is advisory.
IRREVERSIBLE_VERBS = frozenset({
    "restart", "failover", "delete", "revoke", "drain", "rollback",
    "scale", "evict", "truncate", "detach", "purge",
})


@dataclass
class Verdict:
    allowed_actions: list[dict] = field(default_factory=list)
    refusals: list[dict] = field(default_factory=list)
    diagnosis_only: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.refusals

    def to_dict(self) -> dict:
        return {
            "allowed_actions": self.allowed_actions,
            "refusals": self.refusals,
            "diagnosis_only": self.diagnosis_only,
            "errors": self.errors,
        }


def _refuse(verdict: Verdict, action: dict, reason: str, detail: str = "") -> None:
    verdict.refusals.append({
        "action": action,
        "reason": reason,
        "detail": detail,
    })


def review(proposal: dict, *, candidates: Iterable[str], known_services: Iterable[str],
           runbooks: list[Runbook]) -> Verdict:
    """Check one agent proposal. Never raises: a malformed proposal is a refusal,
    not a crash, because this runs in an incident path."""
    v = Verdict()
    cand = list(candidates)
    known = set(known_services)
    books = by_id(runbooks)          # current revisions only
    all_ids = {b.id for b in runbooks} | {b.supersedes for b in runbooks if b.supersedes}

    root = proposal.get("root_cause")
    actions = proposal.get("actions") or []

    # 1. grounding
    if not root or root not in known:
        v.errors.append(
            f"root_cause {root!r} is not a service present in the telemetry")
        v.diagnosis_only = True
        return v

    # 2. candidacy
    if cand and root not in cand:
        v.errors.append(
            f"root_cause {root!r} is not among the RCA candidates {cand}")
        v.diagnosis_only = True
        return v

    if not actions:
        v.diagnosis_only = True
        return v

    rb_id = proposal.get("runbook_id")

    for action in actions:
        verb = (action.get("verb") or "").lower()
        target = action.get("target") or ""
        checked = set(action.get("preconditions_checked") or [])

        if not verb or not target:
            _refuse(v, action, "malformed", "action needs both verb and target")
            continue

        # Advisory verbs (investigate, notify, collect) need no runbook.
        if verb not in IRREVERSIBLE_VERBS:
            v.allowed_actions.append(action)
            continue

        # 3. runbook must exist and be current
        if not rb_id:
            _refuse(v, action, "no_runbook",
                    f"{verb} is irreversible and cites no runbook")
            continue
        if rb_id not in books:
            if rb_id in all_ids:
                _refuse(v, action, "superseded_runbook",
                        f"{rb_id} has been superseded by a newer revision")
            else:
                _refuse(v, action, "unknown_runbook", f"{rb_id} does not exist")
            continue

        rb = books[rb_id]

        # 4. authorisation
        allowed = rb.action_for(verb, target)
        if allowed is None:
            _refuse(v, action, "not_authorised",
                    f"{rb.id} does not list {verb} on {target}")
            continue

        # 5. preconditions
        missing = [p for p in allowed.required_preconditions if p not in checked]
        if missing:
            _refuse(v, action, "unmet_preconditions",
                    f"{verb} on {target} requires {missing}")
            continue

        v.allowed_actions.append(action)

    v.diagnosis_only = not v.allowed_actions
    return v
