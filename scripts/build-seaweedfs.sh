#!/usr/bin/env bash
# Same locked acquisition and offline assembly gates as the pinned upstream CI.
set -euo pipefail
source_dir=${1:?source checkout required}
arch=${2:?native architecture required}
test "$(git -C "$source_dir" rev-parse HEAD)" = fd9b0001ee86a432faf773d40ab6f528dbfdad67
case "$(uname -m):$arch" in x86_64:amd64|aarch64:arm64) ;; *) exit 1 ;; esac
export CONTAINER_RUNTIME=podman IMAGE=localhost/seaweedfs-stack-ci:build
python3 "$source_dir/.github/scripts/check_artifact_lock.py" "$source_dir/artifacts/seaweedfs.lock.json"
bash "$source_dir/scripts/fetch-artifacts.sh" "$arch"
bash "$source_dir/scripts/fetch-base-images.sh"
bash "$source_dir/scripts/build-image.sh" "$arch"
test "$(podman image inspect "$IMAGE" --format '{{.Architecture}}')" = "$arch"
