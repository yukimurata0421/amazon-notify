FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY amazon_notify ./amazon_notify
COPY config.example.json config.full.example.json ./

RUN pip install --upgrade pip && pip install .

ENTRYPOINT ["amazon-notify"]
CMD ["--help"]
