FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    TMPDIR=/var/lib/anker-cache

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --requirement requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /var/lib/anker-cache/authcache \
    && chown -R app:app /var/lib/anker-cache

COPY src ./src

FROM base AS test

COPY requirements-dev.txt ./requirements-dev.txt
RUN python -m pip install --no-cache-dir --requirement requirements-dev.txt
COPY pyproject.toml ./pyproject.toml
COPY tests ./tests
USER app
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]

FROM base AS runtime

USER app
CMD ["python", "-m", "solarbank_collector"]
