---
id: rb-ledger-latency-v2
service: ledger
supersedes: rb-ledger-latency-v1
symptoms:
  - ledger client-span latency above 5x baseline
  - orders and payments inclusive latency elevated with normal self time
allowed_actions:
  - verb: failover
    target: ledger
    required_preconditions:
      - replica_lag_under_10s
      - no_active_schema_migration
      - primary_confirmed_unhealthy
  - verb: scale
    target: ledger
    required_preconditions:
      - connection_pool_saturated
---

# Ledger latency degradation

The ledger is a Postgres-backed store reached only through client spans, so it has
no self time of its own. Elevated ledger latency shows up as elevated *inclusive*
latency on `payments` and `orders` while their self time stays normal.

## Diagnose

1. Compare ledger client-span p95 against the 7-day baseline.
2. Check `pg_stat_activity` for long-running queries and lock waits.
3. Check connection pool saturation on `payments`.

## Mitigate

Failover is **only** valid when replica lag is under 10s, no schema migration is
in flight, and the primary is confirmed unhealthy. Failing over during a migration
can lose committed writes.

If the pool is saturated but the primary is healthy, scale the pool instead — a
failover will not help and costs an election.
