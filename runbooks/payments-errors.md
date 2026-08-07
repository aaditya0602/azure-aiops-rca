---
id: rb-payments-errors-v3
service: payments
supersedes: rb-payments-errors-v2
symptoms:
  - payments own-error rate elevated
  - orders and gateway error rates elevated with payments as the deepest culprit
allowed_actions:
  - verb: rollback
    target: payments
    required_preconditions:
      - recent_deploy_within_1h
      - previous_revision_healthy
  - verb: drain
    target: payments
    required_preconditions:
      - spare_capacity_confirmed
---

# Payments error spike

Distinguish payments failing on its own from payments surfacing a ledger failure.
Own-error rate — errors on payments spans with no failed child span — is the
signal that separates them. If payments' errors all have a failed `call ledger`
child, the cause is downstream and this runbook does not apply.

## Mitigate

Rollback only if a deploy landed within the last hour and the previous revision is
known healthy. Rolling back to an untested revision during an incident turns one
problem into two.

**Never** restart payments to clear an error spike: in-flight authorizations are
not idempotent and a restart can double-charge.
