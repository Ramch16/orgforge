# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm
RUN apt-get update -o APT::Update::Error-Mode=any && apt-get install -y --no-install-recommends git chromium ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE NOTICE.md ./
COPY vittics_builder ./vittics_builder
RUN --mount=type=secret,id=build_ca \
    if [ -f /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install --no-cache-dir '.[mcp,browser]' \
    && chmod -R a+rX /app \
    && useradd --uid 10001 --create-home vittics-builder \
    && mkdir -p /var/lib/vittics-builder && chown vittics-builder:vittics-builder /var/lib/vittics-builder
ENV VITTICS_HOME=/var/lib/vittics-builder VITTICS_BROWSER_EXECUTABLE=/usr/bin/chromium PYTHONUNBUFFERED=1
USER vittics-builder
VOLUME /var/lib/vittics-builder
EXPOSE 4700
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:4700/healthz',timeout=3)"
CMD ["python", "-m", "vittics_builder.hosting"]
