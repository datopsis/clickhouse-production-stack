#!/usr/bin/env bash
set -euo pipefail
# Official release archive, pinned checksum. Native Linux amd64 scanner job.
dest=${1:?destination directory required}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
  https://github.com/gitleaks/gitleaks/releases/download/v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz \
  -o "$work/gitleaks.tar.gz"
printf '%s  %s\n' 551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb "$work/gitleaks.tar.gz" | sha256sum --check
mkdir -p "$dest"
tar -xzf "$work/gitleaks.tar.gz" -C "$dest" gitleaks
