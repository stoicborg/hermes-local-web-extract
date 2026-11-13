#!/usr/bin/env bash
# End-to-end: clean Hermes in a container + real sidecar (+ optional Camofox) + real web pages.
#   bash tests/e2e/run.sh
# Needs: Docker, internet. Builds the sidecar image and the Hermes image if missing.
# Camofox: set E2E_CAMOFOX_IMAGE=<your camofox-browser image> to start a throwaway one on an isolated network;
#          otherwise tier-2 checks are reported as SKIP.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
HERMES_REF="${HERMES_REF:-v0.23.0}"
IMAGE="${E2E_IMAGE:-hermes-local-web-extract-e2e:${HERMES_REF}}"
NET=el-e2e-$$
RES="$HERE/results/run-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$RES"
cleanup() { docker rm -f "el-sidecar-$$" "el-camofox-$$" >/dev/null 2>&1; docker network rm "$NET" >/dev/null 2>&1; }
trap cleanup EXIT

docker image inspect local-web-extract-sidecar:0.1.0 >/dev/null 2>&1 || bash "$REPO/sidecar/build.sh"
docker image inspect "$IMAGE" >/dev/null 2>&1 || bash "$HERE/build.sh"
docker network create "$NET" >/dev/null

docker run -d --rm=true --name "el-sidecar-$$" --network "$NET" --init --read-only --tmpfs /tmp:size=64m \
  --cap-drop ALL --security-opt no-new-privileges --memory 768m --pids-limit 256 local-web-extract-sidecar:0.1.0 >/dev/null
CAMOFOX_ENV=()
if [ -n "${E2E_CAMOFOX_IMAGE:-}" ]; then
  docker run -d --rm=true --name "el-camofox-$$" --network "$NET" --shm-size 256m --memory 2g \
    -e CAMOFOX_PORT=9377 --tmpfs /data:size=512m,mode=1777 "$E2E_CAMOFOX_IMAGE" >/dev/null
  CAMOFOX_ENV=(-e "CAMOFOX_URL=http://el-camofox-$$:9377")
  sleep 8
fi
sleep 3
echo "results: $RES"
docker run --rm=true --network "$NET" \
  -v "$REPO":/repo:ro -v "$HERE/run_inside.py":/opt/harness/run_inside.py:ro -v "$RES":/out \
  -e "LOCAL_WEB_EXTRACT_SIDECAR_URL=http://el-sidecar-$$:8080" "${CAMOFOX_ENV[@]}" \
  "$IMAGE" python /opt/harness/run_inside.py 2>&1 | tee "$RES/container_stdout.txt"
exit "${PIPESTATUS[0]}"
