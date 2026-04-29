# Chapter 3 — Methodology

---

## 3.2 System Architecture

### 3.2.1 Two-Container Design

The system is decomposed into exactly two application containers (Figure 3.1):

- **App Container** (`cellpose-poc-app`) — a Python 3.11 Gradio application served on port 8001. It handles all user interaction: file upload, parameter selection, result display, batch processing, and segmentation history.
- **Model Container** (`cellpose-poc-model`) — a Python 3.11 FastAPI service on port 8000. It owns all machine learning logic: model loading, inference, input validation, and persistence to PostgreSQL.

A third container, **PostgreSQL** (`postgres:16-alpine`), stores segmentation history and user accounts. It is not exposed outside the Kubernetes namespace.

This decomposition was chosen deliberately. Separating the UI from the inference engine means the heavy machine learning dependencies (PyTorch, Cellpose, and the ViT-H model weights totalling approximately 6–8 GB) are isolated in one image that can be rebuilt independently of UI changes. A typical UI code change triggers a Docker build that copies only two Python source files — the resulting image layer is approximately 5 MB, rather than several gigabytes.

```
Browser
   │  HTTPS (nginx Ingress, port 443)
   ▼
App Container  :8001  (Gradio + FastAPI middleware)
   │  HTTP POST /segment
   │  HTTP GET  /projects
   │  HTTP POST /auth/login, /auth/register
   ▼
Model Container  :8000  (FastAPI + Cellpose)
   │  SQL
   ▼
PostgreSQL  :5432
```

### 3.2.2 Internal Networking

Inside the Kubernetes namespace `cellpose-poc`, each container is reachable by a ClusterIP service using its service name as a DNS hostname. The Model Container is never exposed to the host network — the Kubernetes Service uses `ClusterIP` only. External traffic enters exclusively through an nginx Ingress controller with TLS termination provided by cert-manager.

The App Container resolves the Model Container address via the environment variable `MODEL_URL`, defaulting to `http://cellpose-poc-model:8000/segment`. This keeps the coupling between containers to a single environment variable.

### 3.2.3 Statelessness

Neither application container persists any state on its own filesystem. Segmentation results are returned to the browser as file downloads. PostgreSQL holds the only durable state (user accounts and job history). This design means both application containers can be replaced or restarted at any time without data loss.

---

## 3.3 App Container

### 3.3.1 Technology Choice: Gradio

The App Container is built on [Gradio](https://www.gradio.app/), a Python library that constructs browser-accessible web interfaces from function signatures and component declarations. It was chosen for three reasons:

- It eliminates the need for a separate JavaScript frontend, allowing the entire UI to be expressed in a single Python file (`app.py`, ~850 lines).
- It provides built-in components for all required UI elements: file upload, sliders, image display, data tables, plots, and progress indicators.
- Its `gr.Blocks` API supports multi-tab layouts, enabling Single Image, Batch, and History workflows to coexist in one page without routing logic.

The application is served via **uvicorn** on port 8001. Gradio's internal FastAPI application object (`_gr_routes.App`) is extracted and extended with a `BaseHTTPMiddleware` subclass (`_RegistrationMiddleware`) that intercepts `GET /register` and `POST /auth/register` before Gradio's own router can handle them. This allows a plain HTML registration page to be served without authentication, while all other paths remain protected by Gradio's built-in login wall.

### 3.3.2 Authentication

Gradio's `auth=` parameter accepts a callable. The `_auth_fn` function is passed here; it is called by Gradio for every login attempt. Rather than maintaining a local password store, it delegates to the Model Container's `POST /auth/login` endpoint, which checks the bcrypt hash in PostgreSQL. This means the App Container holds no credentials — the Model Container is the single source of truth for identity.

```python
def _auth_fn(username: str, password: str) -> bool:
    resp = httpx.post(MODEL_LOGIN_URL,
                      json={"username": username, "password": password},
                      timeout=...)
    if resp.status_code == 200:
        return bool(resp.json().get("valid", False))
    return False
```

New users register at `/register`, a plain HTML page served by the middleware. The registration form posts to `/auth/register`, which the middleware proxies to the Model Container's own `/auth/register` endpoint.

### 3.3.3 User Interface Layout

The UI is organised into three tabs defined in a `gr.Blocks` context:

**Tab 1 — Single Image**

| Control | Widget | Default |
|---|---|---|
| Image upload | `gr.Image(type="numpy")` | — |
| Cell diameter | `gr.Slider(0–200)` | 30 px |
| Flow threshold | `gr.Slider(0–1)` | 0.4 |
| Cell probability threshold | `gr.Slider(−6 to +6)` | 0.0 |
| Overlay opacity | `gr.Slider(0.1–1.0)` | 0.55 |
| Model selection | `gr.Radio(["cyto3","cpsam"])` | cyto3 |
| Segment button | `gr.Button` | — |

Outputs: segmentation overlay image, summary text box, per-cell statistics table (`gr.Dataframe`), cell-size histogram (`gr.Plot`), and download buttons for the overlay PNG and the raw mask array.

A collapsible `gr.Accordion` section within Tab 1 provides 3D z-stack support. A separate file upload widget accepts `.tiff`/`.tif` files; a `gr.Slider` that is hidden until segmentation completes allows the user to navigate z-slices after inference.

**Tab 2 — Batch**

Accepts multiple image files via `gr.File(file_count="multiple")`. Each file is encoded as PNG and posted individually to the Model Container using the same `_call_model` helper as single-image mode. Results are collected into a summary table (filename, model used, cell count, mean area, elapsed time) and packaged into a ZIP archive containing per-image overlay PNGs and mask `.npy` files.

**Tab 3 — History**

A read-only `gr.Dataframe` populated by `GET /projects` on the Model Container. Each row shows the job ID, original filename, model used, cell count, and timestamp. A Refresh button re-fetches on demand. Admin users (identified by matching the `ADMIN_USER` environment variable) see all records; ordinary users see only their own.

### 3.3.4 Segmentation Flow (Single Image)

1. Gradio calls `segment()` with a NumPy RGB array from the `gr.Image` widget.
2. The array is encoded to PNG bytes via Pillow (`_encode_png`).
3. `_call_model()` sends a `multipart/form-data` POST to `MODEL_URL` using the **httpx** library, with form fields for all four Cellpose parameters and the authenticated username.
4. The response body is a raw NumPy array serialised with `numpy.save`. It is deserialised with `numpy.load(io.BytesIO(resp.content))`.
5. `_render_overlay()` alpha-composites a coloured label mask over the original image using the `tab20` matplotlib colormap, with per-cell colours cycling every 20 cells.
6. `_compute_stats()` iterates over unique mask labels to produce per-cell pixel areas and coverage percentages.
7. A matplotlib histogram is generated in-memory and returned as a `gr.Plot`.
8. Temporary files (overlay PNG, masks `.npy`) are written with `tempfile.NamedTemporaryFile(delete=False)` so Gradio can serve them as downloads. Cleanup of the previous call's temporary files is performed at the start of the next call.

### 3.3.5 3D Z-Stack Segmentation Flow

Multi-frame TIFF files are not decoded by Gradio's image widget; they are uploaded via a `gr.File` component which returns the raw file path. The file is read as bytes and sent verbatim to the Model Container with MIME type `image/tiff` using `_call_model_raw()`. This preserves all frames intact.

After inference the response contains a 3D NumPy array of shape `(Z, H, W)`. The `_render_zstack_slice()` helper reads one z-slice from the original TIFF using `imageio.v3.imread`, normalises 16-bit pixel values to the `[0, 255]` range (since fluorescence microscopy TIFFs commonly use 16-bit depth), and renders the overlay for that slice. The z-slider's `change` event calls `navigate_zslice()` to re-render the overlay without re-running inference.

### 3.3.6 HTTP Client Configuration

All calls to the Model Container use an `httpx.Timeout` object configured as follows:

```python
_MODEL_TIMEOUT = httpx.Timeout(
    connect=10.0,   # TCP handshake
    write=60.0,     # upload time for large images
    read=900.0,     # 15 min — cpsam on CPU can take ~10–15 min
    pool=10.0,
)
```

The read timeout of 900 seconds matches the `proxy-read-timeout` annotation on the nginx Ingress.

---

## 3.4 Model Container

### 3.4.1 Technology Choice: FastAPI

The Model Container exposes a REST API built with **FastAPI** (version compatible with Python 3.11). FastAPI was chosen because:

- Its `async def` endpoint support allows the heavy synchronous `CellposeModel.eval()` call to be offloaded to a thread-pool executor (`loop.run_in_executor`) without blocking the uvicorn event loop. This is critical: `/health` must respond instantly even while a 10-minute cpsam inference is running.
- Its built-in request validation (via Pydantic) handles form field parsing, type coercion, and error responses automatically.
- Its `lifespan` context manager provides a structured startup/shutdown hook for model loading and database table creation.

### 3.4.2 API Endpoints

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Liveness/readiness check. Returns 200 `{"ok": true}` when both models are loaded; 503 `{"ok": false, "status": "loading"}` during startup. |
| `GET` | `/parameters` | None | Returns the full parameter schema (type, default, min, max, description) for all four Cellpose parameters. |
| `POST` | `/segment` | X-API-Key | Accepts `multipart/form-data` with an image file and four optional form fields. Returns `masks.npy` as `application/octet-stream` with an `X-Model-Used` header. |
| `GET` | `/projects` | X-API-Key | Returns the last 100 segmentation records from PostgreSQL as a JSON array. Accepts an optional `?user=` query parameter. |
| `POST` | `/auth/register` | None | Creates a new user account (bcrypt-hashed password). |
| `POST` | `/auth/login` | None | Verifies credentials and returns `{"valid": bool, "is_admin": bool}`. |

### 3.4.3 Model Loading

Both Cellpose models (`cyto3` and `cpsam`) are loaded in the `lifespan` startup handler. Loading is parallelised with `asyncio.gather` using two thread-pool executor tasks to reduce total startup time:

```python
cyto3_model, cpsam_model = await asyncio.gather(
    loop.run_in_executor(None, lambda: _load("cyto3")),
    loop.run_in_executor(None, lambda: _load("cpsam")),
)
```

During loading, `GET /health` returns HTTP 503. Kubernetes startup probes are configured to allow up to 300 seconds (30 attempts × 10 seconds) before the container is considered failed, preventing the pod from being killed while weights load.

Model weights are baked into the Docker base image (`Dockerfile.base`) at build time by running a Python snippet that invokes `cellpose.models.CellposeModel(gpu=False, pretrained_model='cyto3')` (and the same for `cpsam`) during the image build. This avoids any network download at container startup.

### 3.4.4 Inference Pipeline

The `POST /segment` endpoint performs the following steps:

**Input validation:**
- File extension and MIME type must belong to `{.png, .tiff, .tif, .jpeg, .jpg}`.
- File size must not exceed 50 MB.
- After decoding, image dimensions must not exceed 8192 × 8192 pixels.

**Z-stack detection:**
Multi-frame TIFF files are identified using `tifffile.TiffFile`:

```python
with tifffile.TiffFile(io.BytesIO(data)) as tif:
    n_frames = len(tif.pages)
    is_zstack = n_frames > 1
```

`imageio.v3` was rejected for this task because its `improps()` function does not expose a reliable frame-count attribute for all TIFF variants (e.g., BigTIFF, LZW-compressed). The `tifffile` library provides unambiguous page-count access.

**Inference:**
Inference is serialised through an `asyncio.Semaphore(1)`:

```python
async with _INFER_SEM:
    masks = await loop.run_in_executor(None, _run_inference)
```

The semaphore limits concurrent inference calls to one. Running multiple `model.eval()` calls in parallel on the same CPU cores causes memory-bandwidth thrashing and degrades throughput for all callers. Requests queue on the semaphore instead. The `/health` endpoint is an `async def` that runs directly on the event loop and is never queued, so readiness probes are always answered.

For 3D z-stacks, `_run_inference` calls `tifffile.imread(io.BytesIO(data))` to read all frames correctly, then calls `_run_2d()` for each frame and stacks the result:

```python
frames = tifffile.imread(io.BytesIO(data))
slices = [_run_2d(frames[i]) for i in range(frames.shape[0])]
return np.stack(slices, axis=0)  # shape: (Z, H, W)
```

**Response:**
The mask array is serialised with `numpy.save` and returned as `application/octet-stream` with the header `X-Model-Used` indicating which model was actually used. The App Container reads this header to display the active model name in the summary.

### 3.4.5 Cellpose Models

Two models are made available to the user:

**cyto3** — a U-Net convolutional backbone trained by the Cellpose team on a large corpus of microscopy images. It is fast: 5–30 seconds on the A40 GPU, 2–10 minutes on CPU. Suitable for most standard microscopy images.

**cpsam** — a ViT-H (Vision Transformer, "huge") backbone derived from Meta's Segment Anything Model (SAM), fine-tuned for cell segmentation. The checkpoint weights are approximately 2.4 GB. Inference is slower (2–20 minutes on GPU, potentially longer on CPU) but offers superior accuracy for difficult or low-contrast images.

Both models are invoked through the same `CellposeModel.eval()` interface. The user selects the model at request time via the `model_type` form field; the App Container passes the choice unchanged to the Model Container.

### 3.4.6 User Accounts and Security

User passwords are hashed with bcrypt before storage (10-round salt, via the `bcrypt` Python library). Plaintext passwords are never persisted or logged.

API access to the `/segment` and `/projects` endpoints is optionally protected by an `X-API-Key` header. When the `API_KEY` environment variable is set, requests without a matching key are rejected with HTTP 401. In development mode (environment variable unset), the check is skipped.

Username validation enforces a regex `^[a-zA-Z0-9_]{3,50}$` to prevent SQL injection via usernames. Password validation requires a minimum length of 8 characters. Database queries use parameterised statements exclusively — no string interpolation in SQL.

### 3.4.7 Persistence

A module-level singleton `psycopg2` connection (`_db_conn`) is maintained with `autocommit=True`. The connection is re-established automatically if it drops. Database operations are wrapped in try/except blocks and logged as warnings on failure; they never cause an inference request to fail. This ensures that the absence or temporary unavailability of PostgreSQL degrades only the history and authentication features, not the core segmentation functionality.

Two tables are used:

**`users`** — stores user accounts:
```sql
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ DEFAULT NOW()
)
```

**`projects`** — records each segmentation job:
```sql
CREATE TABLE IF NOT EXISTS projects (
    id             SERIAL PRIMARY KEY,
    project_name   TEXT,
    image_filename TEXT,
    timestamp      TIMESTAMPTZ DEFAULT NOW(),
    model_used     TEXT,
    cell_count     INT,
    mask_path      TEXT,
    username       TEXT
)
```

---

## 3.5 Infrastructure

### 3.5.1 Kubernetes Deployment

The platform is deployed to a MicroK8s cluster on the university server using a **Helm chart** (`helm-chart/`). Helm was chosen over plain Kubernetes YAML because it parameterises all environment-specific values (image tags, registry addresses, GPU counts, domain names) in a single `values.yaml` file, making promotion between environments straightforward.

Each container runs as a separate Kubernetes `Deployment` with `replicas: 1`. The three deployments are:

**App Deployment** (`cellpose-poc-app`)
- Node selector: `role: management` (CPU-only node)
- Resources: 250 m CPU request, 1 CPU limit; 256 Mi memory request, 1 Gi limit
- Probes: `startupProbe` (20 × 5s = 100s window), `livenessProbe` (15s period), `readinessProbe` (10s period) — all checking `GET /` on port 8001

**Model Deployment** (`cellpose-poc-model`)
- Node selector: `nvidia.com/gpu.present: "true"` when `useGpu: true`
- Resources: 500 m CPU / 4 Gi memory request; 8 CPU / 64 Gi memory limit; `nvidia.com/gpu: 1` in both request and limit
- No memory hard limit was imposed above 64 Gi because the server has 1000 GB RAM; an OOMKill at a lower limit would terminate inference jobs
- Probes: `startupProbe` (30 × 10s = **300s window**), `livenessProbe` (30s period, 5 failures), `readinessProbe` (10s period, 3 failures) — all checking `GET /health` on port 8000

The `startupProbe` on the model pod is critical. Without it, the default `livenessProbe` fires at approximately 55 seconds after container start, before the 30–90 second model load completes, causing the pod to be killed and restarted in an infinite loop. The `startupProbe` disables the liveness probe entirely until `/health` returns 200 for the first time.

**PostgreSQL Deployment** (`cellpose-poc-db`)
- `imagePullPolicy: IfNotPresent` to prevent Docker Hub rate-limit errors
- Resources: 100 m / 256 Mi request; 500 m / 512 Mi limit
- Readiness probe: `pg_isready` command

### 3.5.2 Networking and Ingress

An nginx Ingress resource exposes the App Container at `cellpose-poc.g007.imec.local` with TLS certificates managed by cert-manager (cluster issuer `ca-issuer`). Three critical annotations are applied:

```yaml
nginx.ingress.kubernetes.io/proxy-body-size: "55m"
nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
nginx.ingress.kubernetes.io/proxy-send-timeout: "900"
```

The default nginx `proxy-body-size` of 1 MB would reject any upload larger than 1 MB with HTTP 413. Setting it to 55 MB (5 MB above Gradio's own 50 MB cap) ensures nginx never rejects a request that Gradio would accept. The 900-second proxy timeouts accommodate worst-case cpsam inference on CPU.

### 3.5.3 Docker Image Strategy

The Model Container uses a two-stage image build to keep CI cycle time low despite the large model weights:

**Base image** (`cellpose-poc-model-base:stable`, `Dockerfile.base`):
- Contains: Python 3.11-slim + system packages + all Python dependencies + CUDA-enabled PyTorch (when `USE_CUDA=true`) + baked cyto3 and cpsam model weights.
- Size: approximately 6–8 GB.
- Rebuilt only when `Model_container/requirements.txt` or `Dockerfile.base` changes.
- Pushed once to the local registry; the GPU node caches it indefinitely.

**Code image** (`cellpose-poc-model:{SHA}`, `Dockerfile`):
- Built on every commit. Copies only `cellpose_api/app.py` and `cellpose_api/tasks.py` on top of the base image.
- Layer size: approximately 5 MB.
- Build time: under 60 seconds.

The App Container (`Dockerfile`) has a simpler structure: `python:3.11-slim` base, `pip install -r requirements.txt`, then `COPY app.py`. Since the App Container has no large dependencies, its total image size is in the range of 500 MB.

### 3.5.4 CI/CD Pipeline

The GitLab CI pipeline (`.gitlab-ci.yml`) defines five stages:

| Stage | Job | Trigger |
|---|---|---|
| `test` | `unit-test-model` | Every push |
| `build-base` | `build-model-base` | Changes to `requirements.txt` or `Dockerfile.base` |
| `build` | `build-app`, `build-model` | Every push |
| `deploy` | `deploy` | `main` branch only |
| `verify` | `verify` | `main` branch only |

**Registry addressing** is a critical detail. Two registry addresses are used:

- `PUSH_REGISTRY=10.136.94.110:32000` — the MicroK8s local registry address used by **Kaniko** (the CI image builder). Kaniko runs as a Kubernetes pod; within a pod, `localhost` refers to the pod itself, not the node. The real node IP must be used.
- `PULL_REGISTRY=localhost:32000` — the address used by **Kubernetes** when pulling images onto a node. MicroK8s nodes resolve `localhost:32000` to the local registry daemon running on the same node.

Using `PULL_REGISTRY` in the Kaniko `--build-arg BASE_IMAGE` argument would cause the base image pull to fail silently (the pod cannot reach `localhost:32000`), resulting in a "manifest unknown" error during the thin-layer build.

**Unit tests** run in a `python:3.11-slim` container using a lightweight stub of Cellpose (no GPU, no model weights). The stub `_FakeModel.eval()` returns a deterministic mask array, allowing the full inference path to be exercised in under 5 seconds. Tests cover: health endpoint states, parameter schema, segment endpoint (PNG, grayscale, oversized rejection, format rejection), authentication, and history endpoints.

**Helm deployment** uses `--wait --timeout 30m0s`. The 30-minute timeout accommodates the rare case where the base image cache is cold and the 6–8 GB image must be pulled before the pod starts. In normal operation (warm cache), the pod is running within 2–3 minutes of deployment.

---

## 3.6 Design Decisions and Trade-offs

### 3.6.1 No Message Queue

The system design deliberately omits a message queue (e.g., Celery + Redis). The university server is a single-node cluster with one GPU. Queuing inference requests asynchronously would add operational complexity (two additional containers, a broker, worker management) without a meaningful throughput benefit for a single-user or small-team POC. The `asyncio.Semaphore(1)` in the Model Container provides equivalent serialisation for the inference call with zero additional infrastructure.

### 3.6.2 Synchronous Response for Segmentation

Segmentation results are returned in the same HTTP response as the POST request (synchronous). An alternative design would accept the upload, return a job ID immediately, and have the client poll for results. The synchronous model was chosen because Gradio does not natively support polling-based result retrieval, and the 900-second HTTP timeout is sufficient for even the longest expected cpsam inference job.

### 3.6.3 Single-File Application Modules

Both `App_container/app.py` and `Model_container/cellpose_api/app.py` are single-file modules. This was intentional for the POC phase: a single file is easier to audit, deploy (the thin-layer Dockerfile copies exactly two files), and understand without navigating a module hierarchy. Refactoring into packages would be appropriate if the codebase grows significantly.

### 3.6.4 tifffile Over imageio for Multi-Frame Detection

During development, the `imageio.v3.improps()` function was found to return an `ImageProperties` object that does not reliably expose a frame count for all TIFF variants (BigTIFF, LZW-compressed). Attempting to access `n_frames` raises `AttributeError`. The `tifffile` library provides unambiguous access to `len(tif.pages)` for all TIFF formats and was adopted as the sole library for z-stack detection and multi-frame reading.

---

## 3.7 Summary

This chapter has described the implementation of a two-container on-premise cell segmentation platform. The App Container provides a multi-tab Gradio UI for single-image segmentation, 3D z-stack segmentation, batch processing, and segmentation history. The Model Container exposes a FastAPI REST API that loads two Cellpose models (cyto3 and cpsam) at startup, serialises inference through an asyncio semaphore, and persists job metadata to PostgreSQL. The Kubernetes deployment uses startup probes, resource limits, nginx body-size annotations, and a two-stage Docker image build to ensure reliable, low-latency deployments. A GitLab CI pipeline automates testing, building, and deploying on every push to the `main` branch.
