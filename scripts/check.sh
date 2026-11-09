#!/usr/bin/env bash
set -euo pipefail
yamllint .
ansible-lint --offline
zizmor --offline --min-severity low .github/workflows
python -m unittest discover -s tests -p 'test_*.py'
ANSIBLE_COLLECTIONS_PATH="$PWD/tests/mocks" AWS_EC2_METADATA_DISABLED=true \
  ansible-playbook ansible/playbooks/provision.yml --check -e @tests/provision-vars.yml
ANSIBLE_COLLECTIONS_PATH="$PWD/tests/mocks" AWS_EC2_METADATA_DISABLED=true STACK_MOCK_EXISTING=1 \
  ansible-playbook ansible/playbooks/provision.yml --check -e @tests/provision-vars.yml
ansible-playbook ansible/playbooks/deploy.yml --syntax-check
ansible-playbook ansible/playbooks/verify.yml --syntax-check
