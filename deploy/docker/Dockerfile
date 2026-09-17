FROM python:3.12-slim

ARG UID=990
ARG GID=990

# iproute2: Tailscale IP detection via `ip -j` inside a host-network container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends iproute2 \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid "$GID" svcstatus \
 && useradd --uid "$UID" --gid "$GID" --no-create-home --shell /usr/sbin/nologin svcstatus

COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable && chmod -R a+rX /app

ENV PATH="/app/.venv/bin:$PATH" \
    SSA_CONFIG=/data/config.toml \
    SSA_LOG_STDERR=1 \
    CREDENTIALS_DIRECTORY=/run/credentials

USER svcstatus
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=6s --start-period=45s --retries=3 \
  CMD ["service-status-aggregator", "healthcheck"]
ENTRYPOINT ["service-status-aggregator"]
CMD ["run"]
