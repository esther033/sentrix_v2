# Evaluation policy v3 — fixed before the next matched control/CPU pair

v3 retains every v2 criterion except the inter-transaction long-gap bound.
The bound is **15 seconds**, rather than v2's `max(5s, 3/rps)`. This is a
supervisor/data-integrity boundary, not a success-rate threshold: it is set
to three times the configured five-second per-stage timeout. A one or two
transaction timeout can therefore remain visible as failed transactions
without being mislabeled as a host sleep/data collection gap. The 90% count
coverage check still applies to every 30-second impact and 120-second recovery
observation window, so a 15-second gap cannot qualify a recovery period.

All v2 controls remain: planned duration and rate bounds, no-fault >=99%
control, matched workload/environment, complete quality/data views, explicit
fault removal, ten consecutive successful transactions and 120 seconds of
continuous post-window observation. v2 stays the historical policy for its
invalid control attempts. This policy is fixed before CPU fault injection.
