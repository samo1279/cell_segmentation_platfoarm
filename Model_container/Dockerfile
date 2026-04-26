# Code-only layer — built every commit, pulls in seconds.
# The heavy base (CUDA torch + weights) is in Dockerfile.base, tagged :stable.
# The GPU node already has :stable cached, so only this thin diff is transferred.
ARG BASE_IMAGE=localhost:32000/cellpose-poc-model-base:stable
FROM ${BASE_IMAGE}

# Re-copy only the application source — everything else is inherited from base.
COPY cellpose_api/app.py .
COPY cellpose_api/tasks.py .

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1
# --timeout-keep-alive 620: keep the TCP connection alive for 620 s so it
# outlasts the 900 s read timeout in the Gradio app minus connection overhead.
# Without this, uvicorn's default 5 s keep-alive drops idle connections mid-inference.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "620"]
