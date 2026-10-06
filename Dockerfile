# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
RUN apt-get update -o APT::Update::Error-Mode=any && apt-get install -y --no-install-recommends git chromium ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE.md ./
COPY orgforge ./orgforge
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install --no-cache-dir '.[mcp,browser]' \
    && chmod -R a+rX /app \
    && useradd --uid 10001 --create-home orgforge \
    && mkdir -p /var/lib/orgforge && chown orgforge:orgforge /var/lib/orgforge
ENV ORGFORGE_HOME=/var/lib/orgforge ORGFORGE_BROWSER_EXECUTABLE=/usr/bin/chromium PYTHONUNBUFFERED=1
USER orgforge
VOLUME /var/lib/orgforge
EXPOSE 4700
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4700/healthz',timeout=3)"
CMD ["python", "-m", "orgforge.hosting"]
