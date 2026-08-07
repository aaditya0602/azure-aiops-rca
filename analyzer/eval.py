"""Score detection and localization against the injected ground truth.

Structure of the report:

  detection     recall over injected fault groups, false-positive incidents, MTTD.
  single        groups with ONE cause: top-1 and top-3, overall and per fault kind.
                This is the headline metric and the one comparable to the RCA
                literature.
  concurrent    groups with TWO simultaneous causes on disjoint subtrees: whether
                BOTH true causes appear in top-2 and in top-3. Strictly harder --
                a method that always answers with one strong candidate scores 0.

The per-kind and per-group-size breakdowns are the point. An aggregate number
hides which faults a method actually handles, and the interesting question is not
"which method is best" but "which idea buys which capability".
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.features import detect_incidents, extract  # noqa: E402
from analyzer.model import Topology, read_spans  # noqa: E402
from analyzer.rca import RANKERS, rank_all  # noqa: E402


def read_labels(path: str | Path) -> list[dict]:
    out = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _pct(xs: list[float]) -> float | None:
    return round(100.0 * sum(xs) / len(xs), 1) if xs else None


def evaluate(spans_path: str, labels_path: str, topology_path: str,
             bucket_ms: float = 5000.0, threshold: float = 3.0) -> dict:
    spans = read_spans(spans_path)
    labels = read_labels(labels_path)
    topo = Topology.load(topology_path)

    feat = extract(spans, bucket_ms=bucket_ms)
    incidents = detect_incidents(feat, topo.entrypoints, threshold=threshold)

    groups: dict[int, list[dict]] = defaultdict(list)
    for i, lab in enumerate(labels):
        groups[lab.get("group", i)].append(lab)

    # Match each fault group to the first unclaimed incident overlapping it.
    matched: list[tuple[list[dict], object]] = []
    used: set[int] = set()
    for _gid, labs in sorted(groups.items()):
        g0 = min(l["t_start_ms"] for l in labs)
        g1 = max(l["t_end_ms"] for l in labs)
        for k, inc in enumerate(incidents):
            if k in used:
                continue
            if inc.t_end_ms > g0 and inc.t_start_ms < g1:
                matched.append((labs, inc))
                used.add(k)
                break

    false_positives = len(incidents) - len(used)
    mttd = [max(0.0, inc.t_start_ms - min(l["t_start_ms"] for l in labs)) / 1000.0
            for labs, inc in matched]

    single = {m: {"top1": [], "top3": []} for m in RANKERS}
    concur = {m: {"both_top2": [], "both_top3": []} for m in RANKERS}
    per_kind: dict[str, dict[str, dict[str, list[float]]]] = {}
    cases = []

    for labs, inc in matched:
        rankings = rank_all(feat, inc, topo.entrypoints)
        truths = {l["target"] for l in labs}
        k = len(truths)
        case = {
            "group_size": k,
            "kinds": [l["kind"] for l in labs],
            "truth": sorted(truths),
            "detect_delay_s": round(
                max(0.0, inc.t_start_ms - min(l["t_start_ms"] for l in labs)) / 1000.0, 1),
            "methods": {},
        }

        for name, r in rankings.items():
            if k == 1:
                truth = next(iter(truths))
                t1 = float(truth in r.top(1))
                t3 = float(truth in r.top(3))
                single[name]["top1"].append(t1)
                single[name]["top3"].append(t3)
                kind = labs[0]["kind"]
                per_kind.setdefault(
                    kind, {m: {"top1": [], "top3": []} for m in RANKERS})
                per_kind[kind][name]["top1"].append(t1)
                per_kind[kind][name]["top3"].append(t3)
                case["methods"][name] = {
                    "top1": t1, "top3": t3, "ranked": r.top(3)}
            else:
                b2 = float(truths <= set(r.top(2)))
                b3 = float(truths <= set(r.top(3)))
                concur[name]["both_top2"].append(b2)
                concur[name]["both_top3"].append(b3)
                case["methods"][name] = {
                    "both_top2": b2, "both_top3": b3, "ranked": r.top(3)}
        cases.append(case)

    n_single = len(single["graph"]["top1"])
    n_concur = len(concur["graph"]["both_top2"])

    return {
        "config": {
            "spans": spans_path,
            "labels": labels_path,
            "topology": topology_path,
            "bucket_ms": bucket_ms,
            "detect_threshold": threshold,
            "n_spans": len(spans),
            "n_services_observed": len(feat.services),
            "n_edges_derived": len(feat.edges),
        },
        "detection": {
            "n_groups_injected": len(groups),
            "n_groups_detected": len(matched),
            "recall_pct": _pct([1.0] * len(matched)
                               + [0.0] * (len(groups) - len(matched))),
            "false_positive_incidents": false_positives,
            "mttd_s_mean": round(sum(mttd) / len(mttd), 1) if mttd else None,
            "mttd_s_p95": (round(sorted(mttd)[int(0.95 * (len(mttd) - 1))], 1)
                           if mttd else None),
        },
        "single": {
            "n": n_single,
            "overall": {m: {"top1_pct": _pct(single[m]["top1"]),
                            "top3_pct": _pct(single[m]["top3"])}
                        for m in RANKERS},
            "by_kind": {
                kind: {"n": len(v["graph"]["top1"]),
                       **{m: {"top1_pct": _pct(v[m]["top1"]),
                              "top3_pct": _pct(v[m]["top3"])} for m in RANKERS}}
                for kind, v in sorted(per_kind.items())
            },
        },
        "concurrent": {
            "n": n_concur,
            "overall": {m: {"both_top2_pct": _pct(concur[m]["both_top2"]),
                            "both_top3_pct": _pct(concur[m]["both_top3"])}
                        for m in RANKERS},
        },
        "cases": cases,
    }


def print_report(res: dict) -> None:
    c, d = res["config"], res["detection"]
    print(f"\ntopology={c['topology']}")
    print(f"spans={c['n_spans']}  services_observed={c['n_services_observed']}  "
          f"edges_derived={c['n_edges_derived']}  bucket={c['bucket_ms']:.0f}ms")
    print(f"detection: {d['n_groups_detected']}/{d['n_groups_injected']} fault groups "
          f"(recall {d['recall_pct']}%), false-positive incidents="
          f"{d['false_positive_incidents']}, MTTD mean={d['mttd_s_mean']}s "
          f"p95={d['mttd_s_p95']}s")

    methods = list(res["single"]["overall"].keys())

    print(f"\nSINGLE-CAUSE incidents  (n={res['single']['n']})")
    print(f"  {'method':<18}{'top-1':>9}{'top-3':>9}")
    for m in methods:
        v = res["single"]["overall"][m]
        print(f"  {m:<18}{str(v['top1_pct'])+'%':>9}{str(v['top3_pct'])+'%':>9}")

    print("\n  by fault kind (top-1 %)")
    print(f"    {'kind':<12}{'n':>4}" + "".join(f"{m:>18}" for m in methods))
    for kind, v in res["single"]["by_kind"].items():
        row = f"    {kind:<12}{v['n']:>4}"
        for m in methods:
            row += f"{str(v[m]['top1_pct'])+'%':>18}"
        print(row)

    if res["concurrent"]["n"]:
        print(f"\nCONCURRENT incidents, 2 causes  (n={res['concurrent']['n']})")
        print(f"  {'method':<18}{'both@2':>9}{'both@3':>9}")
        for m in methods:
            v = res["concurrent"]["overall"][m]
            print(f"  {m:<18}{str(v['both_top2_pct'])+'%':>9}"
                  f"{str(v['both_top3_pct'])+'%':>9}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="score RCA against injected labels")
    ap.add_argument("--spans", default="data/synth/spans.jsonl")
    ap.add_argument("--labels", default="data/synth/injections.jsonl")
    ap.add_argument("--topology", default="topology/topology.yaml")
    ap.add_argument("--bucket-ms", type=float, default=5000.0)
    ap.add_argument("--threshold", type=float, default=3.0,
                    help="detector operating point; see eval/results/detection_curve.json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--gate-method", default="attributed",
                    help="method the CI gate asserts on (default: the shipped one)")
    ap.add_argument("--assert-top1", type=float, default=None,
                    help="exit 1 if the gate method's single-cause top-1 is below this pct")
    ap.add_argument("--assert-beats", default=None, metavar="METHOD:MARGIN",
                    help="exit 1 unless gate method beats METHOD by MARGIN points, "
                         "e.g. self_time:15. Catches silent loss of error attribution, "
                         "which would collapse the shipped method onto the baseline.")
    args = ap.parse_args()

    res = evaluate(args.spans, args.labels, args.topology,
                   bucket_ms=args.bucket_ms, threshold=args.threshold)
    print_report(res)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")

    overall = res["single"]["overall"]
    if args.gate_method not in overall:
        print(f"FAIL: unknown --gate-method {args.gate_method!r}; "
              f"choose from {list(overall)}")
        raise SystemExit(2)
    gated = overall[args.gate_method]["top1_pct"] or 0.0
    failures: list[str] = []

    if args.assert_top1 is not None:
        if gated < args.assert_top1:
            failures.append(f"{args.gate_method} top-1 {gated}% "
                            f"< required {args.assert_top1}%")
        else:
            print(f"PASS: {args.gate_method} top-1 {gated}% >= {args.assert_top1}%")

    if args.assert_beats:
        try:
            other, margin_s = args.assert_beats.split(":", 1)
            margin = float(margin_s)
        except ValueError:
            print(f"FAIL: --assert-beats wants METHOD:MARGIN, got {args.assert_beats!r}")
            raise SystemExit(2)
        if other not in overall:
            print(f"FAIL: unknown baseline {other!r}; choose from {list(overall)}")
            raise SystemExit(2)
        base = overall[other]["top1_pct"] or 0.0
        if gated - base < margin:
            failures.append(f"{args.gate_method} ({gated}%) does not beat {other} "
                            f"({base}%) by {margin} points")
        else:
            print(f"PASS: {args.gate_method} {gated}% beats {other} {base}% "
                  f"by {gated - base:.1f} >= {margin} points")

    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
