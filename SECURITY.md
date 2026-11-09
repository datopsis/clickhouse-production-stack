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
