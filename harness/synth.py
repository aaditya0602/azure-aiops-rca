"""Synthetic trace generator with propagating faults.

Purpose: exercise the analyzer against telemetry whose ground truth is known
exactly, fast enough to iterate on the RCA algorithm in seconds rather than
minutes. The real instrumented services emit the same span schema, so whatever
the analyzer scores here it scores there.

The property that makes this a real test rather than a rigged one is that fault
effects PROPAGATE UPWARD. A slow leaf makes every ancestor slow, because a
parent's inclusive duration contains its children's. So the service showing the
largest inclusive-latency anomaly is almost always the gateway, not the culprit.
Any method that just ranks by "most anomalous" gets the wrong answer, and the
analyzer has to actually reason about the call graph to walk down to the cause.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.model import Topology, make_span, write_spans  # noqa: E402

FAULT_KINDS = ("latency", "error", "cpu", "memleak", "dep_fail")


class Fault:
    """One injected fault, and the ground-truth label for it."""

    def __init__(self, kind: str, target: str, t_start_ms: float, t_end_ms: float,
                 magnitude: float, group: int = 0):
        self.kind = kind
        self.target = target
        self.t_start_ms = t_start_ms
        self.t_end_ms = t_end_ms
        self.magnitude = magnitude
        # Faults sharing a group overlap in time: one incident, several causes.
        self.group = group

    def active(self, t_ms: float) -> bool:
        return self.t_start_ms <= t_ms < self.t_end_ms

    def ramp(self, t_ms: float) -> float:
        """1.0 for abrupt faults; memleak degrades gradually across the window."""
        if self.kind != "memleak":
            return 1.0
        span = max(self.t_end_ms - self.t_start_ms, 1.0)
        return min(1.0, max(0.0, (t_ms - self.t_start_ms) / span))

    def to_label(self) -> dict:
        return {
            "kind": self.kind,
            "target": self.target,
            "t_start_ms": round(self.t_start_ms, 1),
            "t_end_ms": round(self.t_end_ms, 1),
            "magnitude": self.magnitude,
            "group": self.group,
        }


def _active_fault(faults: list[Fault], service: str, t_ms: float) -> Fault | None:
    for f in faults:
        if f.target == service and f.active(t_ms):
            return f
    return None


class Simulator:
    def __init__(self, topo: Topology, seed: int):
        self.topo = topo
        self.rng = random.Random(seed)
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"{self._counter:016x}"

    def _own_latency(self, name: str, t_ms: float, faults: list[Fault]) -> float:
        """Self time for one service: its own work, excluding downstream calls."""
        spec = self.topo.services[name]
        mu = math.log(max(spec.base_latency_ms, 0.01))
        lat = self.rng.lognormvariate(mu, spec.latency_sigma)

        f = _active_fault(faults, name, t_ms)
        if f is not None:
            r = f.ramp(t_ms)
            if f.kind in ("latency", "memleak"):
                lat *= 1.0 + (f.magnitude - 1.0) * r
            elif f.kind == "cpu":
                # CPU saturation inflates the mean and fattens the tail: queueing,
                # not a clean constant shift.
                lat *= 1.0 + (f.magnitude - 1.0) * r
                lat *= self.rng.lognormvariate(0.0, 0.6)
            elif f.kind == "dep_fail":
                # Fast failure: the service returns an error quickly, so its own
                # latency goes DOWN. This is the case that defeats every
                # latency-based localizer, including the self-time baseline.
                # Scales with severity: a barely-failing dependency barely speeds up.
                lat *= 1.0 - 0.65 * min(max(f.magnitude / 0.95, 0.0), 1.0) * r
        return max(lat, 0.05)

    def _own_error(self, name: str, t_ms: float, faults: list[Fault]) -> bool:
        p = self.topo.base_error_rate
        f = _active_fault(faults, name, t_ms)
        if f is not None:
            r = f.ramp(t_ms)
            if f.kind in ("error", "dep_fail"):
                p = max(p, f.magnitude * r)
        return self.rng.random() < p

    def simulate(self, name: str, trace_id: str, parent_span_id: str | None,
                 t_ms: float, faults: list[Fault], spans: list[dict]
                 ) -> tuple[float, str]:
        """Run one service for one request. Returns (inclusive_ms, status).

        Emits, in OTel order: this service's server span (if observable), and one
        client span per outbound call.
        """
        spec = self.topo.services[name]
        server_id = self._next_id()
        own = self._own_latency(name, t_ms, faults)
        status = "ERROR" if self._own_error(name, t_ms, faults) else "OK"

        cursor = t_ms + own
        downstream = 0.0

        # Children only get called if this service has not already failed.
        if status == "OK":
            for callee in spec.calls:
                client_id = self._next_id()
                child_ms, child_status = self.simulate(
                    callee, trace_id, client_id, cursor, faults, spans
                )
                spans.append(make_span(
                    trace_id=trace_id,
                    span_id=client_id,
                    parent_span_id=server_id,
                    service=name,
                    peer_service=callee,
                    span_kind="client",
                    operation=f"call {callee}",
                    start_ms=cursor,
                    duration_ms=child_ms,
                    status=child_status,
                ))
                cursor += child_ms
                downstream += child_ms
                if child_status == "ERROR" and \
                        self.rng.random() < self.topo.error_propagation:
                    status = "ERROR"
                    break

        inclusive = own + downstream

        if spec.observable:
            spans.append(make_span(
                trace_id=trace_id,
                span_id=server_id,
                parent_span_id=parent_span_id,
                service=name,
                span_kind="server",
                operation=f"handle {name}",
                start_ms=t_ms,
                duration_ms=inclusive,
                status=status,
            ))
        return inclusive, status


def _descendants(topo: Topology) -> dict[str, set[str]]:
    memo: dict[str, set[str]] = {}

    def walk(n: str) -> set[str]:
        if n in memo:
            return memo[n]
        memo[n] = set()          # guards against revisiting during recursion
        out: set[str] = set()
        for c in topo.services[n].calls:
            out.add(c)
            out |= walk(c)
        memo[n] = out
        return out

    for n in topo.services:
        walk(n)
    return memo


def _magnitude(kind: str, rng: random.Random, scale: float = 1.0) -> float:
    """Fault magnitude, interpolated toward "no effect" by `scale`.

    scale=1.0 is a blatant incident. Low scale is a subtle degradation, which is
    what real production faults usually look like and where localization actually
    gets hard. Sweeping this is how the eval reports a method that saturates:
    a flat 100% only means the faults were too loud.
    """
    if kind in ("latency", "cpu", "memleak"):
        hi = {"latency": (4.0, 9.0), "cpu": (3.0, 6.0), "memleak": (5.0, 10.0)}[kind]
        mag = rng.uniform(*hi)
        return round(1.0 + (mag - 1.0) * scale, 4)
    if kind == "error":
        return round(rng.uniform(0.25, 0.6) * scale, 4)
    return round(0.95 * scale, 4)          # dep_fail


def build_scenario(topo: Topology, seed: int, duration_s: float, rps: float,
                   n_faults: int, quiet_s: float,
                   fault_len_s: tuple[float, float],
                   p_concurrent: float = 0.0,
                   mag_scale: float = 1.0) -> list[Fault]:
    """Schedule fault groups separated by quiet gaps.

    A group is usually one fault. With probability p_concurrent it is two, on
    DISJOINT subtrees -- neither target reachable from the other -- so the
    incident genuinely has two independent causes rather than one cause and its
    own propagation. The second fault starts slightly later so the two have
    different onsets, which is what any temporal-precedence reasoning has to
    survive.
    """
    rng = random.Random(seed ^ 0xA10F5)
    targets = topo.reachable()
    desc = _descendants(topo)
    faults: list[Fault] = []
    t = quiet_s * 1000.0
    horizon = duration_s * 1000.0
    group = 0
    kind_i = 0

    while len(faults) < n_faults:
        length = rng.uniform(*fault_len_s) * 1000.0
        if t + length + quiet_s * 1000.0 > horizon:
            break

        picks = [targets[rng.randrange(len(targets))]]
        if rng.random() < p_concurrent and len(faults) + 2 <= n_faults:
            a = picks[0]
            cands = [b for b in targets
                     if b != a and b not in desc[a] and a not in desc[b]]
            if cands:
                picks.append(cands[rng.randrange(len(cands))])

        for j, tgt in enumerate(picks):
            kind = FAULT_KINDS[kind_i % len(FAULT_KINDS)]
            kind_i += 1
            offset = 0.0 if j == 0 else rng.uniform(0.05, 0.25) * length
            faults.append(Fault(kind, tgt, t + offset, t + length,
                                _magnitude(kind, rng, mag_scale), group))
        group += 1
        t += length + quiet_s * 1000.0

    return faults


def run(topo_path: str, out_spans: str, out_labels: str, seed: int,
        duration_s: float, rps: float, n_faults: int, quiet_s: float,
        fault_len_s: tuple[float, float],
        p_concurrent: float = 0.0,
        mag_scale: float = 1.0) -> tuple[int, int]:
    topo = Topology.load(topo_path)
    faults = build_scenario(topo, seed, duration_s, rps, n_faults, quiet_s,
                            fault_len_s, p_concurrent, mag_scale)
    sim = Simulator(topo, seed)
    spans: list[dict] = []

    n_req = int(duration_s * rps)
    arrival = random.Random(seed ^ 0x1EAF)
    for i in range(n_req):
        # Jittered arrivals so buckets are not perfectly uniform.
        t_ms = (i / rps) * 1000.0 + arrival.uniform(0.0, 1000.0 / rps)
        ep = topo.entrypoints[i % len(topo.entrypoints)]
        sim.simulate(ep, f"t{i:08x}", None, t_ms, faults, spans)

    spans.sort(key=lambda s: s["start_ms"])
    write_spans(out_spans, spans)

    Path(out_labels).parent.mkdir(parents=True, exist_ok=True)
    with Path(out_labels).open("w", encoding="utf-8") as fh:
        for f in faults:
            fh.write(json.dumps(f.to_label(), separators=(",", ":")) + "\n")

    return len(spans), len(faults)


def main() -> None:
    ap = argparse.ArgumentParser(description="generate synthetic traces + labels")
    ap.add_argument("--topology", default="topology/topology.yaml")
    ap.add_argument("--out-spans", default="data/synth/spans.jsonl")
    ap.add_argument("--out-labels", default="data/synth/injections.jsonl")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--duration", type=float, default=3600.0,
                    help="simulated seconds")
    ap.add_argument("--rps", type=float, default=8.0)
    ap.add_argument("--faults", type=int, default=60)
    ap.add_argument("--quiet", type=float, default=20.0,
                    help="quiet seconds between faults")
    ap.add_argument("--fault-min", type=float, default=25.0)
    ap.add_argument("--fault-max", type=float, default=50.0)
    ap.add_argument("--p-concurrent", type=float, default=0.0,
                    help="probability a fault group contains two overlapping faults")
    ap.add_argument("--mag-scale", type=float, default=1.0,
                    help="fault severity, 1.0=blatant, 0.2=subtle degradation")
    args = ap.parse_args()

    n_spans, n_faults = run(
        args.topology, args.out_spans, args.out_labels, args.seed,
        args.duration, args.rps, args.faults, args.quiet,
        (args.fault_min, args.fault_max), args.p_concurrent, args.mag_scale,
    )
    print(f"wrote {n_spans} spans, {n_faults} labeled injections")
    print(f"  spans  -> {args.out_spans}")
    print(f"  labels -> {args.out_labels}")


if __name__ == "__main__":
    main()
