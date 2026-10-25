FROM python:3.12-slim
WORKDIR /srv
COPY miniapp/backend/requirements.txt /srv/requirements.txt
RUN pip install --no-cache-dir -r /srv/requirements.txt
COPY miniapp /srv/miniapp
USER 10001:10001
CMD ["uvicorn", "miniapp.backend.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
