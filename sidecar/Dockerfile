# local-web-extract sidecar (tier 1). All versions pinned.
FROM node:22-slim AS js
WORKDIR /build
RUN npm install --no-audit --no-fund --omit=dev jsdom@31.0.1 @mozilla/readability@0.6.0

FROM python:3.12-slim
COPY --from=js /usr/local/bin/node /usr/local/bin/node
RUN pip install --no-cache-dir trafilatura==2.3.1 curl_cffi==0.17.0 pypdf==7.1.0
WORKDIR /app
COPY --from=js /build/node_modules /app/node_modules
COPY readability_cli.js app.py /app/
USER 65534:65534
EXPOSE 8080
CMD ["python", "-u", "/app/app.py"]
