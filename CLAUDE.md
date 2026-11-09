# Repository guidance

Follow the owner's v1 design: Ansible provisions AWS; Podman with Compose runs
ClickHouse and Keeper on separate private VMs. Kubernetes is roadmap-only.
Preserve the Datopsis image's non-root runtime, read-only root, no capabilities
and no-new-privileges. Never replace its Keeper image silently.

Use required digest-pinned deployment references and a reviewed pinned source
commit for CI builds until the image has releases. Secrets come from runtime
Vault inputs; never add default or placeholder working credentials.

Run `bash scripts/check.sh` with the hash-locked tool environment, then gitleaks
and the native Podman integration test when the environment permits it. Mock
provisioning only: do not request AWS credentials or touch a real account
without a separate explicit instruction. Do not change repository/org settings,
secrets, permissions or branch protections. Never weaken checks to get green.

Update README, architecture, CHANGELOG and docs/ROADMAP.md with behavior and
limitations. Keep implementation simple; separate image/runtime controls from
account/operator responsibilities. Do not claim production readiness, FIPS,
STIG or human approval from automation. No container-hardening CI dependency.

Use Conventional Commit subjects (`feat:`, `fix:`, `test:`, `ci:`, `docs:`).
Do not add AI/tool attribution or Co-Authored-By trailers. Never force-push.
Open a reviewable PR and wait for CI; do not merge without instruction.
