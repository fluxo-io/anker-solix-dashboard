FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src HOME=/tmp TMPDIR=/tmp
WORKDIR /app
RUN groupadd -g 1000 fluxo && useradd -u 1000 -g fluxo -M fluxo
COPY docker/entrypoint.py /app/docker/entrypoint.py

FROM base AS api
COPY api/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=fluxo:fluxo api/src/ ./src/
ENV DATA_DIR=/data
USER fluxo
EXPOSE 8001
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=3)"
CMD ["gunicorn", "--bind", "0.0.0.0:8001", "--workers", "2", "--threads", "4", "--timeout", "30", "solarbank_api.app:create_app()"]

FROM api AS api-tests
USER root
COPY api/requirements-dev.txt ./requirements-dev.txt
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY api/pyproject.toml ./pyproject.toml
COPY tests/api/ ./tests/api/
USER fluxo
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/api"]

FROM base AS web
COPY web/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=fluxo:fluxo web/src/ ./src/
COPY --chown=fluxo:fluxo web/assets/ ./assets/
USER fluxo
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -m solarbank_dashboard.healthcheck
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "60", "solarbank_dashboard.app:server"]

FROM web AS web-tests
USER root
COPY web/requirements-dev.txt ./requirements-dev.txt
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY web/pyproject.toml ./pyproject.toml
COPY tests/web/ ./tests/web/
USER fluxo
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/web"]

FROM base AS collector
COPY collector/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=fluxo:fluxo collector/src/ ./src/
ENV TMPDIR=/data
USER fluxo
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -m solarbank_collector.healthcheck
CMD ["python", "-m", "solarbank_collector"]

FROM collector AS collector-tests
ENV TMPDIR=/tmp
USER root
COPY collector/requirements-dev.txt ./requirements-dev.txt
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY collector/pyproject.toml ./pyproject.toml
COPY tests/collector/ ./tests/collector/
USER fluxo
ENTRYPOINT []
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/collector"]
