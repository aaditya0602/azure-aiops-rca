"""Severity sweep: accuracy as a function of how subtle the fault is.

A method reporting 100% tells you the faults were loud, not that the method is
good. This sweeps fault magnitude from blatant down to barely-visible and reports
where each method breaks, which is the honest way to characterise something that
saturates at the easy end.

Also reports detection recall per severity: a localizer cannot score an incident
that was never detected, so recall is the real ceiling on the whole pipeline.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.eval import evaluate  # noqa: E402
from analyzer.rca import RANKERS  # noqa: E402
from harness import synth  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--topology", default="topology/topology.yaml")
    ap.add_argument("--seeds", default="1337,7,99")
    ap.add_argument("--scales", default="1.0,0.6,0.4,0.25,0.15,0.08")
    ap.add_argument("--duration", type=float, default=3600.0)
    ap.add_argument("--rps", type=float, default=8.0)
    ap.add_argument("--faults", type=int, default=90)
    ap.add_argument("--quiet", type=float, default=20.0)
    ap.add_argument("--p-concurrent", type=float, default=0.35)
    ap.add_argument("--threshold", type=float, default=3.0)
    ap.add_argument("--workdir", default="data/sweep",
                    help="scratch space for span files; each is deleted after scoring")
    ap.add_argument("--keep-spans", action="store_true",
                    help="do not delete generated span files (needs ~0.5GB per run)")
    ap.add_argument("--out", default="eval/results/sweep.json")
    args = ap.parse_args()

    seeds = [int(s) for s in args.seeds.split(",")]
    scales = [float(s) for s in args.scales.split(",")]
    methods = list(RANKERS.keys())
    rows = []

    for scale in scales:
        agg = {m: {"top1": [], "top3": [], "both2": []} for m in methods}
        rec, n_single, n_conc = [], 0, 0

        for seed in seeds:
            sp = f"{args.workdir}/s{seed}_m{scale}_spans.jsonl"
            lb = f"{args.workdir}/s{seed}_m{scale}_labels.jsonl"
            synth.run(args.topology, sp, lb, seed, args.duration, args.rps,
                      args.faults, args.quiet, (25.0, 50.0),
                      args.p_concurrent, scale)
            try:
                res = evaluate(sp, lb, args.topology, threshold=args.threshold)
            finally:
                # Span files are ~0.5GB each; 18 of them would be 10GB of scratch.
                if not args.keep_spans:
                    Path(sp).unlink(missing_ok=True)
                    Path(lb).unlink(missing_ok=True)

            rec.append(res["detection"]["recall_pct"] or 0.0)
            n_single += res["single"]["n"]
            n_conc += res["concurrent"]["n"]
            for m in methods:
                s, c = res["single"]["overall"][m], res["concurrent"]["overall"][m]
                if s["top1_pct"] is not None:
                    agg[m]["top1"].append(s["top1_pct"])
                    agg[m]["top3"].append(s["top3_pct"])
                if c["both_top2_pct"] is not None:
                    agg[m]["both2"].append(c["both_top2_pct"])

        def mean(xs):
            return round(sum(xs) / len(xs), 1) if xs else None

        rows.append({
            "mag_scale": scale,
            "detect_recall_pct": mean(rec),
            "n_single": n_single,
            "n_concurrent": n_conc,
            "methods": {m: {"top1_pct": mean(agg[m]["top1"]),
                            "top3_pct": mean(agg[m]["top3"]),
                            "both_top2_pct": mean(agg[m]["both2"])}
                        for m in methods},
        })
        r = rows[-1]
        print(f"scale={scale:<5} recall={r['detect_recall_pct']:>5}%  " +
              "  ".join(f"{m}={r['methods'][m]['top1_pct']}%" for m in methods))

    out = {"config": {"seeds": seeds, "scales": scales,
                      "topology": args.topology, "rps": args.rps,
                      "duration_s": args.duration,
                      "p_concurrent": args.p_concurrent},
           "rows": rows}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")

    print(f"\n{'scale':>7}{'recall':>9}" + "".join(f"{m:>18}" for m in methods))
    for r in rows:
        line = f"{r['mag_scale']:>7}{str(r['detect_recall_pct'])+'%':>9}"
        for m in methods:
            line += f"{str(r['methods'][m]['top1_pct'])+'%':>18}"
        print(line)
    print(f"\nsingle-cause top-1, mean over seeds {seeds}")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
