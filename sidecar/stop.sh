#!/usr/bin/env bash
# Stop and remove the sidecar container (the image is kept; run.sh starts it again).
NAME="${LOCAL_WEB_EXTRACT_NAME:-local-web-extract}"
if docker rm -f "$NAME" >/dev/null 2>&1; then echo "$NAME removed"; else echo "$NAME not present"; fi
