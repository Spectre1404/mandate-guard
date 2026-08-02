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

# SERVICE picks which app to run; PORT comes from the platform.
ENV SERVICE=ledger_ui \
    PORT=8080

COPY docker-entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 8080

# Exec form pointing at a shell SCRIPT: the script does the variable expansion, so
# it cannot be defeated by a platform that runs the command without a shell.
CMD ["/app/docker-entrypoint.sh"]
