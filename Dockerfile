FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/app/data \
    DB_PATH=/app/data/bot.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py crawler.py database.py notifier.py bot.py ./

# 바인드 마운트한 ./data 를 그대로 쓸 수 있도록 호스트 기본 사용자와 같은 UID를 씁니다.
RUN useradd --create-home --uid 1000 app \
    && mkdir -p /app/data \
    && chown -R app:app /app
USER app

CMD ["python", "bot.py"]
