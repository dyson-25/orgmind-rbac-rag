FROM python:3.12.3-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    APP_ENV=production \
    GENERATION_PROVIDER=openai \
    DATA_DIR=/app/data \
    TMPDIR=/app/data/runtime \
    PORT=8000 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1

WORKDIR /app

COPY requirements.txt ./

RUN pip install --no-cache-dir --default-timeout=300 --retries=10 -r requirements.txt \
    && groupadd --gid 10001 orgmind \
    && useradd --uid 10001 --gid orgmind --no-create-home --shell /usr/sbin/nologin orgmind \
    && mkdir -p /app/data/runtime \
    && chown -R orgmind:orgmind /app/data

COPY --chown=orgmind:orgmind app/ ./app/
COPY --chown=orgmind:orgmind static/ ./static/
COPY --chown=orgmind:orgmind samples/ ./samples/

USER orgmind

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c 'import json, os, urllib.request; response = urllib.request.urlopen("http://127.0.0.1:" + os.environ.get("PORT", "8000") + "/api/health", timeout=4); assert json.load(response)["status"] == "ok"' || exit 1

CMD ["sh", "-c", "exec python -m uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --workers 1"]
