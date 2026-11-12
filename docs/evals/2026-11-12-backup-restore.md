# Backup and restore: native measurements, 12 November 2026

Both native jobs and all existing checks passed in
[run 43216232375](https://github.com/datopsis/clickhouse-production-stack/actions/runs/43216232375)
at commit `e3799e34b17bb3fbb30cb5ce005b562dc58a7db0`. The actual logs and both
uploaded JSON artifacts were downloaded and inspected. Permanent copies:
[amd64](2026-11-12-backup-amd64.json), [arm64](2026-11-12-backup-arm64.json).
All 12 assertion groups pass on both architectures. Signatures are pinned as
constants, retention verifies deletion of every old version/delete marker and
restores the newest catalogued incremental, and HTTP buffering rejects late
SQL errors. The online fixture uses a bandwidth-capped full to ensure a
measurable overlap with writer ACKs. The measurements below refer to this run.

| Assertion | amd64 | arm64 |
| --- | --- | --- |
| Late SQL error rejected after rows are produced | HTTP 500 / code 395 | same |
| Full: count / sum(cityHash64(*)) | 100000 / 8071977936022618708 | same |
| Full: objects / bytes, one coordinator | 14 / 6831058 | same |
| Incremental: count / checksum | 101000 / 9633416485230737509 | same |
| Incremental: objects / bytes | 8 / 77458 | same |
| Manifest contains base and reused-file references | pass | pass |
| Renamed table: exact full signature; original incremental signature unchanged; different Keeper path | pass | pass |
| Online writes: pre-start ACK set ⊆ restored IDs ⊆ end-ACK set (counts) | 19 / 19 / 75 | 15 / 15 / 74 |
| Missing backup restore fails | HTTP 500 / code 599 | same |
| Corrupted manifest restore fails | HTTP 500 / code 1000 | same |
| Wrong S3 credentials fail in native SQL | HTTP 500 / code 499 | same |
| Real systemd timer: nonzero service exit, failure JSON, verifier refusal | pass | pass |
| Subsequent successful helper run: fresh success JSON | pass | pass |
| Fresh full DR: both replica signatures exact, cross-replica write/read works | pass, 3.810 s | pass, 3.682 s |
| Fresh incremental DR: both signatures exact, cross-replica write/read works | pass, 3.833 s | pass, 3.716 s |
| Retention removes three expired family members and all their versions | pass | pass |
| Fresh retained-family DR: exact signature, cross-replica write/read works | pass, 3.665 s | pass, 3.555 s |

The retained point contains the non-deterministic number of online writes plus
one explicit later row: amd64 `101077 / 5941953911302772774`, arm64
`101076 / 6597183697568225004`. Each restore matches its own source exactly.
The live-write test also rejects duplicate/gapped live IDs, checks their payload,
and checks the unchanged initial data's full count/checksum. These are witnesses
for one synchronous writer on the coordinator, not a multi-table transaction or
a guarantee for arbitrary acknowledgement/network timing. All DR incarnations
use empty storage directories and fresh Keeper state, not a reused live cluster.
Restore seconds cover SQL, synchronization, signatures and a replication probe;
provisioning, starting containers and bringing up the OS are excluded.

## Timing

Compared with the latest successful pre-backup main run
[43035691238](https://github.com/datopsis/clickhouse-production-stack/actions/runs/43035691238):

| Job | Before | Measured run | Difference |
| --- | --- | --- | --- |
| functional amd64 | 5m24s | 7m46s | +2m22s |
| functional arm64 | 7m20s | 7m41s | +21s |
| lint | 1m03s | 1m04s | +1s |
| Longest job | 7m20s | 7m46s | +26s |

The new backup test itself took 320.531 seconds on amd64 and 304.798 seconds on
arm64. These are single-run comparisons: download/build/runner variance masks
some added work. Do not interpret the wall-time difference as a controlled
benchmark of the feature alone. Both jobs remain bounded by the unchanged 45-minute timeout.

## Decisions and limits

The one-shard coordinator avoids duplicate per-replica uploads. Each incremental
references its full directly; retention deletes expired families, keeping all
bases needed by retained restore points and the latest family for each database.
Unit tests additionally cover stale latest families, an in-window child pinning
an old full, interrupted deletion, invalid catalog dependencies, concurrent
process exclusion and failed/stale status refusal. Existing security tests were
kept. Merges are paused only for deterministic incremental-size measurement and
resumed before live-write testing; synchronization is not bypassed.

Provisioning tests use offline doubles only, including optional Object Lock.
**No real AWS account or credentials were used or requested.** AWS API/IAM/KMS
acceptance, IMDS temporary-token delivery/expiry, RHEL/SELinux, Object Lock
runtime behavior and production-sized RPO/RTO remain unverified. The S3 stand-in
proves SQL/S3/TLS behavior, not AWS policy enforcement. Recovery uses backup
points only; no replay, cross-region copies, arbitrary Keeper-state recovery or
alert delivery is implemented.

Owner decisions remain daily 02:00 UTC scheduling, weekly full frequency,
30-day minimum retention, 26-hour freshness threshold, Object Lock mode and
duration, restore objectives, and ownership of future failure alerts. These are
configurable defaults and recorded decisions to make, not manufactured approval.
