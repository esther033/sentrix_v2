# Evaluation policy v2 — fixed before the new matched control/CPU pair

v1 and earlier run artifacts remain historical. This policy is implemented
in scripts/evaluation_v2.py and is not tuned to the new fault's outcome.

Impact keeps v1's 5 percentage-point success drop and 3x control p99,
computed on complete 30-second windows only. A window requires >=90% of
expected transactions and no boundary/interior gap above max(5s,3/rps).
This is an aggregate p99 rule, not proof that every request was slow.

Recovery: ten consecutive successful transactions with p50 <=1.5x control
p50, followed (after the tenth transaction completes) by an additional
120 seconds of continuous observation using the same coverage rule and
no impact-triggering full 30s window. stable_at is the first transaction
of that qualifying ten. No complete follow-up observation => unknown.
With complete observation but no qualifying recovery => fail, meaning
not recovered within observation, not permanently unrecoverable.

Control must be no-fault, >=99% successful, with both quality gates and
versioned data views complete, no pod restarts/config changes, and matching
workload timing/rate/VUs/timeouts and environment fingerprint. Fault starts
within 2h of control completion. Fingerprint includes namespace UID, node
identity, workload/collector/storage pod templates, service specs, configmap
data and running image IDs. It excludes transient status and timestamps.

Run validity requires 90-110% target transaction rate over planned duration,
<=2% dropped iterations, transaction elapsed duration within [-5,+30] seconds,
supervised runtime within [-5,+60] seconds, coverage without long gaps,
unique transaction IDs, successful workload and pipeline stages, both
quality gates, stable config and healthy environment after workload.
Recovery outcome is independent of dataset validity.

The supervisor detects >15s gaps between its polls, console inactivity >60s,
process exit and hard duration limits. Sleep prevention is best effort;
lid closure or machine suspension can still invalidate a run. Injection and
removal raw responses are saved. Only explicit AllRecovered is accepted;
NotFound alone is not proof that the effect was reversed. A failed fault
run requests pause on its own Chaos object and records recovery evidence.

Both runs use CPU rate over 60s, span metrics over 30s and identical 19-minute
profiles (2min warmup, 5min baseline, 5min fault/control interval, 5min
recovery, 2min cooldown). No threshold extension after seeing a result.
