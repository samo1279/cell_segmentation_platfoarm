# Changelog

All notable changes to the Cell Segmentation Platform (POC v1) will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased] — API Auth, Async Queue, 3-D Segmentation, GDPR Audit Log (2026-04-22)

### Added
- `Model_container/cellpose_api/tasks.py` (**new file**) — Celery application (`celery_app`) with a
  `run_segmentation` task (name `cellpose_tasks.run_segmentation`) that:
  - lazy-loads the requested Cellpose model on first call inside the worker process;
  - detects multi-frame TIFFs via `imageio.v3.improps` and segments each z-slice independently,
    stacking per-slice masks into a `(Z, H, W)` array (3-D z-stack support);
  - falls back to standard 2-D `model.eval()` for single-frame images;
  - returns masks serialised as a `.npy` binary blob (``bytes``) stored in the Celery result backend.
  - Reads `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND` env vars (default: `redis://redis:6379/0`).
  - Configured with pickle serialiser so large NumPy byte payloads round-trip correctly.
- `Model_container/cellpose_api/app.py` — `API_KEY` env var read at startup; `verify_api_key`
  FastAPI dependency (`X-API-Key` header): raises HTTP 401 if `API_KEY` is set and the header
  does not match; silently passes through when `API_KEY` is unset (open dev mode).
- `Model_container/cellpose_api/app.py` — `GET /segment/{job_id}` endpoint: polls the Celery
  result backend; returns HTTP 202 + `{"status": "pending"|"started"|"retry", "job_id": "..."}` while
  the task is running; returns `application/octet-stream` masks.npy on success; raises HTTP 500 on
  task failure.
- `Model_container/cellpose_api/app.py` — `audit_log` table DDL executed at startup
  (`CREATE TABLE IF NOT EXISTS`): columns `id SERIAL PRIMARY KEY`, `action TEXT NOT NULL`,
  `image_hash TEXT NOT NULL` (SHA-256 of raw image bytes — no filename, GDPR-safe),
  `timestamp TIMESTAMPTZ DEFAULT NOW()`.
- `Model_container/cellpose_api/app.py` — On every `POST /segment` call, inserts a row into
  `audit_log` (best-effort; never aborts the request on DB failure).
- `Model_container/cellpose_api/app.py` — On startup, executes
  `DELETE FROM projects WHERE timestamp < NOW() - INTERVAL '30 days'` to auto-purge stale records
  (GDPR data-minimisation requirement).
- `Model_container/requirements.txt` — Added `celery[redis]` and `redis`.
- `docker-compose.yml` — `redis` service: `redis:7-alpine`, internal-only (`expose: ["6379"]`),
  `redis-cli ping` healthcheck.
- `docker-compose.yml` — `celery_worker` service: same image as `model`, command
  `celery -A tasks.celery_app worker --loglevel=info --concurrency=1`, depends on `db` + `redis`,
  GPU reservation, 8 G memory limit.
- `Model_container/Dockerfile` — `COPY cellpose_api/tasks.py .` so the worker image includes the
  task definitions.

### Changed
- `Model_container/cellpose_api/app.py` — `POST /segment`: now a **202 Accepted** endpoint;
  validates the image and enqueues `run_segmentation.delay(...)` via Celery instead of running
  `model.eval()` inline; returns `{"job_id": "<celery-task-id>"}`.  Synchronous inference and
  direct mask streaming have been moved to the Celery worker.
- `Model_container/cellpose_api/app.py` — `POST /segment` and `GET /segment/{job_id}` and
  `GET /projects` now require the `X-API-Key` header when `API_KEY` env var is set (protected by
  `Depends(verify_api_key)`).  `GET /health` and `GET /parameters` remain unauthenticated.
- `docker-compose.yml` — `model` service: added `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, and
  `API_KEY` env vars; added `redis` to `depends_on` (condition: service_healthy).

## [Unreleased] — History Tab (2026-04-22)

### Added
- `App_container/app.py` — `MODEL_PROJECTS_URL` constant: derives base URL from `MODEL_URL` env var with `/segment` replaced by `/projects`.
- `App_container/app.py` — `load_history()` function: calls `GET /projects` on the Model Container with a 10 s timeout; parses a JSON list of `{id, image_name, model, cell_count, timestamp}` objects into Dataframe rows; returns an empty list on any network or HTTP error.
- `App_container/app.py` — **History tab** (`gr.Tab("History")`): `gr.Dataframe` with columns ID, Image name, Model, Cell count, Timestamp; "Refresh" button wired to `load_history()`; "Load Selected" button placeholder present for future wiring.

### Changed
- `App_container/app.py` — `gr.Tabs()` block now has three tabs: "Single Image", "Batch", "History".

---

## [Unreleased] — Fix History tab empty (2026-04-22)

### Fixed
- `App_container/app.py` — `load_history()` was reading wrong JSON keys (`image_name`, `model`) from `GET /projects`; corrected to `image_filename` and `model_used` matching the actual API response.
- `helm-chart/templates/deployment.yaml` — Added `DATABASE_URL` env var to the model container so `psycopg2` can connect to PostgreSQL in the cluster; previously missing, causing `_get_db_conn()` to return `None` and `/projects` to always return 503.
- `helm-chart/templates/deployment.yaml` — Added PostgreSQL `Deployment` (postgres:16-alpine) with readiness probe.
- `helm-chart/templates/services.yaml` — Added PostgreSQL `ClusterIP` Service (`{{ .Release.Name }}-db:5432`).
- `helm-chart/values.yaml` — Added `db:` block with image, port, name, user, password.

## [Unreleased] — Phase 3: Persistence & Annotation Infrastructure (2026-04-22)

### Added
- `docker-compose.yml` — `db` service: `postgres:16-alpine`, internal-only (`expose: ["5432"]`), `pg_isready` healthcheck, `postgres_data` volume mount.
- `docker-compose.yml` — `cvat` service: `cvat/server:latest`, internal-only (`expose: ["8080"]`), depends on `db`, mounts `images_volume`.
- `docker-compose.yml` — Named volumes: `postgres_data`, `images_volume`, `results_volume`.
- `docker-compose.cpu.yml` — Mirrored `db`, `cvat`, and volume definitions for CPU/macOS dev environments.
- `Model_container/cellpose_api/app.py` — `_get_db_conn()` helper: reads `DATABASE_URL` env var via `python-dotenv`, maintains a module-level `psycopg2` singleton with auto-reconnect; returns `None` gracefully when `DATABASE_URL` is unset so the container still works in local/test mode without a database.
- `Model_container/cellpose_api/app.py` — `projects` table DDL executed at startup (`CREATE TABLE IF NOT EXISTS`): columns `id SERIAL PRIMARY KEY`, `project_name TEXT`, `image_filename TEXT`, `timestamp TIMESTAMPTZ DEFAULT NOW()`, `model_used TEXT`, `cell_count INT`, `mask_path TEXT`.
- `Model_container/cellpose_api/app.py` — `GET /projects` endpoint: returns last 100 rows from `projects` ordered by `timestamp DESC` as a JSON array; returns HTTP 503 with descriptive message when no database is configured.
- `Model_container/cellpose_api/app.py` — `POST /segment`: best-effort `INSERT INTO projects` after successful inference (filename, model name, cell count); DB failure never aborts the segmentation response.
- `Model_container/requirements.txt` — Added `psycopg2-binary` and `python-dotenv`.
- `Model_container/cvat_serverless/function.py` — Nuclio serverless function (~100 lines): `init_context` / `handler` entry points; decodes base64 image from CVAT event body, POSTs to `http://model:8000/segment`, converts returned `masks.npy` to CVAT polygon annotation format via `skimage.measure.find_contours`; all parameters configurable via env vars.
- `Model_container/cvat_serverless/nuclio.yaml` — Nuclio function descriptor: `python:3.9-slim` base image, builds with `requests`, `numpy`, `scikit-image`; 64 MB max request body; 300 s event timeout; 2 HTTP workers; env vars forwarded to function.

### Changed
- `docker-compose.yml` — `model` service: added `DATABASE_URL` env var, `images_volume` and `results_volume` mounts, `depends_on: db`.
- `docker-compose.yml` — `app` service: added `images_volume` and `results_volume` mounts.
- `docker-compose.cpu.yml` — `model` service: added `DATABASE_URL` env var to match base file.

## [Unreleased] — Phase 2A + Phase 2C: Batch Processing & UX Improvements (2026-04-20)

### Added
- `App_container/app.py` — **Batch tab** (`gr.Tab("Batch")`): multi-file upload via `gr.File(file_count="multiple", file_types=["image"])`, same 3 sliders + model radio as Single tab, "Run Batch" button.
- `App_container/app.py` — `batch_segment()` function: loops over uploaded files, calls `_call_model()` per image with `gr.Progress` reporting, collects per-image results.
- `App_container/app.py` — Batch summary `gr.Dataframe` with columns: Filename, Model, Cell count, Mean area (px), Time (s).
- `App_container/app.py` — ZIP download (`gr.File`) in Batch tab — packages all overlay PNGs under `overlays/` and all masks `.npy` files under `masks/` inside the ZIP.
- `App_container/app.py` — **Overlay Opacity slider** (`gr.Slider(0.1, 1.0, value=0.55)`) in both Single Image and Batch tabs; wired into `_render_overlay()` (previously hardcoded as `0.55`).
- `App_container/app.py` — **Download Statistics (CSV)** button in Single Image tab; triggers `export_csv()` which calls `pandas.DataFrame.to_csv()` and returns a temp file.
- `App_container/app.py` — Shared helper functions extracted: `_encode_png()`, `_call_model()`, `_render_overlay()`, `_compute_stats()` — removes code duplication between single and batch paths.
- `App_container/requirements.txt` — Added `pandas` (explicit, was previously a transitive dep of Gradio; now required directly by `export_csv()`).

### Changed
- `App_container/app.py` — `segment()` signature gains `opacity` parameter (replaces hardcoded `0.55`).
- `App_container/app.py` — `submit_btn.click()` inputs list updated to include `opacity_slider`.
- `App_container/app.py` — UI wrapped in `gr.Tabs()` with "Single Image" and "Batch" tabs.
- `App_container/app.py` — `_pending_batch_cleanup` global tracks batch temp files for deferred deletion (mirrors existing `_pending_cleanup` pattern for single-image path).

---

## [Unreleased] — Manual Integration Test Script (2026-04-20)

### Added
- `tests/integration_test.py` — Manual end-to-end integration test script. Verifies: `GET /health` returns `ok: true`; Gradio UI reachable at `http://localhost:8001`; `POST /segment` with `001_img.png` using both `cyto3` and `cpsam` returns HTTP 200, valid `.npy` masks, non-zero cell count, and `X-Model-Used` header. Prints a per-model summary table (image, model, cell count, elapsed time). Configurable via `MODEL_URL`, `APP_URL`, `IMAGE_PATH`, `TIMEOUT` env vars. Designed to run inside the Docker network via `docker compose exec app python /tests/integration_test.py`.
- `tests/requirements-integration.txt` — Pinned dependencies for the integration test (`httpx`, `numpy`, `Pillow`). **Not wired into CI** — install manually with `pip install -r tests/requirements-integration.txt`.

---

## [Unreleased] — Fix TestClient lifespan compatibility (2026-04-20)

### Fixed
- `Model_container/tests/test_api.py` — Removed `lifespan="off"` from all `TestClient` instantiations; this kwarg was added in starlette 0.26+ and was absent in the CI image. Without a context manager, `TestClient` never triggers lifespan startup, achieving the same effect.

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
