# StillHere backend image (FastAPI + the three in-process workers).
#
#   docker build -t stillhere-backend .
#   docker compose up --build          (backend + TimescaleDB + migrate step; see README.md)
#
# Configuration comes from the environment at run time (compose passes the backend
# env file with env_file). Nothing secret is baked into the image; .dockerignore keeps
# local env files, virtualenvs and database data out of the build context.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# Runtime dependencies only: the test tools, the local test Postgres (pgserver) and the
# USB-serial library (pyserial, used by scripts/bridge.py on the laptop) stay out of the image.
COPY backend/requirements.txt /tmp/requirements.txt
RUN grep -vE '^(pytest|pytest-asyncio|pgserver|pyserial)==' /tmp/requirements.txt > /tmp/runtime.txt \
 && pip install -r /tmp/runtime.txt \
 && rm /tmp/requirements.txt /tmp/runtime.txt \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin stillhere

COPY backend/app ./app
COPY backend/migrations ./migrations
COPY backend/scripts ./scripts

USER stillhere
EXPOSE 8000

# 200 only when the database answers (GET /v1/health returns 503 otherwise).
HEALTHCHECK --interval=10s --timeout=4s --start-period=30s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/v1/health' % os.environ.get('PORT', '8000'), timeout=3)"

# One process, no reloader: the listener is this process (scripts/run.py).
CMD ["python", "scripts/run.py"]
