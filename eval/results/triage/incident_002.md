# Incident postmortem — ledger

**Window:** 100.0s – 135.0s
**Localized to:** ledger (confidence: high)

## Evidence
- `ledger` self 46.846ms vs baseline 10.189ms (severity 2.6), own-error 0.003, 349 requests
- `recommender` self 27.496ms vs baseline 25.685ms (severity 0.22), own-error 0.0, 349 requests
- `payments` self 9.396ms vs baseline 8.935ms (severity 0.14), own-error 0.0, 350 requests
- `inventory` self 5.246ms vs baseline 5.338ms (severity 0.09), own-error 0.003, 698 requests

## Analysis
Ledger shows a 4.6x increase in self latency (46.8 ms vs 10.2 ms baseline) and the highest RCA score (2.60). Its own error rate is elevated while downstream services have normal self time, matching the latency‑degradation pattern.

## Actions
- Diagnosis only; no action proposed.

## Follow-up
Add a detection rule on `ledger` so this is caught without waiting for the entrypoint symptom.