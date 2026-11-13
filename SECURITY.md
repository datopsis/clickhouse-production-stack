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

## TLS trust scope

All TLS contexts disable `loadDefaultCAFile`. Inbound mTLS, Keeper and the
standalone client trust only the private cluster CA. ClickHouse's server-side
outbound context trusts the private CA plus the five explicitly pinned Amazon
Trust Services roots needed for the AWS custom trust-store set; the host backup
SDK trusts only those ATS roots. No system-wide default bundle is used here.
The [vendored bundle and manifest](ansible/files/README.md) record source URLs,
license and SHA-256 hashes, checked by Ansible and contract tests.

ClickHouse 26.8 native S3 uses a shared Poco HTTPS context without an S3-specific
CA override. Thus outbound inter-node and Keeper connections from ClickHouse
also trust those ATS roots. This does not provide per-endpoint trust isolation;
restrict certificate issuance, require identity verification and keep inbound
mTLS private-CA-only. Native CI proves that native and Keeper clients reject a
correctly named certificate from an untrusted second CA, alongside trusted
handshake controls and successful SeaweedFS S3 recovery.

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
access to administrators; collection URLs and credentials are non-overridable.
Restoration of metadata is a privileged operation.

Bucket read plus KMS decrypt access exposes all backed-up rows and schema.
Bucket write can corrupt/replace data or metadata; delete-version permissions
can destroy recovery points. Checksums detect damage, not malicious provenance.
Treat writable backup metadata as untrusted SQL input and restore only from a
trusted administrative boundary. KMS key loss/revocation can make otherwise
intact backups unreadable. Versioning alone does not stop a compromised backup
role: its `s3:DeleteObjectVersion` permission permits permanent deletion of
backups. Object Lock stays off by default and user-configured; opt-in Governance
is recommended with a duration covering retention and full-base dependencies.
The VM role has no bypass permission. A separately authorized administrator can
bypass Governance with an explicit request; Compliance cannot be bypassed.
See the [ransomware protection runbook](docs/BACKUP-RESTORE.md#object-lock-ransomware-protection)
for creation-time configuration, cleanup failures and version recovery limits.
Protect bucket/key administrators, host root and SSM access independently.

The retention helper deletes complete expired families, including old object
versions, and never applies object-age lifecycle expiration to backup data.
Pause retention for external DR readers. Catalog or local status access does
not prove backup integrity: perform the documented fresh-cluster restore drill.
