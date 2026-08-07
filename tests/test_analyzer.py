"""Tests for the properties the evaluation depends on.

These are deliberately not smoke tests. Each one pins an invariant that, if it
broke silently, would make the reported accuracy numbers meaningless:

  - determinism, or no result is reproducible
  - graph derived from spans alone, or the analyzer is secretly reading the answer
  - error attribution, which is the single idea the headline result rests on
  - severity comparability across services, which was a real bug
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.features import extract  # noqa: E402
from analyzer.model import Topology, make_span  # noqa: E402
from analyzer.rca import rank_all  # noqa: E402
from harness import synth  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SMALL = str(REPO / "topology" / "small.yaml")
LARGE = str(REPO / "topology" / "topology.yaml")


# --- topology ---------------------------------------------------------------

def test_both_topologies_load_and_validate():
    for path in (SMALL, LARGE):
        topo = Topology.load(path)
        assert topo.entrypoints
        assert set(topo.reachable()) <= set(topo.names())


def test_cycle_is_rejected(tmp_path):
    p = tmp_path / "cyclic.yaml"
    p.write_text(
        "services:\n"
        "  a: {kind: x, calls: [b]}\n"
        "  b: {kind: x, calls: [a]}\n"
        "entrypoints: [a]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cycle"):
        Topology.load(str(p))


def test_unknown_callee_is_rejected(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text(
        "services:\n"
        "  a: {kind: x, calls: [nope]}\n"
        "entrypoints: [a]\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unknown service"):
        Topology.load(str(p))


# --- determinism ------------------------------------------------------------

def test_same_seed_produces_identical_spans(tmp_path):
    out = []
    for _ in range(2):
        d = tmp_path / f"run{len(out)}"
        synth.run(SMALL, str(d / "s.jsonl"), str(d / "l.jsonl"), seed=42,
                  duration_s=120, rps=5, n_faults=4, quiet_s=5,
                  fault_len_s=(10.0, 15.0))
        out.append((d / "s.jsonl").read_bytes())
    assert out[0] == out[1], "same seed must produce byte-identical spans"


def test_different_seed_produces_different_spans(tmp_path):
    blobs = []
    for seed in (1, 2):
        d = tmp_path / f"s{seed}"
        synth.run(SMALL, str(d / "s.jsonl"), str(d / "l.jsonl"), seed=seed,
                  duration_s=120, rps=5, n_faults=4, quiet_s=5,
                  fault_len_s=(10.0, 15.0))
        blobs.append((d / "s.jsonl").read_bytes())
    assert blobs[0] != blobs[1]


# --- graph derivation -------------------------------------------------------

def test_graph_is_derived_from_spans_not_topology(tmp_path):
    """Every derived edge must exist in the topology, and every reachable
    topology edge must be derived. The analyzer never reads topology.yaml."""
    d = tmp_path / "g"
    synth.run(SMALL, str(d / "s.jsonl"), str(d / "l.jsonl"), seed=7,
              duration_s=300, rps=8, n_faults=0, quiet_s=5,
              fault_len_s=(10.0, 15.0))
    from analyzer.model import read_spans
    feat = extract(read_spans(d / "s.jsonl"))
    topo = Topology.load(SMALL)

    expected = {(s, c) for s in topo.reachable() for c in topo.services[s].calls}
    derived = set(feat.edges.keys())
    assert derived == expected, f"derived {derived} != topology {expected}"


def test_uninstrumented_nodes_have_no_server_spans(tmp_path):
    d = tmp_path / "u"
    synth.run(SMALL, str(d / "s.jsonl"), str(d / "l.jsonl"), seed=7,
              duration_s=120, rps=8, n_faults=0, quiet_s=5,
              fault_len_s=(10.0, 15.0))
    from analyzer.model import read_spans
    spans = read_spans(d / "s.jsonl")
    servers = {s["service"] for s in spans if s["span_kind"] == "server"}
    assert "ledger" not in servers
    assert "cache" not in servers
    # ...but they are still reachable as graph nodes via client spans.
    feat = extract(spans)
    assert "ledger" in feat.services and "cache" in feat.services


# --- error attribution ------------------------------------------------------

def test_propagated_error_is_not_counted_as_own_error():
    """A parent whose ERROR span has a failed child must show err_own ~ 0 while
    err ~ 1. This is the mechanism the headline result depends on."""
    spans = []
    for i in range(40):
        t = i * 100.0
        spans.append(make_span(
            trace_id=f"t{i}", span_id=f"c{i}", parent_span_id=f"p{i}",
            service="parent", peer_service="child", span_kind="client",
            operation="call child", start_ms=t + 1, duration_ms=5.0,
            status="ERROR"))
        spans.append(make_span(
            trace_id=f"t{i}", span_id=f"p{i}", parent_span_id=None,
            service="parent", span_kind="server", operation="handle",
            start_ms=t, duration_ms=8.0, status="ERROR"))

    feat = extract(spans, bucket_ms=1000.0)
    p = feat.idx("parent")
    err = np.nanmean(feat.series["err"][p])
    err_own = np.nanmean(feat.series["err_own"][p])
    assert err > 0.9, f"parent error rate should be ~1, got {err}"
    assert err_own < 0.1, f"propagated errors must not count as own, got {err_own}"

    c = feat.idx("child")
    assert np.nanmean(feat.series["err_own"][c]) > 0.9, \
        "the failing leaf owns its errors"


# --- severity comparability -------------------------------------------------

def test_equal_fold_change_gives_equal_severity_across_scales():
    """Two services with 20x different baselines but the same fold-change must
    score the same. Ranking by per-service z-scores broke exactly this."""
    spans = []
    for i in range(60):
        t = i * 1000.0
        # fast service: 1ms baseline, 4ms during the last 10 buckets
        # slow service: 20ms baseline, 80ms during the last 10 buckets
        fast = 4.0 if i >= 50 else 1.0
        slow = 80.0 if i >= 50 else 20.0
        spans.append(make_span(
            trace_id=f"f{i}", span_id=f"fs{i}", parent_span_id=None,
            service="fast", span_kind="server", operation="h",
            start_ms=t, duration_ms=fast, status="OK"))
        spans.append(make_span(
            trace_id=f"s{i}", span_id=f"ss{i}", parent_span_id=None,
            service="slow", span_kind="server", operation="h",
            start_ms=t, duration_ms=slow, status="OK"))

    feat = extract(spans, bucket_ms=1000.0)
    f_sev = feat.mags["lat_self"][feat.idx("fast"), 55]
    s_sev = feat.mags["lat_self"][feat.idx("slow"), 55]
    assert f_sev == pytest.approx(2.0, abs=0.05), f"4x should be 2.0, got {f_sev}"
    assert s_sev == pytest.approx(2.0, abs=0.05), f"4x should be 2.0, got {s_sev}"


# --- end to end -------------------------------------------------------------

def test_attributed_localizes_a_known_deep_fault(tmp_path):
    """A latency fault on a leaf datastore must be localized to that leaf, not to
    the entrypoint that merely accumulated its latency."""
    from analyzer.features import Incident
    from analyzer.model import read_spans

    topo = Topology.load(SMALL)
    faults = [synth.Fault("latency", "ledger", 60_000.0, 120_000.0, 8.0, 0)]
    sim = synth.Simulator(topo, seed=5)
    spans: list[dict] = []
    for i in range(1600):
        t = i * 100.0
        sim.simulate("gateway", f"t{i:06x}", None, t, faults, spans)
    spans.sort(key=lambda s: s["start_ms"])
    synth.write_spans(tmp_path / "s.jsonl", spans)

    feat = extract(read_spans(tmp_path / "s.jsonl"), bucket_ms=5000.0)
    inc = Incident(b_start=feat.bucket_of(65_000.0), b_end=feat.bucket_of(115_000.0),
                   t_start_ms=65_000.0, t_end_ms=115_000.0, peak_bucket=15)
    ranked = rank_all(feat, inc, topo.entrypoints)

    for method in ("attributed", "graph"):
        assert ranked[method].top(1) == ["ledger"], \
            f"{method} picked {ranked[method].top(3)}"


def test_raw_error_rate_cannot_separate_propagated_from_originated_failure():
    """The invariant behind the error-fault result.

    When a child fails and the parent surfaces that failure, both show the SAME
    raw error rate -- so raw error rate is mathematically unable to tell which one
    is the cause; it can only tie. Attribution breaks the tie, because only the
    child's failures are unexplained by a failed call of its own.

    The size of the resulting accuracy gap is an empirical question answered by
    the eval (error faults: self_time 50%, attributed 100%), not by this test.
    """
    # The fault must be a MINORITY of the run: median/MAD treats constant failure
    # as the baseline, correctly, so a service that always fails is not anomalous.
    spans = []
    n = 400                      # 4 requests per 1000ms bucket -> 100 buckets
    fault_from = 320             # last 20 buckets are the incident
    for i in range(n):
        t = i * 250.0
        failing = i >= fault_from and (i % 2 == 0)   # 50% error rate once broken
        spans.append(make_span(
            trace_id=f"t{i}", span_id=f"c{i}", parent_span_id=f"p{i}",
            service="parent", peer_service="child", span_kind="client",
            operation="call child", start_ms=t + 1.0, duration_ms=4.0,
            status="ERROR" if failing else "OK"))
        spans.append(make_span(
            trace_id=f"t{i}", span_id=f"p{i}", parent_span_id=None,
            service="parent", span_kind="server", operation="handle",
            start_ms=t, duration_ms=6.0,
            status="ERROR" if failing else "OK"))

    feat = extract(spans, bucket_ms=1000.0)
    from analyzer.features import Incident
    b0 = feat.bucket_of(fault_from * 250.0)
    inc = Incident(b_start=b0, b_end=feat.n_buckets - 1,
                   t_start_ms=feat.bucket_start(b0),
                   t_end_ms=float(feat.n_buckets) * 1000.0, peak_bucket=b0)
    ranked = rank_all(feat, inc, ["parent"])

    p, c = feat.idx("parent"), feat.idx("child")
    raw = feat.mags["err"]
    assert raw[p].max() == pytest.approx(raw[c].max(), rel=1e-6), \
        "raw error rate must be identical for propagated vs originated failure"

    own = feat.mags["err_own"]
    assert own[c].max() > own[p].max(), "attribution must favour the true culprit"
    assert ranked["attributed"].top(1) == ["child"], \
        f"attributed picked {ranked['attributed'].top(3)}"
