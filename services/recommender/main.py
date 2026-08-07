"""Python node in the topology, so the trace crosses a language boundary.

Same contract as the .NET node service: GET /work does its own work then calls
downstream, POST /admin/fault injects. Present specifically so the derived call
graph and the analyzer are exercised across a polyglot trace rather than a
single-runtime one.
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass

import httpx
from fastapi import FastAPI, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode
from pydantic import BaseModel

SERVICE_NAME = os.getenv("SERVICE_NAME", "recommender")
BASE_LATENCY_MS = float(os.getenv("BASE_LATENCY_MS", "20"))
LATENCY_SIGMA = float(os.getenv("LATENCY_SIGMA", "0.55"))
BASE_ERROR_RATE = float(os.getenv("BASE_ERROR_RATE", "0.002"))
ERROR_PROPAGATION = float(os.getenv("ERROR_PROPAGATION", "0.85"))

# "inventory=http://inventory:8080,catalog=http://catalog:8080"
DOWNSTREAM: list[tuple[str, str]] = []
for part in os.getenv("DOWNSTREAM", "").split(","):
    part = part.strip()
    if "=" in part:
        name, url = part.split("=", 1)
        DOWNSTREAM.append((name.strip(), url.strip().rstrip("/")))

_URL_TO_PEER = {url: name for name, url in DOWNSTREAM}

provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)


def _peer_hook(span, request):
    """Stamp the logical topology name on client spans.

    The analyzer derives the call graph from peer.service; without this it would
    only ever see host:port and could not name a node.
    """
    if span is None or not span.is_recording():
        return
    url = str(getattr(request, "url", ""))
    for base, name in _URL_TO_PEER.items():
        if url.startswith(base):
            span.set_attribute("peer.service", name)
            return


HTTPXClientInstrumentor().instrument(request_hook=_peer_hook)

app = FastAPI()
# Health and admin traffic is not part of the workload under study.
FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,admin")

_rng = random.Random(hash(SERVICE_NAME) & 0xFFFFFFFF)
_client = httpx.Client(timeout=10.0)


@dataclass
class ActiveFault:
    kind: str
    magnitude: float
    start: float
    end: float

    def ramp(self) -> float:
        if self.kind != "memleak":
            return 1.0
        total = max(self.end - self.start, 1e-6)
        return min(max((time.time() - self.start) / total, 0.0), 1.0)


_fault: ActiveFault | None = None


def _active() -> ActiveFault | None:
    global _fault
    if _fault is not None and time.time() >= _fault.end:
        _fault = None
    return _fault


class FaultRequest(BaseModel):
    kind: str
    magnitude: float
    ttlSeconds: float = 30.0


@app.get("/healthz")
def healthz():
    return {"service": SERVICE_NAME, "ok": True}


@app.get("/admin/fault")
def get_fault():
    f = _active()
    return {"active": False} if f is None else {
        "active": True, "kind": f.kind, "magnitude": f.magnitude, "endsAt": f.end}


@app.post("/admin/fault")
def set_fault(req: FaultRequest):
    global _fault
    now = time.time()
    _fault = ActiveFault(req.kind, req.magnitude, now,
                         now + (req.ttlSeconds if req.ttlSeconds > 0 else 30.0))
    return get_fault()


@app.delete("/admin/fault")
def clear_fault():
    global _fault
    _fault = None
    return {"active": False}


@app.get("/work")
def work(response: Response):
    f = _active()
    own = _rng.lognormvariate(math.log(max(BASE_LATENCY_MS, 0.01)), LATENCY_SIGMA)
    err_rate = BASE_ERROR_RATE

    if f is not None:
        r = f.ramp()
        if f.kind in ("latency", "memleak"):
            own *= 1.0 + (f.magnitude - 1.0) * r
        elif f.kind == "cpu":
            own *= 1.0 + (f.magnitude - 1.0) * r
            own *= _rng.lognormvariate(0.0, 0.6)
        elif f.kind == "dep_fail":
            # Fast failure: latency goes DOWN, which is what defeats latency-based
            # localization.
            own *= 1.0 - 0.65 * min(max(f.magnitude / 0.95, 0.0), 1.0) * r
        if f.kind in ("error", "dep_fail"):
            err_rate = max(err_rate, f.magnitude * r)

    time.sleep(max(own, 0.05) / 1000.0)

    span = trace.get_current_span()
    if _rng.random() < err_rate:
        span.set_status(Status(StatusCode.ERROR, f"{SERVICE_NAME} own failure"))
        response.status_code = 500
        return {"service": SERVICE_NAME, "error": "own"}

    for name, url in DOWNSTREAM:
        try:
            resp = _client.get(f"{url}/work")
            ok = resp.status_code < 400
        except Exception:
            ok = False
        if not ok and _rng.random() < ERROR_PROPAGATION:
            span.set_status(Status(StatusCode.ERROR, f"downstream {name} failed"))
            response.status_code = 503
            return {"service": SERVICE_NAME, "error": f"downstream:{name}"}

    return {"service": SERVICE_NAME}
