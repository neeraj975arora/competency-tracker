# Competency Tracker - production image served by gunicorn
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACKER_DB=/data/tracker.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py manage.py ./
COPY templates ./templates
COPY static ./static
COPY sample_profile.json claude_automation_analyst_profile.json ./

# Run as a non-root user; the database lives in a volume at /data.
RUN useradd --create-home appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/login', timeout=4)" || exit 1

# One worker with threads: each worker runs init_db at import, so several workers could race on first start.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "--access-logfile", "-", "app:app"]
