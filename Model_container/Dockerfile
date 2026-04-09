FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-download cyto3 model weights so they are baked into the image.
# This avoids runtime downloads inside the cluster and makes startup fast.
RUN python -c "from cellpose import models; models.CellposeModel(pretrained_model='cyto3'); print('cyto3 weights downloaded')"

COPY cellpose_api/app.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
