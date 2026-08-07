"""Convert OTel Collector file-exporter output into the analyzer's span schema.

This is the seam that lets the analyzer be validated on fast synthetic data and
then run unchanged on real telemetry: both sides produce the same JSONL. The
collector writes OTLP JSON, which is a faithful record of what the services
actually emitted (and, in the Azure pipeline, of what Application Insights
received), so nothing bespoke sits between the services and the analyzer.

OTLP JSON encodes trace/span ids as hex per the spec, but protobuf's canonical
JSON mapping for `bytes` is base64, and different collector versions have shipped
both. Both are accepted here rather than assuming one.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyzer.model import make_span, write_spans  # noqa: E402

# OTLP span kind enum -> our label. Anything else is dropped: the analyzer models
# service-to-service calls, and internal/producer/consumer spans are not that.
_KIND = {
    2: "server", "SPAN_KIND_SERVER": "server",
    3: "client", "SPAN_KIND_CLIENT": "client",
}


def _hexid(v: str | None) -> str | None:
    if not v:
        return None
    s = str(v)
    try:
        int(s, 16)
        return s.lower()
    except ValueError:
        pass
    try:
        return base64.b64decode(s + "=" * (-len(s) % 4)).hex()
    except Exception:
        return s


def _attrs(items: list[dict] | None) -> dict:
    out = {}
    for a in items or []:
        v = a.get("value", {})
        out[a.get("key")] = (
            v.get("stringValue")
            or v.get("intValue")
            or v.get("boolValue")
            or v.get("doubleValue")
        )
    return out


def convert(in_path: str | Path) -> list[dict]:
    spans: list[dict] = []
    with Path(in_path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue

            for rs in payload.get("resourceSpans", []):
                res = _attrs(rs.get("resource", {}).get("attributes"))
                service = res.get("service.name") or "unknown"

                for ss in rs.get("scopeSpans", []):
                    for sp in ss.get("spans", []):
                        kind = _KIND.get(sp.get("kind"))
                        if kind is None:
                            continue

                        start_ns = int(sp.get("startTimeUnixNano", 0))
                        end_ns = int(sp.get("endTimeUnixNano", 0))
                        if start_ns <= 0 or end_ns < start_ns:
                            continue

                        a = _attrs(sp.get("attributes"))
                        code = (sp.get("status") or {}).get("code")
                        is_err = code in (2, "STATUS_CODE_ERROR")
                        # A 5xx recorded by instrumentation without an explicit
                        # error status still means the call failed.
                        http_status = a.get("http.response.status_code") or \
                            a.get("http.status_code")
                        if not is_err and http_status:
                            try:
                                is_err = int(http_status) >= 500
                            except (TypeError, ValueError):
                                pass

                        peer = a.get("peer.service")
                        spans.append(make_span(
                            trace_id=_hexid(sp.get("traceId")) or "",
                            span_id=_hexid(sp.get("spanId")) or "",
                            parent_span_id=_hexid(sp.get("parentSpanId")),
                            service=service,
                            span_kind=kind,
                            operation=sp.get("name") or "",
                            start_ms=start_ns / 1e6,
                            duration_ms=(end_ns - start_ns) / 1e6,
                            status="ERROR" if is_err else "OK",
                            peer_service=peer if kind == "client" else None,
                        ))

    spans.sort(key=lambda s: s["start_ms"])
    return spans


def main() -> None:
    ap = argparse.ArgumentParser(description="OTLP JSON -> analyzer span JSONL")
    ap.add_argument("--in", dest="inp", default="data/otlp/otlp-spans.jsonl")
    ap.add_argument("--out", default="data/real/spans.jsonl")
    args = ap.parse_args()

    spans = convert(args.inp)
    write_spans(args.out, spans)

    kinds: dict[str, int] = {}
    services: dict[str, int] = {}
    peers = 0
    for s in spans:
        kinds[s["span_kind"]] = kinds.get(s["span_kind"], 0) + 1
        services[s["service"]] = services.get(s["service"], 0) + 1
        if s.get("peer_service"):
            peers += 1

    print(f"converted {len(spans)} spans -> {args.out}")
    print(f"  kinds: {kinds}")
    print(f"  client spans carrying peer.service: {peers}")
    print(f"  services: {dict(sorted(services.items()))}")
    if not peers:
        print("  WARNING: no peer.service attributes found; the call graph will be "
              "empty. Check the client-span enrichment in the services.")


if __name__ == "__main__":
    main()
