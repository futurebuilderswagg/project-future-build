FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir -r /app/requirements.txt

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data \
    && chown app:app /app/data

# Токен и база подключаются при запуске контейнера.
COPY main.py test_template.xlsx /app/
COPY impulse /app/impulse
COPY catalog /app/catalog
COPY assets /app/assets

USER app:app
WORKDIR /app/data

CMD ["python", "/app/main.py"]
