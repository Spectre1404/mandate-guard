#!/bin/sh
# Boot one of the two hosted apps.
#
# A script, not an inline command: Railway (and Render) may run the start command
# without a shell, in which case ${PORT} and ${SERVICE} arrive as literal text and
# uvicorn rejects them. Running a shell script guarantees expansion happens
# wherever it is invoked from.
#
# SERVICE picks the app; PORT is supplied by the platform and defaults to 8080 so
# `docker run -p 8080:8080 <image>` works locally with no arguments.
set -e
exec uvicorn "${SERVICE:-ledger_ui}.app:app" --host 0.0.0.0 --port "${PORT:-8080}"
