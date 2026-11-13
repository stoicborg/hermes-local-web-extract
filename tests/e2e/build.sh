#!/usr/bin/env bash
# Build the e2e image: clean Hermes at HERMES_REF (default v0.23.0) fetched from GitHub inside the build.
# Fallback without GitHub access: HERMES_SRC=/path/to/hermes-agent-checkout bash build.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_REF="${HERMES_REF:-v0.23.0}"
IMAGE="${E2E_IMAGE:-hermes-local-web-extract-e2e:${HERMES_REF}}"
if [ -n "${HERMES_SRC:-}" ]; then
  docker build -t "$IMAGE" --build-arg SRC=local --build-context "hermes-local=$HERMES_SRC" "$HERE"
else
  docker build -t "$IMAGE" --build-arg HERMES_REF="$HERMES_REF" "$HERE"
fi
echo "built $IMAGE"
