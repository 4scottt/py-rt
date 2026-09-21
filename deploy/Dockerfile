# syntax=docker/dockerfile:1

# Build stage: uv installs the locked dependencies into /app/.venv, then the
# package itself. Dependencies first, in their own layer, so a code change
# does not re-resolve or re-download them.
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS build
WORKDIR /app

ENV UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.13 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=0

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# README.md is package metadata: hatchling reads it to build the wheel.
COPY README.md ./
COPY pyrt/ ./pyrt/
RUN uv sync --frozen --no-dev

# Runtime stage: the same Debian release the wheels were installed on, with
# no uv, no build tools and no package index cache.
FROM python:3.13-slim-bookworm AS runtime

LABEL org.opencontainers.image.source="https://github.com/4scottt/py-rt" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.title="py-rt" \
      org.opencontainers.image.description="A clean-room Python rewrite of Request Tracker's ticket core"

# The app runs as pyrt (1000:1000); /var/lib/py-rt is the only path it writes
# (MAIL_MODE=file), and it is created here so a mount inherits the owner.
RUN groupadd --gid 1000 pyrt \
 && useradd --uid 1000 --gid 1000 --home-dir /app --shell /usr/sbin/nologin pyrt \
 && install -d -o 1000 -g 1000 /var/lib/py-rt

WORKDIR /app
COPY --from=build --chown=1000:1000 /app/.venv /app/.venv
COPY --from=build --chown=1000:1000 /app/pyrt /app/pyrt

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Fails the build here rather than at the first deploy if the venv did not
# survive the copy (the interpreter it points at must exist in this stage).
RUN pyrt --version

USER pyrt
EXPOSE 8080

# No curl in the image: the CLI GETs /health on 127.0.0.1 with urllib.
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=3 \
  CMD ["pyrt", "healthcheck"]

ENTRYPOINT ["pyrt"]
CMD ["serve"]
