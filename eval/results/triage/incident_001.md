# Incident postmortem — gateway

**Window:** 55.0s – 90.0s
**Localized to:** gateway (confidence: high)

## Evidence
- `gateway` self 4.133ms vs baseline 4.232ms (severity 0.07), own-error 0.363, 350 requests
- `payments` self 9.045ms vs baseline 8.935ms (severity 0.21), own-error 0.005, 223 requests
- `ledger` self 10.387ms vs baseline 10.189ms (severity 0.2), own-error 0.0, 223 requests
- `orders` self 6.45ms vs baseline 6.456ms (severity 0.19), own-error 0.0, 223 requests

## Analysis
gateway has the highest rca_score (0.914) and a high own_error_rate (0.363) with own_error_severity 0.91, indicating many errors originate within the service itself; its inclusive latency (45.074 ms) is also markedly elevated while self latency remains near baseline.

## Actions
- Diagnosis only; no action proposed.

## Follow-up
Add a detection rule on `gateway` so this is caught without waiting for the entrypoint symptom.