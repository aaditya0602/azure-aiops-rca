"""Turn raw spans into a dependency graph and per-service anomaly time series.

Three things here are deliberate, and each one is a place where a benchmark can
accidentally cheat if you are careless:

1. The dependency graph is DERIVED FROM THE SPANS (client spans carry
   peer_service), never read from topology.yaml. The analyzer sees telemetry and
   nothing else, which is the situation a real service is in.

2. Baselines use a median/MAD estimator over the whole run, not a "known quiet"
   period. Picking the reference window using the injection labels would leak
   ground truth into the detector. Median/MAD tolerates the minority of buckets
   containing a fault because it is robust to outliers, so no label is ever
   consulted before scoring.

3. DETECTION and RANKING use different quantities, and conflating them was a real
   bug in the first version of this file. A per-service z-score answers "is this
   service unusual for itself" -- valid for detection, invalid for comparing two
   services, because a rarely-affected service earns a huge z for a small change
   while a frequently-affected one earns a small z for a large change. Ranking
   therefore uses a *comparable* severity: log fold-change for latency (absolute
   ms is not comparable across services whose baselines differ 20x) and a
   log-scaled rate delta for errors (already dimensionless).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np

# Signals tracked per service per bucket.
#   lat_incl  inclusive latency (contains children) -- the naive view
#   lat_self  exclusive/self time (children subtracted) -- what APM localizes on
#   err       error rate over this service's spans
#   err_own   error rate EXCLUDING errors explained by a failed child call.
#             An ERROR span with a failed client child was caused downstream; an
#             ERROR span with no failed child is this service's own failure. This
#             is what separates a culprit from everyone propagating its errors.
SIGNALS = ("lat_incl", "lat_self", "err", "err_own")
LAT_SIGNALS = ("lat_incl", "lat_self")
ERR_SIGNALS = ("err", "err_own")

_LAT_FLOOR = 0.05     # ms; avoids log of ~0 on very fast operations
_MIN_OBS = 8          # buckets needed before a baseline is trustworthy


@dataclass
class Features:
    services: list[str]
    bucket_ms: float
    t0_ms: float
    n_buckets: int
    series: dict[str, np.ndarray]     # signal -> [service, bucket], NaN if no traffic
    medians: dict[str, np.ndarray]    # signal -> [service] robust baseline
    scores: dict[str, np.ndarray]     # signal -> [service, bucket] z-like, >= 0
    mags: dict[str, np.ndarray]       # signal -> [service, bucket] severity, >= 0
    counts: np.ndarray
    edges: dict[tuple[str, str], int]

    def idx(self, service: str) -> int:
        return self.services.index(service)

    def bucket_of(self, t_ms: float) -> int:
        return int((t_ms - self.t0_ms) // self.bucket_ms)

    def bucket_start(self, b: int) -> float:
        return self.t0_ms + b * self.bucket_ms

    def combined(self) -> np.ndarray:
        """Detection signal: max z-score across signals per [service, bucket].

        Max rather than mean so a large error spike is not averaged away by two
        healthy latency signals.
        """
        stack = np.stack([np.nan_to_num(self.scores[s], nan=0.0) for s in SIGNALS])
        return np.max(stack, axis=0)


def build_graph(spans: list[dict]) -> dict[tuple[str, str], int]:
    edges: dict[tuple[str, str], int] = defaultdict(int)
    for s in spans:
        if s.get("span_kind") == "client" and s.get("peer_service"):
            edges[(s["service"], s["peer_service"])] += 1
    return dict(edges)


def _robust_baseline(row: np.ndarray) -> tuple[float, float]:
    """Median and a usable sigma for one service's signal history."""
    obs = row[~np.isnan(row)]
    if obs.size < _MIN_OBS:
        return float("nan"), float("nan")
    med = float(np.median(obs))
    mad = float(np.median(np.abs(obs - med)))
    sigma = 1.4826 * mad
    if sigma <= 1e-9:
        # Zero-variance history, typical for error rate on a healthy service.
        # Derive a scale from the observed upper spread so a jump from exactly
        # zero to nonzero still registers instead of dividing by ~0.
        spread = float(np.percentile(obs, 95) - med)
        sigma = max(spread, 1e-3)
    return med, sigma


def _severity_latency(row: np.ndarray, med: float) -> np.ndarray:
    """log2 fold-change vs own baseline. 1.0 == twice as slow.

    Comparable across services in a way raw milliseconds are not.
    """
    if not np.isfinite(med):
        return np.zeros_like(row)
    base = max(med, _LAT_FLOOR)
    val = np.where(np.isnan(row), base, np.maximum(row, _LAT_FLOOR))
    return np.maximum(np.log2(val / base), 0.0)


def _severity_error(row: np.ndarray, med: float) -> np.ndarray:
    """-log2(1 - delta) on the error-rate rise. 0.5 -> 1.0, 0.75 -> 2.0.

    Puts error severity on the same log scale as latency fold-change, so the two
    can be combined with max() instead of an arbitrary weighting constant.
    """
    if not np.isfinite(med):
        return np.zeros_like(row)
    val = np.where(np.isnan(row), med, row)
    delta = np.clip(val - med, 0.0, 0.99)
    return -np.log2(1.0 - delta)


def extract(spans: list[dict], bucket_ms: float = 5000.0) -> Features:
    if not spans:
        raise ValueError("no spans")

    edges = build_graph(spans)
    services = sorted({s["service"] for s in spans} |
                      {s["peer_service"] for s in spans if s.get("peer_service")})
    sidx = {n: i for i, n in enumerate(services)}

    t0 = min(s["start_ms"] for s in spans)
    t_end = max(s["start_ms"] + s["duration_ms"] for s in spans)
    n_buckets = max(1, int((t_end - t0) // bucket_ms) + 1)

    # Services with server spans are instrumented; Postgres/Redis are not and are
    # visible only through the caller's client span, as in real traces.
    observable = {s["service"] for s in spans if s.get("span_kind") == "server"}

    # Per parent span: total client-child duration, and whether any child failed.
    child_ms: dict[str, float] = defaultdict(float)
    child_failed: dict[str, bool] = defaultdict(bool)
    for s in spans:
        if s.get("span_kind") == "client" and s.get("parent_span_id"):
            p = s["parent_span_id"]
            child_ms[p] += s["duration_ms"]
            if s["status"] == "ERROR":
                child_failed[p] = True

    n_s = len(services)
    acc = {k: np.zeros((n_s, n_buckets)) for k in ("incl", "self", "err", "err_own")}
    counts = np.zeros((n_s, n_buckets))

    for s in spans:
        kind = s.get("span_kind")
        if kind == "server":
            owner = s["service"]
        elif kind == "client" and s.get("peer_service") not in observable:
            # Client-side view of an uninstrumented dependency: attribute it to
            # the dependency, which is the node we want to be able to name.
            owner = s["peer_service"]
        else:
            continue

        b = int((s["start_ms"] - t0) // bucket_ms)
        if b < 0 or b >= n_buckets:
            continue
        i = sidx[owner]

        incl = s["duration_ms"]
        is_err = s["status"] == "ERROR"
        if kind == "server":
            self_ms = max(incl - child_ms.get(s["span_id"], 0.0), 0.0)
            explained = child_failed.get(s["span_id"], False)
        else:
            # Leaf dependency seen from the client side: no children, so any
            # failure is its own.
            self_ms = incl
            explained = False

        acc["incl"][i, b] += incl
        acc["self"][i, b] += self_ms
        acc["err"][i, b] += 1.0 if is_err else 0.0
        acc["err_own"][i, b] += 1.0 if (is_err and not explained) else 0.0
        counts[i, b] += 1.0

    empty = counts == 0
    denom = np.maximum(counts, 1.0)
    series = {
        "lat_incl": np.where(empty, np.nan, acc["incl"] / denom),
        "lat_self": np.where(empty, np.nan, acc["self"] / denom),
        "err": np.where(empty, np.nan, acc["err"] / denom),
        "err_own": np.where(empty, np.nan, acc["err_own"] / denom),
    }

    medians: dict[str, np.ndarray] = {}
    scores: dict[str, np.ndarray] = {}
    mags: dict[str, np.ndarray] = {}
    for key, mat in series.items():
        med = np.full(n_s, np.nan)
        z = np.zeros_like(mat)
        mg = np.zeros_like(mat)
        for i in range(n_s):
            m, sigma = _robust_baseline(mat[i])
            med[i] = m
            if not np.isfinite(m):
                continue
            dev = (np.where(np.isnan(mat[i]), m, mat[i]) - m) / sigma
            z[i] = np.maximum(dev, 0.0)
            mg[i] = (_severity_latency(mat[i], m) if key in LAT_SIGNALS
                     else _severity_error(mat[i], m))
        medians[key] = med
        scores[key] = z
        mags[key] = mg

    return Features(
        services=services,
        bucket_ms=bucket_ms,
        t0_ms=t0,
        n_buckets=n_buckets,
        series=series,
        medians=medians,
        scores=scores,
        mags=mags,
        counts=counts,
        edges=edges,
    )


@dataclass
class Incident:
    b_start: int
    b_end: int          # inclusive
    t_start_ms: float
    t_end_ms: float
    peak_bucket: int


def detect_incidents(feat: Features, entrypoints: list[str], threshold: float = 3.0,
                     min_buckets: int = 1, join_gap: int = 1) -> list[Incident]:
    """Contiguous runs where an entrypoint's symptoms exceed threshold.

    Detection looks only at entrypoints -- the customer-visible signal -- which is
    what a real alert fires on. Localization happens afterwards and is allowed to
    look everywhere.
    """
    comb = feat.combined()
    rows = [feat.idx(e) for e in entrypoints if e in feat.services]
    if not rows:
        raise ValueError(f"no entrypoint present in telemetry: {entrypoints}")
    symptom = np.max(comb[rows, :], axis=0)

    runs: list[list[int]] = []
    for b, is_hot in enumerate(symptom > threshold):
        if is_hot:
            if runs and b - runs[-1][-1] <= join_gap:
                runs[-1].append(b)
            else:
                runs.append([b])

    out = []
    for r in runs:
        b0, b1 = r[0], r[-1]
        if b1 - b0 + 1 < min_buckets:
            continue
        out.append(Incident(
            b_start=b0,
            b_end=b1,
            t_start_ms=feat.bucket_start(b0),
            t_end_ms=feat.bucket_start(b1) + feat.bucket_ms,
            peak_bucket=int(b0 + np.argmax(symptom[b0:b1 + 1])),
        ))
    return out
