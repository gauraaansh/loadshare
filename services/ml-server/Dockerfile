FROM python:3.11-slim

# System deps for xgboost and joblib
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cache — only rebuilds on requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (excludes training/ — not needed at runtime)
COPY main.py schemas.py loader.py inference.py ./

# Models directory — populated by training scripts on the host,
# mounted as a read-only volume at runtime
RUN mkdir -p /app/models

EXPOSE 8002

# Healthcheck — Docker marks the container healthy before the MCP server
# tries to connect. 15s start period covers model loading time (~3-5s).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8002/health || exit 1

CMD ["uvicorn", "main:app", \
     "--host", "0.0.0.0", \
     "--port", "8002", \
     "--workers", "2", \
     "--log-level", "info"]
