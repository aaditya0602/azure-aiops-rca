"""Topology model and the span schema that is the contract between every producer
and the analyzer.

Both the synthetic generator (harness/synth.py) and the real instrumented services
emit this exact JSONL shape, so the analyzer never knows or cares which one it is
reading. That is what makes the fast synthetic loop trustworthy: it exercises the
same code path the real telemetry does.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# --- span schema -------------------------------------------------------------
#
# span_kind is "server" (this service handled a request) or "client" (this service
# made an outbound call). A callee's server span is a child of the caller's client
# span, matching OpenTelemetry. Nodes marked observable: false in the topology --
# Postgres, Redis -- produce no server span at all; they are visible only through
# the caller's client span, exactly as they are in real traces.

SPAN_FIELDS = (
    "trace_id",
    "span_id",
    "parent_span_id",
    "service",
    "peer_service",
    "span_kind",
    "operation",
    "start_ms",
    "duration_ms",
    "status",
)


def make_span(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    service: str,
    span_kind: str,
    operation: str,
    start_ms: float,
    duration_ms: float,
    status: str,
    peer_service: str | None = None,
) -> dict:
    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": parent_span_id,
        "service": service,
        "peer_service": peer_service,
        "span_kind": span_kind,
        "operation": operation,
        "start_ms": round(start_ms, 3),
        "duration_ms": round(duration_ms, 3),
        "status": status,
    }


def write_spans(path: str | Path, spans: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for s in spans:
            fh.write(json.dumps(s, separators=(",", ":")) + "\n")


def read_spans(path: str | Path) -> list[dict]:
    spans = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                spans.append(json.loads(line))
    return spans


# --- topology ----------------------------------------------------------------


@dataclass
class ServiceSpec:
    name: str
    kind: str
    calls: list[str] = field(default_factory=list)
    base_latency_ms: float = 5.0
    latency_sigma: float = 0.4
    observable: bool = True


@dataclass
class Topology:
    services: dict[str, ServiceSpec]
    entrypoints: list[str]
    base_error_rate: float = 0.002
    error_propagation: float = 0.85

    @classmethod
    def load(cls, path: str | Path) -> "Topology":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        services = {}
        for name, spec in raw["services"].items():
            services[name] = ServiceSpec(
                name=name,
                kind=spec.get("kind", "unknown"),
                calls=list(spec.get("calls", [])),
                base_latency_ms=float(spec.get("base_latency_ms", 5.0)),
                latency_sigma=float(spec.get("latency_sigma", 0.4)),
                observable=bool(spec.get("observable", True)),
            )
        topo = cls(
            services=services,
            entrypoints=list(raw["entrypoints"]),
            base_error_rate=float(raw.get("base_error_rate", 0.002)),
            error_propagation=float(raw.get("error_propagation", 0.85)),
        )
        topo.validate()
        return topo

    def validate(self) -> None:
        for name, spec in self.services.items():
            for callee in spec.calls:
                if callee not in self.services:
                    raise ValueError(f"{name} calls unknown service {callee!r}")
        for ep in self.entrypoints:
            if ep not in self.services:
                raise ValueError(f"unknown entrypoint {ep!r}")
        # A cycle would make the recursive simulator run forever; catch it here
        # rather than as a stack overflow three layers down.
        visiting: set[str] = set()
        done: set[str] = set()

        def walk(n: str, path: list[str]) -> None:
            if n in visiting:
                raise ValueError(f"dependency cycle: {' -> '.join(path + [n])}")
            if n in done:
                return
            visiting.add(n)
            for c in self.services[n].calls:
                walk(c, path + [n])
            visiting.discard(n)
            done.add(n)

        for ep in self.entrypoints:
            walk(ep, [])

    def names(self) -> list[str]:
        return list(self.services.keys())

    def reachable(self) -> list[str]:
        """Services reachable from an entrypoint, in stable order."""
        seen: list[str] = []

        def walk(n: str) -> None:
            if n in seen:
                return
            seen.append(n)
            for c in self.services[n].calls:
                walk(c)

        for ep in self.entrypoints:
            walk(ep)
        return seen
