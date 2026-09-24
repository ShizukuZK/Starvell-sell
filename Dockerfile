# Образ для Railway / любого VPS с Docker
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    MALLOC_ARENA_MAX=2

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

# /data — постоянный том (Railway Volume или ./data в docker compose):
# настройки, привязки, аренды, логи. Без тома всё сбрасывается при перевыкладке.
RUN mkdir -p /data

# SIGTERM при перевыкладке — бот корректно сохраняет данные и отпускает замок
STOPSIGNAL SIGTERM
CMD ["python", "main.py"]
