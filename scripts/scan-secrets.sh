#!/usr/bin/env bash
set -euo pipefail
scanner=${1:?path to gitleaks required}
"$scanner" git --redact --no-banner .
# Scan the working copies of every tracked/staged file, including local edits.
# Installed tools/collections are external inputs, not repository source.
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git ls-files -z | tar --null -T - -cf - | tar -xf - -C "$work"
"$scanner" dir --redact --no-banner "$work"
