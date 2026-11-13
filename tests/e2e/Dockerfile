# Clean Hermes at a pinned ref + the e2e harness. Build via build.sh.
#   docker build -t hermes-local-web-extract-e2e:v0.23.0 --build-arg HERMES_REF=v0.23.0 tests/e2e
# Offline / fallback: provide a Hermes source tree as a named build context (see build.sh, HERMES_SRC=...).
ARG SRC=git
ARG HERMES_REF=v0.23.0

FROM python:3.14-slim AS src-git
ARG HERMES_REF
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch "${HERMES_REF}" https://github.com/NousResearch/hermes-agent /opt/hermes-src \
 && rm -rf /opt/hermes-src/.git

FROM scratch AS src-local
COPY --from=hermes-local / /opt/hermes-src

FROM src-${SRC} AS src

FROM python:3.14-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 HERMES_HOME=/root/.hermes
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=src /opt/hermes-src /opt/hermes-src
# Plain pip cannot resolve Hermes' pinned Pillow/cryptography together; upstream uses uv overrides, so mirror that.
# Hermes refuses non-editable wheel builds, so install editable.
RUN pip install uv && printf 'Pillow==13.0.0\ncryptography==50.0.2\n' > /opt/overrides.txt \
 && uv pip install --system --override /opt/overrides.txt -e "/opt/hermes-src" && hermes --version
