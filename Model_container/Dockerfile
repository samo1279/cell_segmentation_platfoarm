FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download cpsam model weights so they are baked into the image.
# Cellpose v4 renamed the default model from cyto3 → cpsam; using the exact
# name ensures the baked cache key matches what runtime looks for.
RUN python -c "from cellpose import models; models.CellposeModel(pretrained_model='cpsam'); print('cpsam weights downloaded')"

COPY cellpose_api/app.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
# --timeout-keep-alive 620: keep the TCP connection alive for 620 s so it
# outlasts the 900 s read timeout in the Gradio app minus connection overhead.
# Without this, uvicorn's default 5 s keep-alive drops idle connections mid-inference.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "620"]
