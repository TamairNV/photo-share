# Claude Code (for auto tagging). npm picks the right native build for this machine (x64 or arm64);
# only the single self-contained binary is copied into the app image, so no Node at runtime.
FROM node:22-slim AS claude
RUN npm install -g @anthropic-ai/claude-code \
 && cp -L "$(npm root -g)/@anthropic-ai/claude-code/bin/claude.exe" /claude && /claude --version

FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=claude /claude /usr/local/bin/claude
COPY app ./app
# HOME: Claude Code keeps a little state there, and the container user (PUID) has no home of its own
ENV DATA_DIR=/data PYTHONUNBUFFERED=1 HOME=/tmp DISABLE_AUTOUPDATER=1
EXPOSE 8000
HEALTHCHECK CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8000/healthz')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
