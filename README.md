# ClickHouse production stack

Ansible provisions AWS EC2; Podman with Compose runs the services. This first
increment is development automation, not a production readiness claim.
Provisioning has **never been applied to AWS** by this project. No AWS resources
or credentials are used in CI.

The variable-driven default is **one shard, two ClickHouse replicas and three
Keeper nodes**, each on its own private VM. Keepers span three availability
zones; replicas span at least two. Multiple shards, observability and backups
are future work in [the ordered roadmap](docs/ROADMAP.md).

## Image contract

Both roles use `datopsis/clickhouse-ubi`. `stack_image` is required and must be
`registry/repository@sha256:` followed by the real 64-character digest. There is
no default, mutable tag or fabricated deployable reference. The image has no
published GitHub release yet. Operators must build/review an immutable artifact
and supply its registry digest; do not infer release qualification from CI.

CI builds image source commit `975d2818ec4f3f295fb5012aeb44cf83cc94a058`, then
uses the resulting local image ID without publishing. The image includes the
multi-call `clickhouse` binary from `clickhouse-common-static` and its entrypoint
executes explicit commands. The stack invokes `clickhouse keeper` and
`clickhouse server` with complete, mounted configuration. It deliberately
bypasses server-only entrypoint initialization, including its generated default
user and loopback health configuration. **Keeper is technically executable;
it is not a documented or qualified role in the image repository.** The stack's
native integration test exercises it; the image owner still needs to decide
its support contract. No alternative Keeper image is substituted.

Every service runs as `101:0`, with a read-only root, all capabilities dropped,
`no-new-privileges`, a bounded `/tmp` tmpfs, and explicit durable storage.
Configuration and TLS keys are mounted read-only. The Podman engine/systemd
unit are rootful; the container processes are non-root throughout. The internal
ClickHouse watchdog is disabled so Podman supervises the actual server process.

## Operator inputs

Copy [site.example.yml](ansible/vars/site.example.yml) outside Git and fill every
required `null`. Defaults live in [defaults.yml](ansible/vars/defaults.yml).

| Variable | Contract |
| --- | --- |
| `stack_name` | Stable namespace for EC2 tags, security groups and containers. |
| `stack_region`, `stack_vpc_id` | Existing operator-managed AWS region and VPC. |
| `stack_subnets` | `a`, `b`, `c` to private subnet IDs in three different AZs. No public-IP-on-launch or direct internet-gateway route. |
| `stack_nodes` | List of `{name, role, id, az}`; default five separate VMs. Exactly three Keepers; at least two replicas. Keep names/IDs/IPs stable. |
| `stack_node_ips` | Node name to reserved private IPv4 address in its designated subnet. Names become certificate SANs and container peer aliases. |
| `stack_shards` | Default and currently accepted value: `1`. |
| `stack_ami_id` | Approved immutable RHEL 9 x86_64 AMI with active SSM agent, Python 3.11 and approved package repositories. |
| `stack_root_device` | AMI root block device name; default `/dev/sda1`, verify against your AMI. |
| `stack_instance_type`, `stack_root_volume_gib` | Defaults `m7i.large`, 100 GiB gp3. Workload sizing remains the operator's responsibility. |
| `stack_instance_profile` | Existing instance profile providing SSM managed-instance access; no application AWS privileges. |
| `stack_ebs_kms_key_id` | Operator-managed KMS key for encrypted EBS. Root/data volume is retained on termination; termination protection is enabled. |
| `stack_ssm_bucket`, `stack_ssm_kms_key_id` | Existing private S3 transfer bucket and KMS key for the Ansible SSM connection. |
| `stack_https_egress_cidrs` | Required private endpoint/proxy/mirror destination CIDRs for HTTPS, including SSM, S3, registry and package access. Catch-all `/0` refused. |
| `stack_client_security_groups` | Existing client SG IDs admitted on 8443/9440 only; default empty. |
| `stack_image` | Required immutable digest of the reviewed Datopsis image or its internal mirror. |
| `stack_bootstrap` | Default `false`. Set `true` only for the initial empty-host deployment. Refused over existing installations. |
| `stack_root` | Default `/srv/clickhouse-stack`; persistent data/configuration location. |
| `stack_verify_writes` | Default `false`. Enable only to create, replicate and remove a disposable verification table. |

Supply `stack_secrets` in an **Ansible Vault encrypted file** using
`ansible-vault create`, never a plaintext example or Git commit. The required
schema below describes fields, not usable credentials:

- `admin_password`, `reader_password`, `interserver_password`, `cluster_secret`:
  independently generated secrets of at least 32 characters each.
- `keeper_identity`: a dedicated `username:password` digest identity, password
  at least 32 characters. ClickHouse uses it for Keeper znode ACLs.
- `ca_cert`: PEM trust bundle for a dedicated cluster CA.
- `nodes`: mapping of every node name to `cert` (PEM chain) and `key` (PEM private
  key). Each certificate needs its node name **and reserved private IP** in SAN and both `serverAuth` and
  `clientAuth` usage. Client certificates from the dedicated CA are also
  required for SQL access. ClickHouse resolves Keeper names to IPs before TLS
  verification, so DNS-only node certificates are insufficient. Do not distribute the CA private key to hosts.

There are no default passwords. Client accounts are `stack_admin` and
`stack_reader`; the latter enforces `readonly=1`. The default ClickHouse user
is absent. Users are password authenticated in addition to mTLS. Inter-server
fetches require a separate password; distributed queries use a cluster secret.
Keeper client traffic and Raft are TLS enabled. TLS verification is strict;
never disable it to resolve a certificate issue. A shared CA is a trust boundary:
any certificate it issues is trusted for transport, so isolate CA issuance.
Review the upstream [native-client hostname verification limitation](https://github.com/datopsis/clickhouse-ubi/blob/975d2818ec4f3f295fb5012aeb44cf83cc94a058/docs/TLS.md).

## Install and run

Controller: Linux, Python 3.13, AWS Session Manager plugin, OpenSSL, and access
to the approved AWS environment. The controller's credentials and policies
are provided by the operator; never put them in inventory. No SSH key or SSH
security-group rule is created. Read [architecture and responsibilities](docs/ARCHITECTURE.md)
before using the playbooks.

```sh
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r .github/requirements/tools.txt
ansible-galaxy collection install -r ansible/requirements.yml
```

Commands for a future operator-run deployment (these have **not** been run
against AWS):

```sh
ansible-playbook ansible/playbooks/provision.yml -e @/secure/site.yml --check
ansible-playbook ansible/playbooks/provision.yml -e @/secure/site.yml
ansible-playbook -i inventory.generated.yml ansible/playbooks/deploy.yml \
  -e @/secure/site.yml -e @/secure/vault.yml --ask-vault-pass -e '{"stack_bootstrap": true}'
```

Provisioning check mode can read AWS APIs when used by an operator. The local
CI command below replaces **all** AWS modules with offline test doubles.
A real check run cannot predict IDs of not-yet-created groups; dependent
security-group reconciliation is skipped in that case. Check mode is not an
AWS apply or IAM validation.

For subsequent deployments omit bootstrap (or set it to YAML `false`). Serial
batches stop on the first failure. Existing quorum is checked before each
Keeper change; cluster queries and replication health gate each replica
change. Bootstrap starts all Keepers before checking quorum, then starts the
replicas. A partial bootstrap requires diagnosis and explicit recovery; never
turn off checks to make a rerun proceed. Unchanged templates do not trigger
recreation. Compose is enabled at boot through a systemd unit.

```sh
ansible-playbook -i inventory.generated.yml ansible/playbooks/deploy.yml \
  -e @/secure/site.yml -e @/secure/vault.yml --ask-vault-pass
ansible-playbook -i inventory.generated.yml ansible/playbooks/verify.yml -e @/secure/site.yml
ansible-playbook -i inventory.generated.yml ansible/playbooks/verify.yml \
  -e @/secure/site.yml -e '{"stack_verify_writes": true}'
```

Read-only verification checks a single Keeper leader, followers, remote
read-only queries, and existing table replica/session/queue health. It does
not prove replication on an empty cluster. The write-enabled verification
creates a unique `default.stack_probe_*` ReplicatedMergeTree table on both
replicas, inserts a row, synchronizes and reads each replica, then drops the
table. Failed runs may leave a probe table for diagnosis. Verification and
rolling updates require a quiet, fully caught-up cluster; pending queues fail
closed. It is conservative, not an availability SLA.

Reprovisioning refuses duplicate identities, stopped VMs, or changes to AMI,
instance type, subnet or private IP. It never implicitly replaces a stateful
host. Configuration/image changes use the deployment playbook; VM/storage
replacement, automated upgrades and rollback remain separate roadmap work.
Data lives on the encrypted retained root EBS volume in this increment.
Retaining a volume is **not a backup**, and a new VM does not automatically
reattach an old volume.

## Local and native CI checks

```sh
bash scripts/check.sh
bash scripts/install-gitleaks.sh .local/bin
bash scripts/scan-secrets.sh .local/bin/gitleaks
```

Tools and Compose dependencies have `.in` inputs and hash-locked `.txt`
outputs, including `filelock==4.2.9`. Regenerate tools with Python 3.13 and the
runtime lock for Python 3.11; review all changes. `amazon.aws==12.1.0` is pinned.
CI uses immutable action SHAs, read-only repository permissions, no persisted
checkout credentials, and no secrets or AWS permissions.

CI jobs: `lint` (ansible-lint, yamllint, zizmor, rendering/security contracts,
mocked provisioning); `secrets` (gitleaks history and working tree); native
`functional (amd64)` and `functional (arm64)` (Podman image build, actual
five-node Compose topology, TLS/authentication refusal tests, replication,
replica failover and recovery, Keeper failure, Compose idempotence and clean
shutdown).
A development CI result does not qualify the RHEL/SELinux host path. The
GitHub Ubuntu 24.04 runner's distro Podman is a separate compatibility test;
deployment requires Podman 5.3 or newer on the chosen RHEL 9 AMI.

For local functional testing, build the pinned image source as CI does, install
`.github/requirements/runtime.txt` with `--require-hashes`, then run
`sudo env PATH="$PWD/.venv/bin:$PATH" .venv/bin/python tests/functional.py IMAGE_ID`.
The script generates disposable credentials, removes the Compose topology on
exit, and never contacts AWS. A sandbox may block container namespaces or
networking; report that limitation and use native CI, not a Docker substitute.

See [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md), and the
[roadmap](docs/ROADMAP.md). Licensed under [Apache-2.0](LICENSE).
