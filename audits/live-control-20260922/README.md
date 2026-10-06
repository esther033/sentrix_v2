# Live control validation — m2-live-control-20260922-001

Final selected generation: **cpu60-v3**. `pipeline-cpu60-v3.json` points
to the final canonical/RE2 directories and binds source/output hashes.
Export, canonicalization, projection, and hash validation passed.

The four-minute no-fault workload completed 481/481 transactions successfully;
timeout and dropped iterations were zero. Transaction p50=295ms, p99=2677ms.
This is a smoke/integration run, not a formal control for the CPU incident.

Gate A and B passed. Event UID matched with 0.395086s collection lag.
The sentinel was generated after the workload export window; its six raw
Loki entries are separately retained in sentinel-loki-response.json.
Zero events inside the workload window does not contradict that result.

Final raw export contains all 2,215 discovered traces, including all 481
transaction trace IDs, and 35,395 log lines. RE2 metrics have 315 rows and
51 columns with no required service columns missing. Canonical data keeps
unresolved identity rows; RE2 intentionally excludes them.

Validation evidence:
- result.json: transaction results, counts, source/derived row checks,
  CPU/memory/workload/error numerical comparisons.
- latency-crosscheck.json: seven services × p50/p90, all 14 values agree
  with direct Prometheus histogram_quantile calculations at one sample time.
- pods-before.json / pods-after.json: no container restart-count increases
  during the attended control/export period. This is not a long-term check.
- chaos-after.json: prior faults remained recovered; no new fault injected.

Problems found and retained:
1. No port-forward processes were running. IPv4 frontend/observability
   forwarding restored. Additional IPv6 loopback observability forwarding
   removed localhost connection fallback delays (~2.1s to ~0.05s).
2. Windows transient destination locks caused atomic rename failures.
   Bounded rename retry added; regression test passed (41 offline tests).
3. Two pre-workload CPU 30s windows had only one cAdvisor sample. Kept the
   incomplete raw-v2 generation; CPU rate changed explicitly to 60s and
   re-exported as cpu60-v3. Control/fault comparisons must use that same rule.
4. One extra searched trace initially failed with a closed connection.
   After the overnight continuation it returned 404. Its validated original
   response already existed in raw-v2. Imported that checkpoint into the
   new generation with original timestamp, checksum, identical request, and
   provenance under raw-cpu60-v3/_export/recoveries/. No invented spans or
   forced completion flags were used. Prior failed attempts remain archived.

The localhost frontend returned HTTP 200 at final verification. Existing M1,
old M2 runs, and PVCs were not deleted. Remaining work: recovery/control
policy corrections and matched formal control → CPU fault pilot.
