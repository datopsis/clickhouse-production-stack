# Security policy

This repository has no supported release yet. Report suspected vulnerabilities
privately through [GitHub private vulnerability reporting](https://github.com/datopsis/clickhouse-production-stack/security/advisories/new)
if available. If that facility is unavailable, contact a Datopsis maintainer
through their existing private channel; do not put exploit details or secrets
in a public issue. The owner must confirm the reporting channel is enabled
and monitored. This PR does not change repository settings.

Preserve non-root containers, read-only roots, no capabilities,
no-new-privileges, digest-pinned deployment images, TLS verification and
runtime-supplied secrets. Never commit keys, Vault passwords, real credentials,
generated test credentials or AWS account data. `no_log` is not protection for
S3 transfer objects or host/session logs; see the architecture boundary.

Do not weaken a failed check. Record the exact input, failing command and
unverified boundary. CI is not evidence of production readiness, FIPS
validation, STIG compliance, host qualification or AWS provisioning success.
Rotate compromised secrets and certificates through the operator's incident
process; automated rotation and recovery are not implemented yet.

## Backup trust boundary

The dedicated backup bucket defaults to SSE-KMS with a provisioned rotating
customer-managed key, versioning, Block Public Access, bucket-owner-enforced
ownership and a TLS-only policy. Explicit non-KMS or wrong-key requests are
refused. These are mocked provisioning contracts, not verified AWS controls.
See [AWS SSE-KMS](https://docs.aws.amazon.com/AmazonS3/latest/userguide/UsingKMSEncryption.html).

Only the ClickHouse instance role receives data/version read-write-delete
permissions for the configured bucket prefix; KMS use is restricted to that
key through regional S3 and that prefix's encryption context. Keepers retain
SSM-only profiles. Host helpers use IMDSv2 only, with hop limit 1 unchanged;
they write temporary session credentials to protected read-only container
configuration. Runtime renewal, token expiry and IAM enforcement still require
an authorized AWS rehearsal. No static credentials or CA private keys are
shipped. SQL never embeds AWS keys. Restrict named-collection and BACKUP/RESTORE
access to administrators; collection URL overrides and restoration of metadata
are privileged operations.

Bucket read plus KMS decrypt access exposes all backed-up rows and schema.
Bucket write can corrupt/replace data or metadata; delete-version permissions
can destroy recovery points. Checksums detect damage, not malicious provenance.
Treat writable backup metadata as untrusted SQL input and restore only from a
trusted administrative boundary. KMS key loss/revocation can make otherwise
intact backups unreadable. Versioning alone does not stop a compromised backup
role, which needs deletion for retention. Object Lock is optional, with no
bypass permission, and requires the owner's explicit mode/duration decision.
Protect bucket/key administrators, host root and SSM access independently.

The retention helper deletes complete expired families, including old object
versions, and never applies object-age lifecycle expiration to backup data.
Pause retention for external DR readers. Catalog or local status access does
not prove backup integrity: perform the documented fresh-cluster restore drill.
