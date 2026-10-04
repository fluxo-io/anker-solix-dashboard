FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    HOME=/tmp \
    TMPDIR=/tmp

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir --requirement requirements.txt

RUN groupadd --gid 10002 dashboard \
    && useradd --uid 10002 --gid dashboard --no-create-home --shell /usr/sbin/nologin dashboard

COPY assets ./assets
COPY src ./src

FROM base AS test

COPY requirements-dev.txt ./requirements-dev.txt
RUN python -m pip install --no-cache-dir --requirement requirements-dev.txt
COPY pyproject.toml ./pyproject.toml
COPY tests ./tests
USER dashboard
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"]

FROM base AS runtime

EXPOSE 8501
USER dashboard
CMD ["gunicorn", "--bind", "0.0.0.0:8501", "--workers", "2", "--threads", "4", "--timeout", "60", "solarbank_dashboard.app:server"]
