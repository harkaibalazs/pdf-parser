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

# Bind to all interfaces so the server is reachable from outside the container.
ENV HOST=0.0.0.0 \
    PORT=5000

EXPOSE 5000

CMD ["python", "webgui.py"]
