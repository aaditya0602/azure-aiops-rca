"""Incident triage: evidence -> LLM proposal -> gate -> postmortem -> detection rule.

The division of labour is the point. The analyzer decides WHERE the fault is,
using measured signals; the model only explains the evidence and proposes a
mitigation in the runbook's own terms. The model never picks the root cause from
scratch -- the gate refuses any root cause that is not already an RCA candidate.

Closing the loop: each triage emits a proposed detection rule aimed at the signal
that actually moved, so the next occurrence of the same failure is caught by a
specific rule rather than by the generic entrypoint threshold.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import numpy as np

from agent import gate as gate_mod
from agent.providers import Provider
from agent.runbooks import Runbook, for_service
from analyzer.features import Features, Incident
from analyzer.rca import Ranking

SYSTEM_PROMPT = """You are an SRE triage assistant for a distributed system.

You are given: an incident window, a ranked list of candidate root causes produced
by a telemetry analyzer, the measured signals behind that ranking, and the current
runbooks.

Rules:
- The root cause MUST be one of the ranked candidates. Do not invent a service.
- Cite a runbook by its exact id if you propose any state-changing action.
- For each action, list the runbook's required preconditions that you have
  verified. If you cannot verify one, do not propose the action.
- Prefer proposing no action over proposing an unsafe one.

Reply with JSON only, no prose outside it:
{
  "root_cause": "<service>",
  "confidence": "low|medium|high",
  "reasoning": "<2-3 sentences citing the specific signals>",
  "runbook_id": "<id or null>",
  "actions": [
    {"verb": "<verb>", "target": "<service>",
     "preconditions_checked": ["<precondition>", "..."]}
  ]
}"""


@dataclass
class TriageResult:
    incident: dict
    candidates: list[str]
    evidence: dict
    raw_response: str = ""
    proposal: dict = field(default_factory=dict)
    verdict: dict = field(default_factory=dict)
    postmortem: str = ""
    detection_rule: dict = field(default_factory=dict)
    parse_error: str | None = None

    def to_dict(self) -> dict:
        return {
            "incident": self.incident,
            "candidates": self.candidates,
            "evidence": self.evidence,
            "proposal": self.proposal,
            "verdict": self.verdict,
            "postmortem": self.postmortem,
            "detection_rule": self.detection_rule,
            "parse_error": self.parse_error,
        }


def build_evidence(feat: Features, inc: Incident, ranking: Ranking,
                   top_k: int = 4) -> dict:
    """Measured signals for the top candidates. Deliberately compact: the model
    reasons better over a small table than over a full metric dump, and every
    number it is shown is one the analyzer actually used."""
    w = slice(inc.b_start, inc.b_end + 1)
    rows = []
    for name, score in ranking.ranked[:top_k]:
        i = feat.idx(name)

        def val(key: str) -> float | None:
            v = feat.series[key][i, w]
            v = v[~np.isnan(v)]
            return round(float(v.mean()), 3) if v.size else None

        def base(key: str) -> float | None:
            m = feat.medians[key][i]
            return round(float(m), 3) if np.isfinite(m) else None

        def sev(key: str) -> float:
            return round(float(np.nan_to_num(feat.mags[key][i, w], nan=0.0).max()), 2)

        rows.append({
            "service": name,
            "rca_score": round(score, 6),
            "self_ms": val("lat_self"), "self_ms_baseline": base("lat_self"),
            "self_severity_log2": sev("lat_self"),
            "inclusive_ms": val("lat_incl"),
            "error_rate": val("err"),
            "own_error_rate": val("err_own"),
            "own_error_severity": sev("err_own"),
            "requests": int(feat.counts[i, w].sum()),
        })

    callees: dict[str, list[str]] = {}
    for (caller, callee) in feat.edges:
        callees.setdefault(caller, []).append(callee)

    return {
        "window_s": [round(inc.t_start_ms / 1000.0, 1), round(inc.t_end_ms / 1000.0, 1)],
        "signals": rows,
        "dependencies": {r["service"]: sorted(callees.get(r["service"], []))
                         for r in rows},
    }


def _extract_json(text: str) -> dict:
    """Models wrap JSON in prose or fences regardless of instructions."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start:end + 1]
    return json.loads(text)


def _detection_rule(feat: Features, inc: Incident, root: str) -> dict:
    """Rule aimed at whichever signal actually moved for this root cause.

    This is the closed-loop output: the generic entrypoint threshold found the
    incident late; a rule on the culprit's own signal finds the next one sooner.
    """
    i = feat.idx(root)
    w = slice(inc.b_start, inc.b_end + 1)
    best_key, best_sev = None, 0.0
    for key in ("lat_self", "err_own", "lat_incl"):
        s = float(np.nan_to_num(feat.mags[key][i, w], nan=0.0).max())
        if s > best_sev:
            best_key, best_sev = key, s
    if best_key is None:
        return {}

    baseline = feat.medians[best_key][i]
    if best_key in ("lat_self", "lat_incl"):
        threshold = round(float(baseline) * (2.0 ** (best_sev * 0.6)), 3)
        unit, comparison = "ms", "mean over 5m window"
    else:
        threshold = round(min(float(baseline) + 0.5 * (2.0 ** -best_sev), 0.95), 4)
        unit, comparison = "rate", "mean over 5m window"

    return {
        "service": root,
        "signal": best_key,
        "baseline": round(float(baseline), 4),
        "threshold": threshold,
        "unit": unit,
        "comparison": comparison,
        "observed_severity_log2": round(best_sev, 2),
        "rationale": (f"{root} {best_key} reached severity {best_sev:.2f} (log2 "
                      f"scale) during this incident; alerting on {best_key} "
                      f"directly detects it without waiting for the entrypoint "
                      f"symptom to cross its threshold."),
    }


def _postmortem(inc: Incident, evidence: dict, proposal: dict,
                verdict: dict) -> str:
    root = proposal.get("root_cause", "unknown")
    rows = evidence.get("signals", [])
    top = rows[0] if rows else {}
    allowed = verdict.get("allowed_actions", [])
    refused = verdict.get("refusals", [])

    lines = [
        f"# Incident postmortem — {root}",
        "",
        f"**Window:** {evidence['window_s'][0]}s – {evidence['window_s'][1]}s",
        f"**Localized to:** {root} "
        f"(confidence: {proposal.get('confidence', 'unknown')})",
        "",
        "## Evidence",
    ]
    for r in rows:
        lines.append(
            f"- `{r['service']}` self {r['self_ms']}ms vs baseline "
            f"{r['self_ms_baseline']}ms (severity {r['self_severity_log2']}), "
            f"own-error {r['own_error_rate']}, {r['requests']} requests")
    lines += ["", "## Analysis", proposal.get("reasoning", "_none supplied_"), ""]

    lines.append("## Actions")
    if allowed:
        for a in allowed:
            lines.append(f"- APPROVED `{a.get('verb')}` on `{a.get('target')}` "
                         f"(preconditions: "
                         f"{', '.join(a.get('preconditions_checked') or []) or 'none required'})")
    if refused:
        for r in refused:
            act = r.get("action", {})
            lines.append(f"- REFUSED `{act.get('verb')}` on `{act.get('target')}` "
                         f"— {r.get('reason')}: {r.get('detail')}")
    if not allowed and not refused:
        lines.append("- Diagnosis only; no action proposed.")
    if verdict.get("errors"):
        lines += ["", "## Gate errors"] + [f"- {e}" for e in verdict["errors"]]

    if top:
        lines += ["", "## Follow-up",
                  f"Add a detection rule on `{root}` so this is caught without "
                  f"waiting for the entrypoint symptom."]
    return "\n".join(lines)


def triage(provider: Provider, feat: Features, inc: Incident, ranking: Ranking,
           runbooks: list[Runbook], top_k: int = 4) -> TriageResult:
    evidence = build_evidence(feat, inc, ranking, top_k=top_k)
    candidates = ranking.top(top_k)

    # Retrieve runbooks for the candidates only. Current revisions only -- a
    # superseded runbook is never offered, and the gate refuses one if cited.
    relevant: list[Runbook] = []
    for c in candidates:
        relevant.extend(for_service(runbooks, c))
    if not relevant:
        relevant = runbooks

    def _rb_block(b: Runbook) -> str:
        allowed = [{"verb": a.verb, "target": a.target,
                    "required_preconditions": a.required_preconditions}
                   for a in b.allowed_actions]
        return (f"--- runbook {b.id} (service: {b.service}) ---\n"
                f"allowed_actions: {json.dumps(allowed)}\n"
                f"{b.body[:1200]}")

    rb_text = "\n\n".join(_rb_block(b) for b in relevant)

    user = (
        f"Ranked candidates (most likely first): {json.dumps(candidates)}\n\n"
        f"Measured evidence:\n{json.dumps(evidence, indent=2)}\n\n"
        f"Runbooks:\n{rb_text}\n"
    )

    result = TriageResult(
        incident={"b_start": inc.b_start, "b_end": inc.b_end,
                  "t_start_ms": inc.t_start_ms, "t_end_ms": inc.t_end_ms},
        candidates=candidates,
        evidence=evidence,
    )

    result.raw_response = provider.complete(SYSTEM_PROMPT, user)
    try:
        result.proposal = _extract_json(result.raw_response)
    except (json.JSONDecodeError, ValueError) as e:
        result.parse_error = f"could not parse JSON from model output: {e}"
        result.verdict = {"allowed_actions": [], "refusals": [],
                          "diagnosis_only": True,
                          "errors": [result.parse_error]}
        result.postmortem = _postmortem(inc, evidence, {}, result.verdict)
        return result

    verdict = gate_mod.review(
        result.proposal,
        candidates=candidates,
        known_services=feat.services,
        runbooks=runbooks,
    )
    result.verdict = verdict.to_dict()

    root = result.proposal.get("root_cause")
    if root in feat.services:
        result.detection_rule = _detection_rule(feat, inc, root)

    result.postmortem = _postmortem(inc, evidence, result.proposal, result.verdict)
    return result
