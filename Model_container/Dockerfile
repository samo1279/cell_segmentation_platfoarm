# Runtime image for the FastAPI / Cellpose model service.
#
# Local development (default): builds from python:3.11-slim and installs all
# requirements including CPU-only PyTorch.
#
# CI/CD (fast path): pass BASE_IMAGE=<registry>/cellpose-poc-model-base:stable
# and INSTALL_REQUIREMENTS=false to reuse the prebuilt CUDA/Cellpose/weights
# layer. Only the thin COPY app.py layer is rebuilt per commit.
ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

ARG INSTALL_REQUIREMENTS=true
ARG USE_CUDA=false

WORKDIR /app

COPY requirements.txt .

RUN if [ "${INSTALL_REQUIREMENTS}" = "true" ]; then \
      apt-get update && \
      apt-get install -y --no-install-recommends curl ca-certificates && \
      rm -rf /var/lib/apt/lists/* && \
      pip install --no-cache-dir -r requirements.txt && \
      if [ "${USE_CUDA}" = "true" ]; then \
        pip install --no-cache-dir --force-reinstall \
          torch==<FILL FROM pip freeze of base image>+cu121 \
          torchvision==<FILL FROM pip freeze of base image>+cu121 \
          torchaudio==<FILL FROM pip freeze of base image>+cu121 \
          --index-url https://download.pytorch.org/whl/cu121; \
      fi; \
    else \
      if ! command -v curl >/dev/null 2>&1; then \
        apt-get update && \
        apt-get install -y --no-install-recommends curl ca-certificates && \
        rm -rf /var/lib/apt/lists/*; \
      fi; \
    fi

# Re-copy only the application source — everything else is inherited from base.
COPY cellpose_api/app.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
# --timeout-keep-alive 620: keep the TCP connection alive for 620 s so it
# outlasts the 900 s read timeout in the Gradio app minus connection overhead.
# Without this, uvicorn's default 5 s keep-alive drops idle connections mid-inference.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "620"]
