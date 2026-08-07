# Incident postmortem — recommender

**Window:** 15.0s – 45.0s
**Localized to:** recommender (confidence: high)

## Evidence
- `recommender` self 162.358ms vs baseline 25.685ms (severity 2.88), own-error 0.003, 299 requests
- `orders` self 6.774ms vs baseline 6.456ms (severity 0.15), own-error 0.0, 300 requests
- `ledger` self 10.002ms vs baseline 10.189ms (severity 0.11), own-error 0.0, 299 requests
- `payments` self 8.821ms vs baseline 8.935ms (severity 0.08), own-error 0.0, 299 requests

## Analysis
Recommender shows a large RCA score (2.88), self latency 162 ms vs baseline 25 ms, and elevated error rates, while other services have near‑zero scores and no errors.

## Actions
- Diagnosis only; no action proposed.

## Follow-up
Add a detection rule on `recommender` so this is caught without waiting for the entrypoint symptom.