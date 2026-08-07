# Incident postmortem — gateway

**Window:** 150.0s – 180.0s
**Localized to:** gateway (confidence: high)

## Evidence
- `gateway` self 20.885ms vs baseline 4.232ms (severity 2.91), own-error 0.0, 300 requests
- `recommender` self 26.612ms vs baseline 25.685ms (severity 0.24), own-error 0.0, 296 requests
- `inventory` self 5.316ms vs baseline 5.338ms (severity 0.07), own-error 0.003, 595 requests
- `ledger` self 10.021ms vs baseline 10.189ms (severity 0.07), own-error 0.003, 300 requests

## Analysis
Gateway shows the highest RCA score (2.91) and a self latency increase from 4.232 ms to 20.885 ms, plus elevated inclusive latency (85.39 ms) and error rate (0.017). Other services have low scores and near‑baseline latencies.

## Actions
- Diagnosis only; no action proposed.

## Follow-up
Add a detection rule on `gateway` so this is caught without waiting for the entrypoint symptom.