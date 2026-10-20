FROM python:3.11-slim AS runner

WORKDIR /app

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000 \
    HOST=0.0.0.0 \
    DATA_DIR=/app/data

# Install curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY app/ /app/app/

# Create data directory and non-root user
RUN mkdir -p /app/data && \
    useradd -u 10001 -m -s /bin/sh appuser && \
    chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f "http://127.0.0.1:${PORT:-8000}/health" || exit 1

CMD ["sh", "-c", "umask 077; exec python -m uvicorn app.main:app --host \"${HOST:-0.0.0.0}\" --port \"${PORT:-8000}\""]
