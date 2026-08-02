# One image, two services. Railway/Render run it twice with different commands.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MANDATE_GUARD_HOSTED=1 \
    MANDATE_GUARD_LEDGER_DIR=/data/ledgers \
    MANDATE_GUARD_EXPORT_DIR=/data/exports

WORKDIR /app

COPY requirements-hosted.txt .
RUN pip install --no-cache-dir -r requirements-hosted.txt

# Only what the hosted surfaces need. No spike, no tests, no dev scripts.
COPY backend/ backend/
COPY ledger_ui/ ledger_ui/
COPY storefront/ storefront/
COPY evidence/seeds/ evidence/seeds/
COPY evidence/sample/evidence-packet.pdf evidence/sample/
COPY evidence/sample/screenshots/dashboard.png evidence/sample/screenshots/

RUN mkdir -p /data/ledgers /data/exports

# $PORT is provided by the platform; SERVICE picks which app to run.
ENV SERVICE=ledger_ui
CMD ["sh", "-c", "uvicorn ${SERVICE}.app:app --host 0.0.0.0 --port ${PORT:-8000}"]
