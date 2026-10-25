ARG PYTHON_IMAGE=mirror.gcr.io/library/python:3.12-alpine@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d
FROM ${PYTHON_IMAGE}
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY miniapp/backend/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir --no-deps -r /srv/requirements.txt && pip check \
    && python -c "import sqlite3; assert sqlite3.sqlite_version_info >= (3, 51, 3)" \
    && mkdir -p /catalog /miniapp-data /miniapp-backups /data /config \
    && chown 10001:10001 /miniapp-data /miniapp-backups /data /config
COPY miniapp /srv/miniapp
USER 10001:10001
CMD ["uvicorn", "miniapp.backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--no-proxy-headers", "--limit-concurrency", "64", "--timeout-keep-alive", "5", "--h11-max-incomplete-event-size", "32768"]
