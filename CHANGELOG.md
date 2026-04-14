# Changelog

All notable changes to the Cell Segmentation Platform (POC v1) will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — Phase 1 Complete (POC v1 Foundation)

### Fixed
- `Model_container/cellpose_api/app.py` — added `asyncio.Semaphore(1)` (`_INFER_SEM`) around `MODEL.eval()` to serialize concurrent inference requests. Without this, parallel `POST /segment` calls compete for the same CPU cores, causing memory-bandwidth thrashing that makes every request slower and more likely to timeout.
- `.gitlab-ci.yml` — increased segment smoke-test timeout from 120 s → 600 s. `cpsam` runs a ViT-H backbone; even a 64×64 image can take several minutes on CPU-only nodes. The previous 120 s limit caused the verify job to report a failure even when the model was working correctly (the prior job succeeded because the command was guarded with `|| true`).

### Fixed (continued)
- `helm-chart/templates/deployment.yaml` — replaced brittle `initialDelaySeconds` hack with a proper K8s `startupProbe` (30 × 10 s = up to 5 min grace). While the startupProbe is pending, Kubernetes fully disables the liveness probe — the pod **cannot** be killed during model loading. readinessProbe and livenessProbe now have no `initialDelaySeconds`; they only begin after the startupProbe succeeds (`model-dev`)
- `Model_container/cellpose_api/app.py` — removed `channels=[0, 0]` from `MODEL.eval()` call; parameter is deprecated since Cellpose v4.0.1 and Cellpose v4 auto-detects channel layout from image shape (`model-dev`)
- `Model_container/cellpose_api/app.py` — loading `CellposeModel` directly in the async lifespan coroutine blocked the event loop for 60–90 s; all liveness/readiness probe requests timed out silently and Kubernetes killed the pod in an infinite restart loop. Fix: `await loop.run_in_executor(None, ...)` loads the model in a background thread so uvicorn keeps serving HTTP requests (returning `/health → 503`) during the entire load window (`model-dev`)
- `helm-chart/templates/deployment.yaml` — readiness `failureThreshold` raised from 6 → 12 (2 min of 503s tolerated after initial delay); liveness `initialDelaySeconds` raised from 30 → 120, `periodSeconds` 20 → 30, `timeoutSeconds: 5` added — liveness can no longer fire and kill the pod while the model is still loading (`model-dev`)
- `docker-compose.yml` — `start_period` 30 s → 90 s, `retries` 5 → 15, `interval` 15 s → 10 s, added `--max-time 5` to `curl` health check command so the health check properly waits for the model to finish loading before Docker Compose starts the app container (`model-dev`)

### Added
- `README.md` — project overview, quick start, usage guide, API reference, configuration, project structure, development guide, roadmap (`docs`)
- `App_container/app.py` — full Gradio Blocks UI: image upload, diameter/flow/cellprob sliders, `segment()` callback via `httpx`, colored overlay rendering (tab20 colormap, alpha 0.55), cell count summary, per-cell stats table (Cell ID, area px, area %), size distribution histogram, overlay PNG + `masks.npy` download buttons (`gradio-dev`)
- `App_container/requirements.txt` — `gradio`, `httpx`, `numpy`, `Pillow`, `matplotlib` (`gradio-dev`)
- `App_container/Dockerfile` — `python:3.11-slim`, port 8001 (`gradio-dev`)
- `GET /parameters` endpoint in `Model_container/cellpose_api/app.py` — returns JSON schema with type, default, min, max, description for all Cellpose parameters (`model-dev`)
- Input validation in `POST /segment`: 50 MB file size cap, PNG/TIFF/JPEG format whitelist, 8192×8192 max resolution, structured 422 responses (`model-dev`)
- `USE_GPU` environment variable in Model Container — replaces hardcoded `gpu=False` (`model-dev`)
- `curl` and `build-essential` in `Model_container/Dockerfile` for healthcheck support (`devops`)
- `HEALTHCHECK` directive in `Model_container/Dockerfile` — polls `GET /health` every 30s (`devops`)
- `improved_system_design.md` — full architecture spec with Mermaid diagrams, API contract, source code, docker-compose reference
- `.github/instructions/system-design.instructions.md` — workspace instruction enforcing 2-container architecture, API contract immutability, mandatory changelog entries
- `.github/agents/gradio-dev.agent.md` — Gradio UI developer agent with Gradio conventions and cross-agent handoffs
- `.github/agents/model-dev.agent.md` — Model Container developer agent with FastAPI/Cellpose conventions and cross-agent handoffs
- `.github/agents/devops.agent.md` — DevOps agent for Docker, networking, integration testing, and cross-agent handoffs
- `.github/agents/docs.agent.md` — Technical documentation agent for README, CHANGELOG, and design doc
- `.github/plan.md` — phased implementation plan (4 phases, 42 numbered steps)
- `CHANGELOG.md` — this file

### Changed
- `docker-compose.yml` — rewritten: renamed service `cellpose-api` → `model`, fixed build context from `.` to `./Model_container`, changed model from `ports: 8002:8000` to `expose: ["8000"]` (internal only), added `healthcheck`, added `depends_on: model: condition: service_healthy`, added `app` service on port 8001 (`devops`)
- `Model_container/cellpose_api/app.py` — `gpu` flag now reads `USE_GPU` env var; `/health` reports actual GPU state; errors split into 422 validation and 500 segmentation with structured `{"detail": "..."}` body (`model-dev`)
- `Model_container/Dockerfile` — added `curl`, `build-essential`, `HEALTHCHECK` directive (`devops`)
- `App_container/app.py` — uses `matplotlib.colormaps["tab20"]` (replaces deprecated `plt.cm.get_cmap`); handles grayscale (2-D) and RGBA (4-channel) inputs before overlay rendering (`gradio-dev`)

### Verified (Phase 1D — Integration Tests, 2026-04-07)
- `POST /segment` synthetic 256×256 random image → HTTP 200, masks shape (256, 256)
- `POST /segment` realistic 512×512 cell-like grayscale → HTTP 200, 6 cells detected
- `POST /segment` missing `image` field → 422 Unprocessable Entity
- `POST /segment` non-image file (text/plain) → 422 Unprocessable Entity
- `GET /health` → 200, `{"ok": true, "model": "cyto3", "gpu": false}`
- `GET /parameters` → 200, full schema with type/default/min/max/description for all 3 parameters
- Gradio UI at `http://localhost:8001` → HTTP 200
