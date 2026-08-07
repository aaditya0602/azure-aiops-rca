"""Drive real faults into the running stack and record the ground truth.

The labels this writes are the spine of the whole evaluation: without them there
is no accuracy number, only opinions. Timestamps are epoch milliseconds, matching
the span timestamps that come out of the collector, so labels and telemetry line
up without any clock reconciliation.

Faults are injected via each service's own /admin/fault endpoint, so the fault
lives inside the process being measured rather than in the load generator.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.model import Topology  # noqa: E402
from harness.synth import FAULT_KINDS, _descendants, _magnitude  # noqa: E402


def _admin_url(base_map: dict[str, str], service: str) -> str:
    return f"{base_map[service].rstrip('/')}/admin/fault"


def plan(topo: Topology, seed: int, n_groups: int, fault_len_s: float,
         quiet_s: float, p_concurrent: float, mag_scale: float
         ) -> list[list[dict]]:
    """Schedule fault groups. Same shape as the synthetic scenario builder so the
    real and synthetic tracks are directly comparable."""
    rng = random.Random(seed ^ 0xA10F5)
    targets = topo.reachable()
    desc = _descendants(topo)
    groups: list[list[dict]] = []
    kind_i = 0

    for _ in range(n_groups):
        picks = [targets[rng.randrange(len(targets))]]
        if rng.random() < p_concurrent:
            a = picks[0]
            cands = [b for b in targets
                     if b != a and b not in desc[a] and a not in desc[b]]
            if cands:
                picks.append(cands[rng.randrange(len(cands))])

        group = []
        for j, tgt in enumerate(picks):
            kind = FAULT_KINDS[kind_i % len(FAULT_KINDS)]
            kind_i += 1
            group.append({
                "kind": kind,
                "target": tgt,
                "magnitude": _magnitude(kind, rng, mag_scale),
                "delay_s": 0.0 if j == 0 else rng.uniform(0.05, 0.25) * fault_len_s,
            })
        groups.append(group)
    return groups


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology", default="topology/small.yaml")
    ap.add_argument("--base-url", default="http://localhost",
                    help="services are reached at BASE:PORT from --ports")
    ap.add_argument("--ports", default="",
                    help="service=url pairs, e.g. gateway=http://localhost:8080")
    ap.add_argument("--compose-net", action="store_true",
                    help="reach services by compose service name on port 8080")
    ap.add_argument("--seed", type=int, default=1337)
    ap.add_argument("--groups", type=int, default=24)
    ap.add_argument("--fault-len", type=float, default=30.0)
    ap.add_argument("--quiet", type=float, default=20.0)
    ap.add_argument("--p-concurrent", type=float, default=0.35)
    ap.add_argument("--mag-scale", type=float, default=1.0)
    ap.add_argument("--out", default="data/real/injections.jsonl")
    args = ap.parse_args()

    topo = Topology.load(args.topology)

    base_map: dict[str, str] = {}
    if args.compose_net:
        base_map = {s: f"http://{s}:8080" for s in topo.services}
    for part in args.ports.split(","):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            base_map[k.strip()] = v.strip()
    # Anything unspecified is assumed reachable on the compose network.
    for s in topo.services:
        base_map.setdefault(s, f"http://{s}:8080")

    groups = plan(topo, args.seed, args.groups, args.fault_len, args.quiet,
                  args.p_concurrent, args.mag_scale)

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    labels: list[dict] = []
    total_s = len(groups) * (args.fault_len + args.quiet)
    print(f"{len(groups)} fault groups, ~{total_s/60:.1f} min total")

    with httpx.Client(timeout=10.0) as client:
        for gi, group in enumerate(groups):
            time.sleep(args.quiet)
            started: list[tuple[dict, float]] = []

            for f in sorted(group, key=lambda x: x["delay_s"]):
                if f["delay_s"] > 0:
                    time.sleep(f["delay_s"])
                url = _admin_url(base_map, f["target"])
                body = {"kind": f["kind"], "magnitude": f["magnitude"],
                        "ttlSeconds": args.fault_len}
                try:
                    r = client.post(url, json=body)
                    r.raise_for_status()
                except Exception as e:
                    print(f"  ! failed to inject {f['kind']} on {f['target']}: {e}")
                    continue
                t_start = time.time() * 1000.0
                started.append((f, t_start))
                print(f"  [{gi+1}/{len(groups)}] {f['kind']} on {f['target']} "
                      f"mag={f['magnitude']}")

            remaining = args.fault_len - sum(f["delay_s"] for f in group)
            time.sleep(max(remaining, 1.0))

            t_end = time.time() * 1000.0
            for f, t_start in started:
                try:
                    client.delete(_admin_url(base_map, f["target"]))
                except Exception:
                    pass
                labels.append({
                    "kind": f["kind"],
                    "target": f["target"],
                    "t_start_ms": round(t_start, 1),
                    "t_end_ms": round(t_end, 1),
                    "magnitude": f["magnitude"],
                    "group": gi,
                })

            # Write incrementally so an interrupted run still yields usable labels.
            with Path(args.out).open("w", encoding="utf-8") as fh:
                for lab in labels:
                    fh.write(json.dumps(lab, separators=(",", ":")) + "\n")

    print(f"\nwrote {len(labels)} labels -> {args.out}")


if __name__ == "__main__":
    main()
