#!/usr/bin/env bash
# Build the sidecar image only.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
docker build -t local-web-extract-sidecar:0.1.0 .
