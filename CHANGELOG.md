# Changelog

## Unreleased

- Add the v1 Ansible/AWS and Podman/Compose deployment with a variable-driven
  one-shard, two-replica, three-Keeper default across private multi-AZ VMs.
- Require IMDSv2, encrypted retained EBS, explicit security-group flows and
  SSM access. Provisioning has never been applied to AWS.
- Add strict TLS, runtime Vault secrets, non-root hardened services, guarded
  serial deployment, health verification and an opt-in replication probe.
- Add hash-locked tools, pinned collections/actions/image source, lint,
  secret scanning, offline AWS doubles and native Podman topology tests.
- Document control ownership, first-release limitations and ordered remaining
  observability, backup/restore, upgrades, multiple-shard and Kubernetes work.
