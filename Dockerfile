FROM python:3.12-slim

WORKDIR /srv/it-radar-bot

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY config.yaml .

# БД и логи — в примонтованный том /data
ENV DB_PATH=/data/bot.db CONFIG_PATH=/srv/it-radar-bot/config.yaml

CMD ["python", "-m", "app.main"]
