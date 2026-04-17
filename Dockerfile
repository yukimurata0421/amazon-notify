FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY pyproject.toml README.md LICENSE ./
COPY amazon_notify ./amazon_notify

RUN pip install --upgrade pip build && python -m build --wheel

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 amazon-notify \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin amazon-notify

COPY --from=builder /build/dist/*.whl /tmp/
COPY config.example.json config.full.example.json ./

RUN pip install --upgrade pip && pip install /tmp/*.whl && rm -f /tmp/*.whl

USER amazon-notify

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD amazon-notify --config /app/config.json --health-check >/dev/null 2>&1 || exit 1

ENTRYPOINT ["amazon-notify"]
CMD ["--help"]
