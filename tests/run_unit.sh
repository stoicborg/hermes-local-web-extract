#!/usr/bin/env bash
# Run the unit tests in a clean python container (repo mounted read-only; nothing is written back).
#   bash tests/run_unit.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
docker run --rm=true -v "$REPO":/src:ro python:3.12-slim sh -c \
  "pip install -q pytest 2>&1 | tail -1; mkdir /tmp/local_web_extract && cp -r /src/. /tmp/local_web_extract/ && cd /tmp/local_web_extract && python -m pytest -p no:cacheprovider"
