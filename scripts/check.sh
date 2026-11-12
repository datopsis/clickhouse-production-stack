#!/usr/bin/env bash
set -euo pipefail
yamllint .
ansible-lint --offline
zizmor --offline --min-severity low .github/workflows
python -m unittest discover -s tests -p 'test_*.py'
ANSIBLE_COLLECTIONS_PATH="$PWD/tests/mocks" AWS_EC2_METADATA_DISABLED=true \
  ansible-playbook ansible/playbooks/provision.yml --check -e @tests/provision-vars.yml
ANSIBLE_COLLECTIONS_PATH="$PWD/tests/mocks" AWS_EC2_METADATA_DISABLED=true STACK_MOCK_EXISTING=1 STACK_MOCK_MAIN_ROUTE=1 \
  ansible-playbook ansible/playbooks/provision.yml --check -e @tests/provision-vars.yml
ANSIBLE_COLLECTIONS_PATH="$PWD/tests/mocks" AWS_EC2_METADATA_DISABLED=true \
  ansible-playbook ansible/playbooks/provision.yml --check -e @tests/provision-vars.yml \
  -e '{"stack_backup_object_lock": true, "stack_backup_object_lock_mode": "GOVERNANCE", "stack_backup_object_lock_days": 30}'
ansible-playbook ansible/playbooks/deploy.yml --syntax-check
ansible-playbook ansible/playbooks/verify.yml --syntax-check
if ANSIBLE_COLLECTIONS_PATH="$PWD/tests/mocks" AWS_EC2_METADATA_DISABLED=true STACK_MOCK_PUBLIC_SUBNET=1 \
  ansible-playbook ansible/playbooks/provision.yml --check -e @tests/provision-vars.yml; then
  echo 'ERROR: provisioning accepted a public subnet' >&2
  exit 1
fi
echo 'PASS: offline provisioning refused a public subnet'
