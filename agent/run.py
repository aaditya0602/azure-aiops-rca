"""Run triage over the incidents in a span file.

    python agent/run.py --provider cassette
    python agent/run.py --provider zai --limit 3
    python agent/run.py --provider zai --record        # record cassettes

Writes one JSON record per incident plus a markdown postmortem per incident, and
prints a gate summary. With --provider cassette and no key this runs entirely
offline, provided cassettes for those exact prompts have been recorded.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import providers as prov  # noqa: E402
from agent.runbooks import load_all  # noqa: E402
from agent.triage import triage  # noqa: E402
from analyzer.features import detect_incidents, extract  # noqa: E402
from analyzer.model import Topology, read_spans  # noqa: E402
from analyzer.rca import rank_all  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="triage detected incidents")
    ap.add_argument("--spans", default="data/synth/spans.jsonl")
    ap.add_argument("--topology", default="topology/topology.yaml")
    ap.add_argument("--threshold", type=float, default=3.0)
    ap.add_argument("--bucket-ms", type=float, default=5000.0)
    ap.add_argument("--method", default="attributed",
                    help="ranker whose candidates the agent reasons over")
    ap.add_argument("--provider", default="cassette",
                    choices=["cassette", "zai", "azure_foundry", "azure"])
    ap.add_argument("--model", default=None)
    ap.add_argument("--record", action="store_true",
                    help="with --provider cassette, record missing cassettes "
                         "through the real provider in RECORD_PROVIDER")
    ap.add_argument("--limit", type=int, default=5,
                    help="triage at most this many incidents (each is an LLM call)")
    ap.add_argument("--out-dir", default="eval/results/triage")
    ap.add_argument("--require-triaged", type=int, default=None,
                    help="exit 1 unless at least N incidents triaged with no provider "
                         "error. Used by CI to verify cassette replay end to end.")
    args = ap.parse_args()

    topo = Topology.load(args.topology)
    spans = read_spans(args.spans)
    feat = extract(spans, bucket_ms=args.bucket_ms)
    incidents = detect_incidents(feat, topo.entrypoints, threshold=args.threshold)
    runbooks = load_all()

    print(f"{len(incidents)} incidents detected; triaging up to {args.limit}")
    print(f"provider={args.provider} method={args.method} "
          f"runbooks={[b.id for b in runbooks]}")

    try:
        provider = prov.build(args.provider, args.model, record=args.record)
    except prov.ProviderError as e:
        print(f"\nprovider unavailable: {e}")
        raise SystemExit(2)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    approved = refused = diagnosis = errored = 0
    records = []

    for n, inc in enumerate(incidents[:args.limit]):
        ranking = rank_all(feat, inc, topo.entrypoints)[args.method]
        try:
            res = triage(provider, feat, inc, ranking, runbooks)
        except prov.ProviderError as e:
            print(f"  incident {n}: provider error: {e}")
            errored += 1
            continue

        v = res.verdict
        approved += len(v.get("allowed_actions", []))
        refused += len(v.get("refusals", []))
        diagnosis += 1 if v.get("diagnosis_only") else 0

        (out_dir / f"incident_{n:03d}.md").write_text(res.postmortem,
                                                     encoding="utf-8")
        records.append(res.to_dict())

        root = res.proposal.get("root_cause", "?")
        flag = "diagnosis-only" if v.get("diagnosis_only") else "action approved"
        print(f"  incident {n}: candidates={ranking.top(3)} -> {root} [{flag}]")
        for r in v.get("refusals", []):
            print(f"      refused {r['action'].get('verb')} on "
                  f"{r['action'].get('target')}: {r['reason']}")
        for e in v.get("errors", []):
            print(f"      gate error: {e}")

    (out_dir / "triage.json").write_text(json.dumps(records, indent=2),
                                         encoding="utf-8")

    print(f"\ntriaged {len(records)} incidents: {approved} actions approved, "
          f"{refused} refused, {diagnosis} diagnosis-only, {errored} provider errors")
    print(f"wrote {out_dir}/")

    # Total failure must not look like success to a caller.
    if errored and not records:
        print("no incident was triaged; every provider call failed")
        raise SystemExit(1)

    if args.require_triaged is not None:
        problems = []
        if len(records) < args.require_triaged:
            problems.append(f"triaged {len(records)} < required {args.require_triaged}")
        if errored:
            problems.append(f"{errored} provider errors")
        # A grounded proposal names a service the ranker actually offered. The gate
        # enforces this too; asserting it here catches a cassette recorded against
        # a different prompt, which would otherwise replay silently.
        for n, rec in enumerate(records):
            root = (rec.get("proposal") or {}).get("root_cause")
            if root and root not in rec["candidates"]:
                problems.append(f"incident {n}: {root!r} not in candidates")
        if problems:
            for p in problems:
                print(f"FAIL: {p}")
            raise SystemExit(1)
        print(f"PASS: {len(records)} incidents triaged, no provider errors, "
              f"all proposals grounded in the ranker's candidates")


if __name__ == "__main__":
    main()
