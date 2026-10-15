FROM python:3.14-alpine@sha256:f6a589d43c42b9e7f7dc67a12d37132491f362859a5d750607710cc56da3bc72

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DB_PATH=/data/index-bridge.sqlite3

WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
COPY scripts ./scripts

RUN addgroup -S -g 10001 bridge \
    && adduser -S -D -H -u 10001 -G bridge -s /sbin/nologin bridge \
    && mkdir -p /data \
    && chown 10001:10001 /data

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read(64)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-access-log", "--limit-concurrency", "32", "--timeout-keep-alive", "5", "--h11-max-incomplete-event-size", "16384"]
