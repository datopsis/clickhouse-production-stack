# Ordered remaining work

This is the plan of record. Passing PR CI is development evidence, not a
supported release, AWS rehearsal, FIPS/STIG claim or human sign-off.

1. **Close platform and image qualification.** Owner chooses the exact RHEL
   AMI/Podman/SELinux baseline, instance/storage sizing, image digest and Keeper
   role support. Rehearse provisioning, SSM, idempotence, rolling failure,
   reboot, retained-volume recovery and multi-AZ failures in an explicitly
   authorized disposable AWS account. Verify controller/host IAM and private
   networking. No AWS apply has occurred in this increment.
2. **Observability.** Add authenticated metrics collection, dashboards, replica
   lag/Keeper quorum/disk/TLS expiry alerts, structured log retention and tested
   alert delivery. Define service objectives before alert thresholds. No
   observability service is implemented in the first increment.
3. **Backup and restore.** Choose storage/IAM/KMS/retention policies, implement
   consistent backups, restore into an isolated cluster, test point-in-time and
   disaster recovery, and measure RPO/RTO. Include Keeper metadata and lost-host
   recovery. No backup implementation is included in the first increment.
4. **Upgrades and lifecycle.** Version compatibility checks, staged upgrades,
   preflight backup gates, rollback limits, VM/storage replacement, dual-secret
   and CA overlap rotation, maintenance draining and recovery from interrupted
   bootstrap. Add client failover/insert-quorum policies and workload tests.
5. **Multiple shards.** Extend the currently one-shard contract, distributed
   table policy, shard placement, capacity/rebalancing and failure tests. Reject
   unsupported shard counts until those tests exist.
6. **Kubernetes later.** Evaluate a separate deployment target after operational
   contracts stabilize. Reuse image/configuration/verification contracts; do
   not introduce Kubernetes machinery or dependencies into AWS v1.

Owner decisions still needed: exact host baseline, Keeper support contract,
release image/admission policy, CA/credential rotation ownership, client entry
points and consistency requirements, storage sizing/retention and future
recovery objectives. No approval has been manufactured for these decisions.
