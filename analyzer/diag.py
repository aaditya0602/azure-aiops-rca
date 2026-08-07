"""Diagnostic: dump raw signals and anomaly scores around individual injections.

Not part of the pipeline. Exists to answer "why did this case fail" with evidence
instead of guesswork.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.eval import read_labels  # noqa: E402
from analyzer.features import detect_incidents, extract  # noqa: E402
from analyzer.model import Topology, read_spans  # noqa: E402
from analyzer.rca import rank_all  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spans", default="data/synth/spans.jsonl")
    ap.add_argument("--labels", default="data/synth/injections.jsonl")
    ap.add_argument("--topology", default="topology/topology.yaml")
    ap.add_argument("--kind", default=None, help="only this fault kind")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--bucket-ms", type=float, default=5000.0)
    ap.add_argument("--threshold", type=float, default=3.5)
    args = ap.parse_args()

    spans = read_spans(args.spans)
    labels = read_labels(args.labels)
    topo = Topology.load(args.topology)
    feat = extract(spans, bucket_ms=args.bucket_ms)
    incidents = detect_incidents(feat, topo.entrypoints, threshold=args.threshold)

    print(f"services: {feat.services}")
    print(f"edges: {sorted(feat.edges.items())}")
    print(f"incidents detected: {len(incidents)}  labels: {len(labels)}")

    shown = 0
    for lab in labels:
        if args.kind and lab["kind"] != args.kind:
            continue
        inc = next((i for i in incidents
                    if i.t_end_ms > lab["t_start_ms"] and i.t_start_ms < lab["t_end_ms"]),
                   None)
        print("\n" + "=" * 78)
        print(f"FAULT {lab['kind']} on {lab['target']} mag={lab['magnitude']} "
              f"window=[{lab['t_start_ms']/1000:.0f}s,{lab['t_end_ms']/1000:.0f}s]")
        if inc is None:
            print("  NOT DETECTED")
            b0 = feat.bucket_of(lab["t_start_ms"])
            b1 = feat.bucket_of(lab["t_end_ms"])
            comb = np.nan_to_num(feat.combined(), nan=0.0)
            print(f"  peak combined score in window, per service:")
            for i, s in enumerate(feat.services):
                print(f"    {s:<14}{comb[i, b0:b1+1].max():8.2f}")
            shown += 1
            if shown >= args.limit:
                break
            continue

        b0, b1 = inc.b_start, inc.b_end
        print(f"  detected buckets [{b0},{b1}]  "
              f"t=[{inc.t_start_ms/1000:.0f}s,{inc.t_end_ms/1000:.0f}s]")
        print(f"  {'service':<14}{'lat_incl':>10}{'lat_self':>10}{'err':>10}"
              f"{'a_incl':>9}{'a_self':>9}{'a_err':>9}{'count':>8}")
        for i, s in enumerate(feat.services):
            w = slice(b0, b1 + 1)
            def m(key):
                v = feat.series[key][i, w]
                v = v[~np.isnan(v)]
                return float(v.mean()) if v.size else float("nan")
            def a(key):
                return float(np.nan_to_num(feat.scores[key][i, w], nan=0.0).max())
            print(f"  {s:<14}{m('lat_incl'):10.2f}{m('lat_self'):10.2f}{m('err'):10.3f}"
                  f"{a('lat_incl'):9.2f}{a('lat_self'):9.2f}{a('err'):9.2f}"
                  f"{feat.counts[i, w].sum():8.0f}")

        rankings = rank_all(feat, inc, topo.entrypoints)
        for name, r in rankings.items():
            marks = " ".join(
                f"{n}{'*' if n == lab['target'] else ''}:{v:.4g}"
                for n, v in r.ranked[:4]
            )
            print(f"  {name:<18}{marks}")

        shown += 1
        if shown >= args.limit:
            break


if __name__ == "__main__":
    main()
