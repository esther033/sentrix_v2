# Traffic-surge evidence supplement to policy v3

Frozen before the first formal surge run. Existing v3 impact and recovery
thresholds are unchanged. The intervention is a 2 -> 5 -> 2 transactions/s
arrival schedule. Its control uses the identical scenario boundaries at 2/s.

Each transaction summary records k6's scenario name and scenario start time.
All five phases must have observed transactions, consistent scenario start
times, the declared schedule, 90-110% achieved arrival count, and the existing
coverage-gap check. Recovery and cooldown must return to the base rate.
Only then may removal_confidence become confirmed. Wall-clock supervisor
polls alone are not injection/removal evidence. Timing denotes the arrival
schedule transition, not completion of all outstanding surge requests.

## Trace source anomalies

Raw trace completeness describes successful retrieval of known transaction
trace IDs, not correctness of every emitted span. Invalid timestamp intervals
remain unchanged in raw and canonical, with timestamp_valid=false. The RE2
projection excludes them and reports the count. This exclusion must accompany
dataset release notes; raw completeness must never imply zero projection loss.

## Run completion

Workload completion is distinct from export, projection and final assessment.
The attended runner refreshes its heartbeat during postprocessing. A missing
process plus incomplete pipeline is interrupted, never complete. Historical
execution failures remain preserved when a later postprocessing reassessment
succeeds.
