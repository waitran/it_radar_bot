FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS dependencies

WORKDIR /srv/it-radar-bot

COPY requirements.txt .
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m pip --isolated install --index-url https://pypi.org/simple --no-cache-dir --no-deps -r requirements.txt \
    && python -m pip check

FROM dependencies AS test
COPY app ./app
COPY config.yaml .
COPY tests ./tests
RUN python -m tests.test_rules && python -m unittest discover -s tests -v

FROM dependencies AS runtime
COPY --from=test /srv/it-radar-bot/app ./app
COPY --from=test /srv/it-radar-bot/config.yaml .
# SQLite lives in the mounted volume; logs go to stderr.
RUN mkdir -p /data /backups && chown 10001:10001 /data /backups && chmod 700 /data /backups
ENV DB_PATH=/data/bot.db CONFIG_PATH=/srv/it-radar-bot/config.yaml

ENV HOME=/tmp
USER 10001:10001

CMD ["python", "-m", "app.main"]
