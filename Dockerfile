FROM python:3.12-slim

# Tesseract is needed for the OCR option; without it the parser still runs
# but leaves ocr_text empty.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py webgui.py ./

EXPOSE 5000

# Served by gunicorn so multiple requests are handled concurrently.
# Tunables (with defaults): PORT, WEB_CONCURRENCY (workers), GUNICORN_THREADS,
# GUNICORN_TIMEOUT. The timeout is generous because a single request runs the
# parser synchronously and large PDFs can take a while. `exec` lets gunicorn
# receive SIGTERM directly for graceful shutdown.
CMD ["sh", "-c", "exec gunicorn --bind 0.0.0.0:${PORT:-5000} --workers ${WEB_CONCURRENCY:-4} --threads ${GUNICORN_THREADS:-2} --timeout ${GUNICORN_TIMEOUT:-600} webgui:app"]
