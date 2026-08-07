"""Detection operating curve: recall vs false-positive incidents vs MTTD.

Localization accuracy is capped by detection -- an incident that never alerts can
never be localized -- so the detector's operating point is reported explicitly
rather than left as a magic constant. Reads the span file once and re-scores at
each threshold.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.eval import evaluate  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spans", default="data/synth/spans.jsonl")
    ap.add_argument("--labels", default="data/synth/injections.jsonl")
    ap.add_argument("--topology", default="topology/topology.yaml")
    ap.add_argument("--thresholds", default="1.5,2.0,2.5,3.0,3.5,5.0,7.0")
    ap.add_argument("--out", default="eval/results/detection_curve.json")
    args = ap.parse_args()

    rows = []
    print(f"{'thr':>6}{'recall':>9}{'FP':>6}{'MTTD_s':>9}"
          f"{'attributed_top1':>18}{'graph_top1':>13}")
    for thr in [float(t) for t in args.thresholds.split(",")]:
        res = evaluate(args.spans, args.labels, args.topology, threshold=thr)
        d, s = res["detection"], res["single"]["overall"]
        row = {
            "threshold": thr,
            "recall_pct": d["recall_pct"],
            "false_positive_incidents": d["false_positive_incidents"],
            "mttd_s_mean": d["mttd_s_mean"],
            "n_single": res["single"]["n"],
            "n_concurrent": res["concurrent"]["n"],
            "attributed_top1_pct": s["attributed"]["top1_pct"],
            "graph_top1_pct": s["graph"]["top1_pct"],
        }
        rows.append(row)
        print(f"{thr:>6}{str(d['recall_pct'])+'%':>9}"
              f"{d['false_positive_incidents']:>6}{str(d['mttd_s_mean']):>9}"
              f"{str(s['attributed']['top1_pct'])+'%':>18}"
              f"{str(s['graph']['top1_pct'])+'%':>13}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(
        json.dumps({"spans": args.spans, "rows": rows}, indent=2), encoding="utf-8")
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
