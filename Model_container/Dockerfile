# Self-contained build — works on any machine (local Mac or server).
#
# Build ARG:
#   USE_CUDA=false (default) — installs CPU-only PyTorch. Use this for local dev.
#   USE_CUDA=true            — replaces CPU wheels with CUDA 12.1 wheels.
#                              Use this when building the image for the GPU server.
#
# Local (Docker Compose, macOS):
#   docker compose up --build            # USE_CUDA defaults to false
#
# Server image build (before helm deploy):
#   docker build --build-arg USE_CUDA=true -t <registry>/cellpose-poc-model:latest .

FROM python:3.11-slim

ARG USE_CUDA=false

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Install all Python dependencies.
# cellpose pulls in CPU torch as a transitive dependency.
RUN pip install --no-cache-dir -r requirements.txt

# When USE_CUDA=true, reinstall torch and torchvision from the CUDA 12.1 index.
# This replaces the CPU-only wheels installed above with CUDA-enabled ones.
# torchvision must be reinstalled alongside torch to ensure matched CUDA versions.
RUN if [ "$USE_CUDA" = "true" ]; then \
      pip install --no-cache-dir --force-reinstall torch torchvision \
        --index-url https://download.pytorch.org/whl/cu121; \
    fi

# Pre-download Cellpose model weights and bake them into the image.
# gpu=False during build: Docker build never has GPU access; weights are device-independent.
RUN python -c "\
from cellpose import models; \
models.CellposeModel(gpu=False, pretrained_model='cyto3'); \
print('cyto3 weights downloaded'); \
models.CellposeModel(gpu=False, pretrained_model='cpsam'); \
print('cpsam weights downloaded')"

# Copy application source (thin layer — changes frequently).
COPY cellpose_api/app.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1

# --timeout-keep-alive 620: keeps TCP connections alive longer than Gradio's
# 900 s read timeout minus connection overhead. Without this, uvicorn's default
# 5 s keep-alive drops idle connections during long inference runs.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "620"]
