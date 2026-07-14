FROM python:3.12.10-slim-bookworm@sha256:fd95fa221297a88e1cf49c55ec1828edd7c5a428187e67b5d1805692d11588db

LABEL org.opencontainers.image.source="https://github.com/ccawmiku/hermes-supergrok-gateway"
LABEL org.opencontainers.image.description="Password-protected SuperGrok OAuth gateway for OpenAI and Anthropic clients"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.version="1.0.4"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SUPERGROK_OPENAI_HOME=/data

WORKDIR /app

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY pyproject.toml README.md LICENSE UPSTREAM.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir --no-deps . \
    && addgroup --system gateway \
    && adduser --system --ingroup gateway gateway \
    && mkdir -p /data \
    && chown gateway:gateway /data

USER gateway
VOLUME ["/data"]
EXPOSE 8645

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8645/health', timeout=2).read()"]

CMD ["supergrok-openai", "serve", "--host", "0.0.0.0", "--port", "8645", "--allow-network", "--no-browser"]
