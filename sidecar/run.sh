#!/usr/bin/env bash
# Build (if needed) and start the local-web-extract sidecar. Published on 127.0.0.1 only by default.
#   LOCAL_WEB_EXTRACT_PORT   host port            (default 18650)
#   LOCAL_WEB_EXTRACT_BIND   host bind address    (default 127.0.0.1; change only if you know why)
#   LOCAL_WEB_EXTRACT_NAME   container name       (default local-web-extract)
#   LOCAL_WEB_EXTRACT_REBUILD=1  force image rebuild
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
NAME="${LOCAL_WEB_EXTRACT_NAME:-local-web-extract}"
IMG="local-web-extract-sidecar:0.1.0"
PORT="${LOCAL_WEB_EXTRACT_PORT:-18650}"
BIND="${LOCAL_WEB_EXTRACT_BIND:-127.0.0.1}"

if [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null || true)" = "true" ]; then
  echo "$NAME already running"; exit 0
fi
if [ "${LOCAL_WEB_EXTRACT_REBUILD:-0}" = "1" ] || ! docker image inspect "$IMG" >/dev/null 2>&1; then
  docker build -t "$IMG" .
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --init --restart unless-stopped \
  -p "${BIND}:${PORT}:8080" \
  --read-only --tmpfs /tmp:size=64m \
  --cap-drop ALL --security-opt no-new-privileges \
  --memory 768m --pids-limit 256 \
  "$IMG" >/dev/null
for _ in $(seq 1 40); do
  if curl -fsS "http://${BIND}:${PORT}/health" >/dev/null 2>&1; then echo "up on ${BIND}:${PORT}"; exit 0; fi
  sleep 0.5
done
echo "health check failed" >&2; docker logs --tail 20 "$NAME" >&2; exit 1
