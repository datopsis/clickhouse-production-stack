# Architecture and ownership

The v1 design is Ansible on AWS, followed by Podman with Compose on EC2.
There is no Kubernetes dependency, controller, manifest or abstraction layer.
The XML templates and immutable image contract are independent of AWS; a later
orchestrator can reuse them with its own addressing, secrets and storage.

## Traffic and placement

Each node has a separate private VM. The default Keeper placements are AZs
a/b/c; the two replicas are in a/b, comprising one shard. Names and IDs are
stable variables, not generated ordinal identities. `/etc/hosts` entries in
each container resolve peer names to operator-reserved private IPs. Certificate
SANs include those names and reserved private IPs. The ClickHouse Keeper client
validates its resolved IP, so the IP SAN is required while extended verification
stays enabled. CI uses a private Compose subnet with fixed IPs for the same contract. The container's bridge address is never advertised for
inter-server fetches: `interserver_http_host` advertises its stable peer name.

| Source | Destination | TCP ports | Protection |
| --- | --- | --- | --- |
| Approved client SGs | Replicas | 8443 HTTPS, 9440 native TLS | mTLS plus password |
| Replicas | Replicas | 8443 verification, 9440 distributed queries, 9010 part fetches | mTLS, cluster secret / inter-server password |
| Replicas | Keepers | 9281 | mTLS plus digest identity for znode ACLs |
| Keepers | Keepers | 9444 Raft, 9281 quorum verification | mTLS |
| Hosts | Operator endpoints/proxy/mirrors | 443 | AWS/API/registry/repository TLS |

Server mTLS validates the client certificate chain; it does not require a
client source IP in its certificate. Outbound clients validate the named server.
SQL identities and Keeper ACLs provide application authorization in addition
to transport trust.

Provisioning checks explicit subnet route tables, falling back to the VPC main
table, and rejects direct internet-gateway routes. NAT/transit gateways and
endpoint policy still require operator review.

No port 22, public IP, default egress rule or plaintext service port is created.
Security groups are stateful, so response traffic does not require reverse
rules. AWS VPC resolver/time services have AWS-specific SG semantics; network
firewalls, DNS policy and time synchronization remain account responsibilities.
The stack does not provision a load balancer. Clients must be configured with
both replica endpoints and a driver retry policy. CI proves the survivor can
serve and recover, not transparent failover of an arbitrary client session.
Replication is asynchronous by default; a successful insert with no quorum
setting is not a promise against data loss under multiple failures. Workload
consistency and `insert_quorum` policy must be chosen by the operator.

## Boundaries

| Stack controls | Account/operator controls |
| --- | --- |
| EC2 creation with explicit private interface, IMDSv2, hop limit 1, encrypted gp3 EBS, retention and termination protection | VPC/subnets/routes/AZ capacity, reserved IP allocation, AMI provenance, quotas, billing, KMS keys and grants |
| Dedicated role SGs with explicit ingress and egress | Client SG ownership, private connectivity, endpoints, DNS policies and egress destinations |
| SSM inventory and encrypted transfer configuration | IAM instance profile, controller IAM/SSM policy, SSM agent, Session Manager plugin and session audit |
| Podman packages, locked Compose provider, systemd boot lifecycle | Approved RHEL repositories, mirrors, package maintenance, SELinux/cgroup qualification, kernel/OS patching and time sync |
| Non-root UID 101, read-only root, no capabilities, no-new-privileges, resource bounds | Host root/SSM administrator trust and approved workload sizing |
| TLS-only configurations, secret file permissions and redacted Ansible tasks | Dedicated CA, certificate issuance/rotation/revocation, Vault keys, password generation and client certificates |
| Serial rollouts, health gates, disposable verification option | Maintenance windows, recovery decisions, data retention, client retry/consistency policy |
| Native development tests and immutable CI image source | Release image qualification, signature policy, image registry access, support decisions |

The SSM connection plugin uses S3 even for some non-copy tasks. Supply a private
transfer bucket with public access blocked, enforced TLS and SSE-KMS, tight
controller permissions scoped to the instance prefixes, short object lifecycle,
and **versioning disabled** (or an independently enforced deletion policy for
all old versions). Ansible deletes transfer objects after normal completion;
interrupted runs can leave files, including secret-bearing module arguments.
Do not assume `no_log` protects the S3 transport or SSM/session capture. Restrict
all those stores and logs. See the
[Ansible SSM connection documentation](https://docs.ansible.com/projects/ansible/latest/collections/amazon/aws/aws_ssm_connection.html).

Provisioning attaches an existing instance profile rather than creating broad
IAM policies. At minimum the host needs the SSM agent control/data channels;
the controller needs scoped session start/resume/terminate, instance/subnet
read, EC2 launch/tag/SG operations, restricted `iam:PassRole`, transfer-bucket
access and KMS use. The account owner must derive and review exact resources
and conditions. No example wildcard IAM policy or account setting change is
made here. Registry and package access must work through allowed HTTPS paths;
no public networking is added as a fallback.

## Lifecycle and limitations

Compose allows 300 seconds for graceful shutdown and systemd allows 360; this
exceeds ClickHouse's 120-second connection-drain default and leaves time for
coordination/storage cleanup. The failure test deliberately kills one replica to simulate abrupt loss.
The ClickHouse watchdog is disabled explicitly so the server is PID 1 and
receives shutdown signals directly; Podman owns crash restart. CI requires
exit code zero on normal database shutdown.

Normal reruns verify health before editing each node, then recreate only that
node on template/secret/image changes and verify it before advancing. Bootstrap
is explicitly different because quorum cannot exist until enough Keepers have
started. Bootstrap over an existing installation is rejected. Failed partial
bootstrap and failed rolling changes stop for diagnosis; no automated rollback
or destructive retry is implemented.

Changing a shared secret or CA requires an overlap/rotation procedure; replacing
all credentials in one pass can break peer authentication during a rolling
update. This increment does not automate dual-trust rotation. Do not use a
normal rollout as an untested credential migration. Stop and rehearse rotation
on a disposable cluster first. The roadmap tracks that work.

Data and Keeper logs/snapshots are persistent bind mounts on encrypted EBS;
`/tmp` is disposable. EBS retention and replication are not backup/restore.
There is no automated volume adoption, host replacement, destructive teardown,
monitoring or alerting implementation in this increment.

Tests on GitHub are development evidence. The mock AWS modules prove the
playbook reaches expected parameter contracts with no network or SDK calls;
they do not prove IAM permissions, API acceptance, subnet routing, AMI/SSM
readiness, SELinux behavior or storage lifecycle on AWS. An operator-applied
rehearsal is required before any operational claim. None has occurred.

## References reviewed

- `datopsis/clickhouse-ubi` at `975d2818ec4f3f295fb5012aeb44cf83cc94a058`:
  README, Compose, Containerfile, entrypoint, runtime/TLS/production docs,
  agent guidance, roadmap and hardened CI.
- `datopsis/nginx-ubi` at `aa60c0612e1043f77df7476ce97e30724719e784`:
  agent guidance, roadmap and CI conventions.
- `datopsis/seaweedfs-ubi` at `1bfd88f26ff63f9b33839ab1d5477e8f7f57376c`:
  agent guidance and CI; its work plan is `docs/README.md` (there is no
  `docs/ROADMAP.md` at that revision).
- [ClickHouse TLS configuration](https://clickhouse.com/docs/guides/sre/tls/configuring-tls)
  and [Keeper configuration](https://clickhouse.com/docs/guides/sre/keeper/clickhouse-keeper).

`datopsis/container-hardening` is not a CI dependency. No human approval or
support qualification is asserted by any checked test.

## Backup ownership

Native SQL writes a single configured database to a dedicated private S3 bucket,
separate from SSM's transfer bucket. Ansible manages the bucket, customer-managed
rotating KMS key, TLS-only policy, versioning, multipart-abort lifecycle and a
bucket/prefix-scoped policy on the existing ClickHouse role through a dedicated
CloudFormation resource stack. The bucket/key are retained on removal. Keepers
keep a separate SSM-only role. The operator controls IAM profiles, controller
permissions, KMS administration, private endpoints and disaster recovery access.
All provisioning is mocked; AWS acceptance remains unverified.

One designated host schedules native full/incremental SQL and retention. Both
replica hosts renew short-lived IMDSv2 role credentials into a read-only mounted
named collection, preserving hop limit 1. Only outgoing TLS clients also trust
the public CA bundle for AWS; inbound cluster mTLS retains the dedicated CA.
Backup status is a local atomic JSON record consumed by verification. Object
Lock, schedule, retention, failure-alert delivery and recovery objectives are
owner decisions. A compromised host/role can delete backups unless a separate
immutability boundary prevents it.

Fresh-cluster restore rebuilds replicated-table Keeper metadata from SQL; it
never imports live Keeper snapshots. Only backup-time recovery is implemented,
with no transaction replay. See [the runbook](BACKUP-RESTORE.md) for scope,
replication-path requirements and retention coordination during DR.
