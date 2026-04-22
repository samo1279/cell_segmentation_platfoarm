FROM python:3.11-slim

# Set USE_CUDA=true at build time to install CUDA-enabled PyTorch wheels.
# CPU build is the default so the image builds on any machine (including macOS).
# GPU build targets CUDA 12.1; the NVIDIA driver on the host satisfies the
# remaining runtime dependency — no CUDA base image is required because the
# PyTorch whl bundles all necessary CUDA shared libraries.
ARG USE_CUDA=false

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .

# Install all dependencies first (cellpose pulls in CPU torch as a transitive dep).
RUN pip install --no-cache-dir -r requirements.txt

# THEN forcibly reinstall torch + torchvision together from the CUDA index.
# torchvision must be reinstalled alongside torch because cellpose→segment_anything
# depends on torchvision, and torchvision C++ extensions are built against a specific
# torch version. Replacing torch alone (e.g. 2.11.0→2.5.1+cu121) while leaving
# torchvision 0.26.0 (built for 2.11.0) causes "operator torchvision::nms does not exist"
# at import time. Reinstalling both from the same cu121 index guarantees a matched pair.
RUN if [ "$USE_CUDA" = "true" ]; then \
      pip install --no-cache-dir --force-reinstall torch torchvision \
        --index-url https://download.pytorch.org/whl/cu121; \
    fi

# Pre-download both model weights so they are baked into the image.
# gpu=False is explicit: Docker build never has GPU access, so there is no
# device to initialize on.  At runtime, CellposeModel(gpu=USE_GPU) finds the
# same cached weights by name and moves them to the correct device — the weight
# cache key is model name only and is device-independent.
# cyto3: U-Net backbone, fast. cpsam: ViT-H transformer, best accuracy.
# Single-line form avoids Kaniko misreading 'from cellpose import models' as a FROM instruction.
RUN python -c "from cellpose import models; models.CellposeModel(gpu=False, pretrained_model='cyto3'); print('cyto3 weights downloaded'); models.CellposeModel(gpu=False, pretrained_model='cpsam'); print('cpsam weights downloaded')"

COPY cellpose_api/app.py .
COPY cellpose_api/tasks.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
# --timeout-keep-alive 620: keep the TCP connection alive for 620 s so it
# outlasts the 900 s read timeout in the Gradio app minus connection overhead.
# Without this, uvicorn's default 5 s keep-alive drops idle connections mid-inference.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "620"]
