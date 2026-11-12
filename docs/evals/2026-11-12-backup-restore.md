# Backup and restore: native measurements, 12 November 2026

Both native jobs and all existing checks passed in
[run 43213117854](https://github.com/datopsis/clickhouse-production-stack/actions/runs/43213117854)
at commit `cb9f29c984b526f30600dcb44d8019b7d2c9f658`. The actual logs and both
uploaded JSON artifacts were downloaded and inspected. Permanent copies:
[amd64](2026-11-12-backup-amd64.json), [arm64](2026-11-12-backup-arm64.json).
Subsequent assertions additionally pin the observed signatures as constants,
select the newest catalogued incremental for retention recovery, and reject
late SQL errors through HTTP response buffering. The online fixture now uses a
bandwidth-capped full to prevent a fast backup finishing between writer ACKs;
the overlap and consistency assertions remain mandatory. Their results are in the PR's
latest run; the measurements below refer specifically to the linked run.

| Assertion | amd64 | arm64 |
| --- | --- | --- |
| Full: count / sum(cityHash64(*)) | 100000 / 8071977936022618708 | same |
| Full: objects / bytes, one coordinator | 14 / 6831058 | same |
| Incremental: count / checksum | 101000 / 9633416485230737509 | same |
| Incremental: objects / bytes | 8 / 77458 | same |
| Manifest contains base and reused-file references | pass | pass |
| Renamed table: exact full signature; original incremental signature unchanged; different Keeper path | pass | pass |
| Online writes: pre-start ACKs ⊆ restored IDs ⊆ ACKs by completion | 20 ⊆ 20 ⊆ 21 | 21 ⊆ 22 ⊆ 22 |
| Missing backup restore fails | HTTP 500 / code 599 | same |
| Corrupted manifest restore fails | HTTP 500 / code 1000 | same |
| Wrong S3 credentials fail in native SQL | HTTP 500 / code 499 | same |
| Real systemd timer: nonzero service exit, failure JSON, verifier refusal | pass | pass |
| Subsequent successful helper run: fresh success JSON | pass | pass |
| Fresh full DR: both replica signatures exact, cross-replica write/read works | pass, 3.752 s | pass, 3.618 s |
| Fresh incremental DR: both signatures exact, cross-replica write/read works | pass, 3.800 s | pass, 3.767 s |
| Retention removes three expired family members and all their versions | pass | pass |
| Fresh retained-family DR: exact signature, cross-replica write/read works | pass, 3.675 s | pass, 3.662 s |

The retained point contains the non-deterministic number of online writes plus
one explicit later row: amd64 `101022 / 11969266495042210420`, arm64
`101024 / 2298852680825790458`. Each restore matches its own source exactly.
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
| functional amd64 | 5m24s | 7m57s | +2m33s |
| functional arm64 | 7m20s | 7m13s | -7s |
| lint | 1m03s | 1m17s | +14s |
| Longest job | 7m20s | 7m57s | +37s |

The new backup test itself took 294.504 seconds on amd64 and 280.484 seconds on
arm64. These are single-run comparisons: download/build/runner variance masks
some added work, especially on arm64. Do not interpret the arm64 total as an
optimization. Both jobs remain bounded by the unchanged 45-minute timeout.

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
