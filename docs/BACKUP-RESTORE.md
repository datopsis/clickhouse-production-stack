# Native backup and restore

This implements roadmap item 3 for **one shard**, ClickHouse **26.8.2.7**, and
Atomic databases with ReplicatedMergeTree tables. AWS provisioning is mocked
only. No AWS account, credentials, deployment, IAM acceptance, KMS operation,
SSM connection, RHEL host, or real disaster recovery time has been verified.
Native amd64/arm64 Actions exercise the production templates and SQL against
TLS-authenticated SeaweedFS. See the PR evidence for measured results.

## Contract and consistency

`replica1` is the default designated backup node. The host helper runs
`SYSTEM SYNC REPLICA` for the selected database's replicated tables, then one
synchronous `BACKUP DATABASE ... TO S3(stack_backup, 'data/ID/')`. One coordinator
is appropriate for this one-shard topology: it avoids independently uploading
both replicas. An unavailable or lagging coordinator fails instead of silently
switching snapshots. Local tables on other replicas are outside this scope.

The selected database defaults to `default`; set `stack_backup_database` to
the application's database. It is **not ALL databases**, configuration, users,
certificates, external dictionaries, external sources or arbitrary Keeper
znodes. Preserve the deployment inputs, Vault and CA recovery material through
the operator's separate secure process. Avoid DDL/mutations during a backup;
this increment tests append-only replicated MergeTree data, not transactional
consistency across tables. Stop writers and drain replication for a business
snapshot spanning multiple tables.

Source reviewed at `v26.8.2.7-lts`:

- [StorageReplicatedMergeTree::backupData and restoreDataFromBackup](https://github.com/ClickHouse/ClickHouse/blob/v26.8.2.7-lts/src/Storages/StorageReplicatedMergeTree.cpp)
  collects visible parts, coordinates replicated part ownership, and selects
  one replica to insert restored data. Other replicas fetch those parts.
- [BackupEntriesCollector](https://github.com/ClickHouse/ClickHouse/blob/v26.8.2.7-lts/src/Backups/BackupEntriesCollector.cpp)
  distinguishes a local coordinator's root layout from `ON CLUSTER` layouts.
- [BackupImpl](https://github.com/ClickHouse/ClickHouse/blob/v26.8.2.7-lts/src/Backups/BackupImpl.cpp)
  stores base references and file checksums; restoring requires the base.
- [BackupInfo](https://github.com/ClickHouse/ClickHouse/blob/v26.8.2.7-lts/src/Backups/BackupInfo.cpp)
  and [BackupIO_S3](https://github.com/ClickHouse/ClickHouse/blob/v26.8.2.7-lts/src/Backups/BackupIO_S3.cpp)
  resolve named collections including session tokens. Temporary credentials
  stay outside SQL and the backup catalog.
- [Native S3 documentation](https://clickhouse.com/docs/operations/backup/s3_endpoint)
  documents `base_backup` and renamed restore syntax. Live documentation is
  not version pinned; the source and native version assertion are authoritative.

A live backup captures the parts visible during collection, not an externally
chosen global transaction timestamp. The concurrent-write test uses one
synchronous append-only writer on the coordinator and a bandwidth-capped full
backup (to guarantee a measurable overlap window), records every acknowledged
ID, and asserts all IDs acknowledged before starting BACKUP are restored and
every restored ID was acknowledged by BACKUP completion. It also checks the
original data count and payloads. It does not claim that every write completed
during BACKUP is included, or that these bounds apply to arbitrary concurrent
writers, retries, async inserts, mutations, or multi-table transactions.

**Restore points are backup times only. There is no WAL/binlog replay between
them.** Configured daily backups imply a nominal 24-hour RPO only while backups
succeed. Monitor the age of the last success; failed jobs make RPO longer.
Small-fixture restore seconds are measured in CI, not a production RTO promise.

## Provisioning and owner choices

Ansible's pinned `amazon.aws.cloudformation` module manages a dedicated backup
resource stack: bucket, rotating customer-managed KMS key, bucket policy, and
an inline policy on an existing ClickHouse instance role. The bucket and key
have retain-on-delete/replace policies and the resource stack has termination
protection. EC2 remains provisioned by the existing Ansible modules.

Required inputs are `stack_backup_bucket`, `stack_clickhouse_instance_profile`
and its existing `stack_clickhouse_role_name`. The ClickHouse profile must be
separate from Keepers' SSM-only `stack_instance_profile`. The operator owns
these profile/role relationships (profile names, not ARNs), scoped controller CloudFormation permissions,
PassRole, SSM policy, private S3 endpoints and KMS administration. Provisioning
refuses an existing VM with a different attached profile; it does not silently
replace it or attach a new role. No real AWS
request is part of tests. The root KMS statement delegates policy management
to the account, not public or universal KMS access.

Defaults to review before deployment:

| Input | Default | Meaning |
| --- | --- | --- |
| `stack_backup_node` | `replica1` | Exactly one scheduler; stop the old timer before moving ownership. |
| `stack_backup_database` | `default` | One application database per configured schedule. |
| `stack_backup_prefix` | `clickhouse` | Exclusive managed namespace within the dedicated bucket. |
| `stack_backup_schedule` | `*-*-* 02:00:00 UTC` | Daily systemd calendar, persistent catch-up after downtime. |
| `stack_backup_full_every_hours` | `168` | New full at least weekly when jobs run. |
| `stack_backup_retention_days` | `30` | Minimum retention of successful restore points, extended by family dependencies. |
| `stack_backup_max_age_hours` | `26` | `verify.yml` rejects stale success. |
| `stack_backup_object_lock` | `false` | Optional owner decision, not enabled implicitly. |

## Object Lock (ransomware protection)

Object Lock stays **off by default** and is an explicit owner choice. The backup
role holds `s3:DeleteObjectVersion` so retention can reclaim expired backups.
Without Object Lock, an attacker controlling a ClickHouse VM can use that role
to permanently delete backup versions. Versioning and SSE-KMS do not prevent
this; SSE-KMS provides confidentiality at rest, not immutability.

| Mode | Protection and trade-off |
| --- | --- |
| `GOVERNANCE` | Blocks the backup role from deleting protected versions; a separately authorized administrator can override retention. Useful for ransomware resistance with an emergency correction path. Protect that administrator separately from the VMs. |
| `COMPLIANCE` | Versions cannot be deleted or their retention shortened, even by the account root, until expiry. Stronger protection, but mistakes and storage charges cannot be undone through an administrative bypass. |

**Recommended opt-in: Governance, with a duration at least as long as backup
retention** (30 days with the current defaults). Native incrementals depend on
an older full: for protection of the complete dependency chain throughout that
window, add at least the full interval (30 + 7 = **37 days** here), and account
for delayed fulls/outages. This implementation does not automatically extend
an old full's Object Lock date when an incremental references it. The latest
family can be retained indefinitely by the helper; a finite object lock is not
indefinite ransomware protection. Monitor successful full creation and rehearse
recovery from protected versions.

For this stack, enable Object Lock **at bucket creation**, before the first
backup. Set these exact Ansible variables in the operator's site vars:

```yaml
stack_backup_object_lock: true
stack_backup_object_lock_mode: GOVERNANCE
stack_backup_object_lock_days: 37
stack_backup_retention_days: 30
```

Use `COMPLIANCE` only after deciding its irreversible operational constraints.
There are no separate CloudFormation parameters: Ansible renders
`Bucket.Properties.ObjectLockEnabled: true`,
`Bucket.Properties.ObjectLockConfiguration.ObjectLockEnabled: Enabled`, and
`Bucket.Properties.ObjectLockConfiguration.Rule.DefaultRetention.Mode/Days`
from the three `stack_backup_object_lock*` variables. Off omits these properties;
on and off remain covered by mocked provisioning tests. Enabling Object Lock
cannot later be undone, and versioning cannot be suspended.

AWS now supports enabling Object Lock on existing versioned buckets. That is a
separate migration, **not tested by this stack**; a newly configured default
protects new versions, not existing backups retroactively. Existing versions
need explicit retention. See [AWS configuration instructions](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-configure.html).
Do not treat changing the flag after deployment as a verified migration.

The expiry job still selects whole expired families, deletes incrementals
before their full, and never requests bypass. If an object is still locked,
S3 refuses deletion: the job exits nonzero, records failure, and leaves the
catalog/base available for retry after expiry. Earlier deletions may already
have completed; the operation is not transactional. A lock longer than the
backup retention window can therefore cause expected cleanup failures until
expiry. Investigate status failures; do not weaken verification or grant bypass
to the VM role to make the timer green. The normal dependency retention can
also keep objects beyond their lock expiry.

A separate administrator needs `s3:BypassGovernanceRetention` **and** permission
for the intended delete/retention operation, and must explicitly send
`x-amz-bypass-governance-retention: true` (AWS CLI
`--bypass-governance-retention`). For example, an authorized administrator can
use `aws s3api delete-object --bucket BUCKET --key KEY --version-id VERSION
--bypass-governance-retention`. This permanently deletes that version; preserve
all retained incremental dependencies before overriding protection. The S3
console may supply the bypass header automatically for an authorized admin.
Bypass does not defeat Compliance mode or a legal hold. See
[AWS retention modes](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)
and [governance bypass](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock-managing.html).

Object Lock protects versions, not availability of the current key: an attacker
may add a delete marker or a malicious newer version, requiring administrator
recovery of known-good versions. It does not protect against stolen read/KMS
decrypt permissions or KMS key destruction. Keep key and bypass administrators
outside the VM's trust boundary. Object Lock behavior is template-tested only;
CI's S3 stand-in does not prove AWS enforcement.

## Credentials and execution

Containers retain UID 101, read-only root, no capabilities and
no-new-privileges. EC2 retains IMDSv2 and hop limit 1. Every ClickHouse host
runs a credential-renewal service every five minutes. It obtains only the
instance role via IMDSv2 (no environment/file/static credential fallback),
atomically writes UID-101-readable `config/config.d/backup.xml`, and ClickHouse
loads that read-only mounted named collection. Its URL and credential fields
are non-overridable; SQL supplies only a relative backup directory. Only the
admin has named-collection access. No token appears in SQL or
command arguments. A backup/restore invocation refreshes before issuing SQL.
ClickHouse freezes collection credentials for an operation: a job that exceeds
the role session's remaining lifetime may fail and needs a smaller workload or
an explicitly qualified credential strategy. Renewal is not a claim that an
already-running backup adopts refreshed credentials.

The production template cannot select the helper's test credential-file hook.
That hook is used only with generated per-run SeaweedFS credentials in CI.
HTTPS verifies both cluster and S3 identities. `loadDefaultCAFile` is false in
all rendered contexts. The ClickHouse server's outbound `client-ca.crt` combines
the private cluster CA with only the five pinned Amazon Trust Services roots;
Keeper, the standalone client and inbound mTLS trust only the private CA.
The host backup SDK uses only `s3-ca.crt` (the pinned ATS bundle). Bundle SHA-256:
`dfaa39c0350288ded35812f7b80f76c4a422b97ecc04c4ac84e325bec528b1b8`.
[Sources, individual fingerprints and update procedure](../ansible/files/README.md)
are committed alongside the bundle; deployment downloads no roots.

The reviewed 26.8 S3 implementation uses the common Poco HTTPS client pool,
with no endpoint-specific CA context. Consequently ClickHouse's outbound
inter-node and Keeper clients also trust the explicit ATS roots. This is not
private-CA-only outbound isolation; no other public roots are trusted. Native
CI checks native and Keeper client rejection of a correctly named leaf from a
second throwaway CA, with successful private-CA TLS handshakes as controls.
SeaweedFS backup/restore uses an explicit CI-only S3 CA in the outbound bundle;
it is never added to the inbound cluster trust store.

The host service uses the existing hash-locked boto3 dependency, not a backup
tool. ClickHouse itself writes and restores all backup data. The SDK only
handles credential acquisition and catalog/retention S3 operations.

## Back up and verify

On the designated host after deployment:

```sh
sudo systemctl start clickhouse-backup.service
sudo /srv/clickhouse-stack/venv/bin/python /srv/clickhouse-stack/backup.py \
  /srv/clickhouse-stack/backup.json status
sudo systemctl list-timers clickhouse-backup.timer
sudo cat /srv/clickhouse-stack/backup-status.json
```

The HTTPS client buffers responses with `wait_end_of_query=1` so a late SQL
error cannot masquerade as an already-streamed HTTP 200. Native CI explicitly
checks a query that throws after producing rows. See the
[HTTP response caveats](https://clickhouse.com/docs/interfaces/http#http-response-codes-caveats).

Force a new full with the same lock and status handling:

```sh
sudo /srv/clickhouse-stack/venv/bin/python /srv/clickhouse-stack/backup.py \
  /srv/clickhouse-stack/backup.json full
```

`run` chooses full/incremental automatically, then applies retention. Nonzero
exit is failure. An OS file lock excludes overlapping manual/timer work;
systemd also serializes its oneshot. Only this node may coordinate writes and
retention for the prefix. This is not a distributed lease. Moving ownership
requires stopping and waiting for the old service first.

The atomic JSON status contains state (`running`, `success`, `failure`), action,
start/completion epoch times, backup ID/base and deleted IDs, or an error type.
It never contains credentials or raw server exception bodies. A crash leaves
`running`, which verification rejects. `verify.yml` checks this record on the
designated node. Bootstrap runs an initial backup before verification; there
is no first-deployment exemption. Future observability should alert on service
failure, renewal failure, running-too-long, and stale/missing status.

The remote `PREFIX/catalog/ID.json` records completed backups, database and
base ID. The data is under `PREFIX/data/ID/`. Keep this catalog accessible during
DR; local status is not needed to restore a known ID. Record row counts and
checksums for important tables at quiesced backup points:

```sql
SELECT count(), sum(cityHash64(*)) FROM app.events;
```

## Retention

Each incremental references its family's full directly. New incrementals can
grow as the week progresses or merges rewrite parts; incremental does not
promise a fixed size ratio in production. Retention keeps every family with
any restore point inside the window, plus the latest family for each database
even when stale. It deletes only whole expired families, children before the
full, removing all object versions and delete markers under their exact paths.
No S3 object-age expiration is configured. Lifecycle aborts incomplete multipart
uploads after seven days only. Never add data/noncurrent-version expiration:
it cannot understand native base dependencies.

A failed backup has no success catalog entry and is never selected as a base.
A process crash may leave orphan data without a catalog entry; inspect it and
remove only after excluding live operations. Automatic orphan cleanup is not
implemented. Unknown or dangling catalog dependencies fail closed. Changing
retention never makes an in-window incremental lose its full.

Stop the scheduler and wait for active work before an external/manual restore
or a retention-policy change. Use the helper on the same designated node for
normal operations so its lock protects a restore against local retention.
During isolated DR, stop production retention too: separate hosts do not share
an OS lock. Do not run external backup writers within the managed prefix.

## Restore under a new name

Choose an unused target. The helper always requires a different name, including
DR. It runs `RESTORE ... AS ... ON CLUSTER stack`: restored replicated data is
inserted once and fetched by peers. Existing nonempty targets fail; never enable
`allow_non_empty_tables` to force a recovery through.

```sh
sudo /srv/clickhouse-stack/venv/bin/python /srv/clickhouse-stack/backup.py \
  /srv/clickhouse-stack/backup.json restore-table \
  --id BACKUP_ID --source app.events --target app.events_undo
sudo /srv/clickhouse-stack/venv/bin/python /srv/clickhouse-stack/backup.py \
  /srv/clickhouse-stack/backup.json restore-database \
  --id BACKUP_ID --source app --target app_recovered
```

Create the target database first when restoring a table into a different DB.
Use **Atomic databases and UUID-based replication paths**, e.g.
`ReplicatedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')`.
A literal Keeper path could point a renamed restore back at the original table;
that schema must be migrated/rehearsed separately before using this runbook.
Do not assume changing a SQL name changes a literal replication path.

Synchronize and compare count/checksum on both replicas. Inspect
`system.replicas.zookeeper_path` to confirm isolation from the original. Keep
clients on the original until the recovered data has been assessed; switching
clients or dropping the original is a separate operator action.

## Disaster recovery drill

1. Pause production retention and record backup IDs and expected signatures.
2. Build a separate private five-VM topology with fresh ClickHouse storage and
   **fresh Keeper storage**, new stack namespace, private addresses and
   certificates. Reuse the selected version and hardened deployment contract.
   Do not attach old Keeper snapshots or point this cluster at live Keepers.
3. Provision the isolated stack with its **own** backup bucket and normal role
   permissions, then deploy it normally, including its initial bootstrap backup.
   Never apply a second resource stack over the source bucket. Stop the isolated
   backup timer. Add scoped read access to the source bucket/prefix and KMS
   decrypt permission to its role through the owner's DR process. Restore using
   a copy of `backup.json` whose bucket, prefix and URL select the source;
   retain the isolated stack's own node, verification and credential paths.
   Its normal schedule must continue using its own bucket after the drill.
4. Make the S3 collection and backup URL refer to the source prefix, choose a
   known completed ID, and restore the database under a new name using the
   command above. Preserve the full when restoring an incremental.
5. Compare exact counts/checksums on both replicas. Insert a disposable probe
   on one restored replica, synchronize the other, and confirm it reads the
   probe. Check Keeper quorum and `system.replicas` health. Remove the probe.
6. Measure elapsed restore/verification time and the age of the selected point.
   Report data beyond that point as unrecoverable by this backup mechanism.
   Resume retention only after the drill has finished.

CI performs the destructive drill only on disposable local Podman storage:
one source cluster, then separate fresh five-node incarnations for the full,
incremental and retained newest family. Keeper application replication metadata
is recreated by SQL. Arbitrary coordination state and lost-host/in-place
Keeper recovery remain outside this scope.

## Native validation

```sh
bash scripts/check.sh
# After the two pinned image builds, on a native Podman host:
sudo env PATH="$PWD/.venv/bin:$PATH" .venv/bin/python tests/backup_functional.py \
  CLICKHOUSE_IMAGE_ID SEAWEEDFS_IMAGE_ID /tmp/backup-evidence.json
```

The pinned SeaweedFS commit is `fd9b0001ee86a432faf773d40ab6f528dbfdad67`.
`scripts/build-seaweedfs.sh` verifies that checkout, uses its artifact-lock
validator/fetch/verifier and offline build script, and asserts native image
architecture. It never publishes images. Each Actions architecture uploads a
secret-free assertion artifact; no generated keys/configuration/server logs
are uploaded by the backup test. AWS/KMS/IMDS behavior is not simulated by a
successful S3-compatible test.
