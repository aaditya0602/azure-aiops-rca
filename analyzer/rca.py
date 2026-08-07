"""Root-cause localization, and the ablation ladder it has to climb.

Four rankers, identical input (derived graph + severity series for one incident
window). Each step changes exactly ONE thing from the step above it, so the eval
attributes the win to a specific idea rather than to "the whole pipeline":

  naive_inclusive  severity of INCLUSIVE latency + raw error rate.
                   The strawman. A parent's duration contains its children's, so
                   the largest inclusive anomaly is usually the entrypoint.

  self_time        severity of SELF (exclusive) time + raw error rate.
                   Changes one thing: inclusive -> exclusive latency. This is the
                   strong baseline -- how commercial APM localizes latency, and
                   the first thing a competent engineer reaches for.

  attributed       severity of self time + OWN error rate.
                   Changes one thing: raw error rate -> error rate excluding
                   failures explained by a failed child call. Needs parent/child
                   span structure, so it is a graph-derived FEATURE, but there is
                   no random walk here. Isolating it matters: without this rung
                   you cannot tell whether a graph method wins because of the
                   walk or merely because of better features.

  graph            attributed severity re-weighted by a random walk with restart
                   over the derived call graph, seeded at the symptomatic
                   entrypoint and biased toward anomalous neighbours, then scaled
                   by two causal priors:
                     frontier  -- an anomalous node whose own dependencies are
                                  all healthy is where blame stops propagating.
                     precedence -- a cause goes anomalous no later than its effects.

Severity is comparable across services by construction (see features.py): log2
fold-change for latency, -log2(1-delta) for error rates. Per-service z-scores are
NOT used for ranking, only for detection -- comparing two services by their
individual z-scores was a real bug here and it made all methods fail identically
on error faults.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analyzer.features import Features, Incident

EPS = 0.05          # keeps the walk ergodic across healthy nodes
RHO = 0.35          # willingness to walk back up toward callers
TAU = 0.45          # self-loop weight; retains mass on anomalous nodes
DAMPING = 0.85
BETA_FRONTIER = 1.6
GAMMA_EARLY = 0.5
ANOM_THR = 1.0      # severity units (log2): 1.0 == 2x slower, or ~50% errors


@dataclass
class Ranking:
    method: str
    ranked: list[tuple[str, float]]

    def top(self, k: int) -> list[str]:
        return [n for n, _ in self.ranked[:k]]


def _severity(feat: Features, inc: Incident, lat_signal: str, err_signal: str
              ) -> np.ndarray:
    """Peak comparable severity per service inside the incident window."""
    w = slice(inc.b_start, inc.b_end + 1)
    lat = np.nan_to_num(feat.mags[lat_signal][:, w], nan=0.0).max(axis=1)
    err = np.nan_to_num(feat.mags[err_signal][:, w], nan=0.0).max(axis=1)
    return np.maximum(lat, err)


def _ranked(services: list[str], score: np.ndarray, method: str) -> Ranking:
    order = np.argsort(-score)
    return Ranking(method, [(services[i], float(score[i])) for i in order])


def rank_naive_inclusive(feat: Features, inc: Incident, **_) -> Ranking:
    return _ranked(feat.services,
                   _severity(feat, inc, "lat_incl", "err"),
                   "naive_inclusive")


def rank_self_time(feat: Features, inc: Incident, **_) -> Ranking:
    return _ranked(feat.services,
                   _severity(feat, inc, "lat_self", "err"),
                   "self_time")


def rank_attributed(feat: Features, inc: Incident, **_) -> Ranking:
    return _ranked(feat.services,
                   _severity(feat, inc, "lat_self", "err_own"),
                   "attributed")


def _onset(feat: Features, inc: Incident, sev_thr: float, lookback: int = 2
           ) -> np.ndarray:
    """First bucket at/just before the incident where each service went anomalous.

    Services that never cross the threshold get the window end, so they earn no
    earliness bonus.
    """
    w_lat = np.nan_to_num(feat.mags["lat_self"], nan=0.0)
    w_err = np.nan_to_num(feat.mags["err_own"], nan=0.0)
    sev = np.maximum(w_lat, w_err)
    b0 = max(0, inc.b_start - lookback)
    b1 = inc.b_end
    onset = np.full(sev.shape[0], float(b1))
    for i in range(sev.shape[0]):
        hot = np.nonzero(sev[i, b0:b1 + 1] > sev_thr)[0]
        if hot.size:
            onset[i] = float(b0 + hot[0])
    return onset


def rank_graph(feat: Features, inc: Incident, entrypoints: list[str], **_) -> Ranking:
    services = feat.services
    n = len(services)
    sidx = {s: i for i, s in enumerate(services)}
    sev = _severity(feat, inc, "lat_self", "err_own")

    # --- random walk over the DERIVED graph ---------------------------------
    M = np.zeros((n, n))
    for (caller, callee), w in feat.edges.items():
        if caller not in sidx or callee not in sidx:
            continue
        i, j = sidx[caller], sidx[callee]
        # Down toward dependencies, preferring anomalous ones: the "where did the
        # badness come from" direction.
        M[i, j] += w * (EPS + sev[j])
        # Some mass walks back up so a descent into a healthy subtree can escape.
        M[j, i] += w * (EPS + sev[i]) * RHO

    for i in range(n):
        M[i, i] += (EPS + sev[i]) * TAU

    row = M.sum(axis=1, keepdims=True)
    row[row == 0] = 1.0
    M = M / row

    e = np.zeros(n)
    for ep in entrypoints:
        if ep in sidx:
            e[sidx[ep]] = 1.0
    e = e / e.sum() if e.sum() else np.full(n, 1.0 / n)

    p = e.copy()
    for _ in range(200):
        nxt = (1.0 - DAMPING) * e + DAMPING * (M.T @ p)
        s = nxt.sum()
        if s > 0:
            nxt /= s
        if np.max(np.abs(nxt - p)) < 1e-12:
            p = nxt
            break
        p = nxt

    # --- causal priors ------------------------------------------------------
    callees: dict[int, list[int]] = {i: [] for i in range(n)}
    for (caller, callee), _w in feat.edges.items():
        if caller in sidx and callee in sidx:
            callees[sidx[caller]].append(sidx[callee])

    frontier = np.array([
        1.0 if sev[i] > ANOM_THR and not any(sev[j] > ANOM_THR for j in callees[i])
        else 0.0
        for i in range(n)
    ])

    onset = _onset(feat, inc, ANOM_THR)
    span = max(float(onset.max() - onset.min()), 1.0)
    earliness = 1.0 - (onset - onset.min()) / span

    # The walk supplies structural plausibility, severity supplies magnitude, and
    # the priors supply causal direction. Multiplying keeps a structurally likely
    # but perfectly healthy node from ever winning.
    score = (p * sev
             * (1.0 + BETA_FRONTIER * frontier)
             * (1.0 + GAMMA_EARLY * earliness))
    return _ranked(services, score, "graph")


RANKERS = {
    "naive_inclusive": rank_naive_inclusive,
    "self_time": rank_self_time,
    "attributed": rank_attributed,
    "graph": rank_graph,
}


def rank_all(feat: Features, inc: Incident, entrypoints: list[str]
             ) -> dict[str, Ranking]:
    return {name: fn(feat, inc, entrypoints=entrypoints)
            for name, fn in RANKERS.items()}
