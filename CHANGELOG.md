# Changelog

All notable changes to the Cell Segmentation Platform (POC v1) will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — Model Selector UI (2026-04-20)

### Added
- `App_container/app.py` — `gr.Radio` widget (`choices=["cyto3", "cpsam"]`, default `"cyto3"`) inserted in the left column below the `cellprob_thresh` slider, with an `info` string describing each model's speed/accuracy trade-off.

### Changed
- `App_container/app.py` — `segment()` signature extended with `model_type` parameter.
- `App_container/app.py` — `"model_type"` key added to the `form_data` dict posted to `POST /segment`.
- `App_container/app.py` — `submit_btn.click()` inputs list updated to include `model_choice`.

---

## [Unreleased] — Dual-Model Selection (2026-04-20)

### Added
- `Model_container/cellpose_api/app.py` — `MODELS` dict (`{"cyto3": None, "cpsam": None}`) at module level; both models loaded in parallel at startup via `asyncio.gather` + `loop.run_in_executor`. `MODEL` kept as a backward-compat alias pointing to `cyto3`.
- `Model_container/cellpose_api/app.py` — `POST /segment` now accepts `model_type: str = Form(default="cyto3")`. Validated against `MODELS` keys; returns 422 for unknown values. Logs the selected model before inference.
- `Model_container/cellpose_api/app.py` — `GET /parameters` response now includes a `model_type` field with `options: ["cyto3", "cpsam"]` and descriptions of each model's speed/accuracy trade-off.
- `Model_container/Dockerfile` — Weight-baking step now pre-downloads **both** `cyto3` and `cpsam` weights so neither requires a network fetch at container startup.

### Changed
- `Model_container/cellpose_api/app.py` — `GET /health` now returns `{"ok": true, "models": {"cyto3": true, "cpsam": true}, "gpu": ...}` instead of a single `model` string. Returns 503 if **either** model is still `None`.
- `Model_container/cellpose_api/app.py` — Inference path uses `MODELS[model_type].eval(...)` instead of the global `MODEL.eval(...)`.

---

## [Unreleased] — GPU Fix (2026-04-20)

### Fixed
- `Model_container/Dockerfile` — **critical bug**: CUDA torch was installed BEFORE `requirements.txt`, causing `pip install cellpose` to silently downgrade it back to the CPU wheel (cellpose lists `torch` as a PyPI dep, so pip overwrote the CUDA build). Fixed by installing `requirements.txt` first, then running `pip install --force-reinstall torch` with the CUDA index URL afterward. This guarantees the final torch in the image is always the CUDA-enabled version when `USE_CUDA=true`.
- `Model_container/Dockerfile` — weight-baking `RUN` step now passes `gpu=False` explicitly to `CellposeModel(...)`. Previously the `gpu=` kwarg was omitted, relying on Cellpose's default. Made explicit to document that build-time initialization is always CPU-only (Docker build never has GPU access) and the weight cache key is device-independent — the same cached weights are found and moved to the correct device at runtime when `USE_GPU=true` is injected.
- `docker-compose.yml` — `USE_GPU` was hardcoded to `false` and no build arg was passed, so the image was always built CPU-only and the model container was told to use CPU at runtime. Fixed: set `USE_GPU=true`, pass `USE_CUDA: "true"` build arg, add `deploy.resources.reservations.devices` NVIDIA GPU device reservation, raise memory limit to 8 G.

### Changed
- `docker-compose.yml` — GPU is now the default for `docker compose up --build` on any Linux server with an NVIDIA GPU + nvidia-container-toolkit. `docker-compose.gpu.yml` override is now redundant for standard deployments but kept for reference (it still provides `count: all` for multi-GPU hosts vs the single-GPU default in the base file).

### Added
- `docker-compose.cpu.yml` — new CPU override for macOS dev machines and GPU-less CI environments. Sets `USE_CUDA: "false"` build arg, `USE_GPU=false` env var, clears `deploy.resources.reservations.devices` to avoid requiring `nvidia-container-toolkit`, and lowers memory limit to 4G. Usage: `docker compose -f docker-compose.yml -f docker-compose.cpu.yml up --build`.

### Verified (healthcheck timings)
- `docker-compose.yml` healthcheck (`start_period: 90s`, `interval: 10s`, `retries: 15`) confirmed sufficient for GPU mode. cpsam loads in ~30–60s on GPU, so `/health` returns OK well before `start_period` expires. Total window = 240s; no change required.

---

## [Unreleased] — GPU Acceleration Support

### Added
- `docker-compose.gpu.yml` — Docker Compose override for Linux/NVIDIA GPU deployments. Sets `USE_CUDA=true` build arg, `USE_GPU=true` env var, and reserves all NVIDIA GPUs via `deploy.resources.reservations.devices`. Memory limit raised to 8 G for cpsam + activations.
- `Model_container/Dockerfile` — `ARG USE_CUDA=false` build argument. When `USE_CUDA=true`, installs PyTorch CUDA 12.1 wheels (`download.pytorch.org/whl/cu121`) before `requirements.txt` so cellpose picks up the CUDA-enabled torch. CPU build (default) is unchanged and works on macOS.
- `improved_system_design.md` — "GPU Acceleration" section documenting the `USE_GPU` code path, `USE_CUDA` build arg, host prerequisites (nvidia-container-toolkit), runtime verification command, and architecture impact.

### Changed
- Nothing in the API contract or two-container architecture.

---

## [Unreleased] — Phase 1 Complete (POC v1 Foundation)


### Fixed
- `Model_container/cellpose_api/app.py` — added `asyncio.Semaphore(1)` (`_INFER_SEM`) around `MODEL.eval()` to serialize concurrent inference requests. Without this, parallel `POST /segment` calls compete for the same CPU cores, causing memory-bandwidth thrashing that makes every request slower and more likely to timeout.
- `.gitlab-ci.yml` — increased segment smoke-test timeout from 120 s → 600 s. `cpsam` runs a ViT-H backbone; even a 64×64 image can take several minutes on CPU-only nodes.
- `App_container/app.py` — replaced `timeout=300.0` (uniform 5-minute timeout) with `httpx.Timeout(connect=10, write=60, read=900, pool=10)`. The uniform timeout was hitting the read phase during long CPU-only cpsam inference (5–15 min for microscopy images), causing "Segmentation timed out" even though the model was still running and would eventually complete.
- `Model_container/Dockerfile` — added `--timeout-keep-alive 620` to the uvicorn CMD. Uvicorn's default 5 s keep-alive was closing the idle TCP connection mid-inference, before the 900 s read timeout in the Gradio app could elapse.

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
