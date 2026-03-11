FROM python:3.11-slim

WORKDIR /app

# Install system deps for pdfplumber (poppler) and cryptography
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev && \
    rm -rf /var/lib/apt/lists/*

# Install Python deps (no ML deps — triage uses Grok API fallback)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY services/ services/
COPY static/ static/
COPY app.py .

# Create runtime directories
RUN mkdir -p data/tokens data/cache attachments

EXPOSE 8080

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
