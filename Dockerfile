FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY src/ ./src/
COPY migrations/ ./migrations/

RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 tg-jobs-searcher \
    && useradd --uid 10001 --gid 10001 --home-dir /var/lib/tg-jobs-searcher \
       --create-home --shell /usr/sbin/nologin tg-jobs-searcher

USER 10001:10001

CMD ["tg-jobs-searcher"]
