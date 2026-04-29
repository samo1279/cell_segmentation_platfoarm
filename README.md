# Cell Segmentation Platform — POC v1

Browser-based cell segmentation for on-premise research labs. Upload microscopy images, tune Cellpose parameters, and receive a coloured overlay, cell count, per-cell statistics, and downloadable results — without sending data outside your network.

> **Thesis context**: This POC demonstrates a GDPR-compliant on-premise alternative to cloud-hosted tools such as the HuggingFace Cellpose Space, where image data never leaves the lab infrastructure.

---

## Architecture

Three Docker services, one internal network:

```
Browser
  └─► App Container  (Gradio + FastAPI, port 8001)
        ├─► /register  — self-registration page
        └─► Model Container  (FastAPI + Cellpose, internal only, port 8000)
                 └─► PostgreSQL 16  (internal only, port 5432)
```

| Service | Image base | Exposed to host |
|---|---|---|
| App | `python:3.11-slim` | Port `8001` |
| Model | `python:3.11-slim` | Internal only |
| DB | `postgres:16-alpine` | Internal only |

Full architecture diagrams, API contract, and design decisions → [document/system_design.md](document/system_design.md)

---

## Deployment Paths

| | Local development | Server / GPU |
|---|---|---|
| File | `compose.yaml` | `helm-chart/` |
| Machine | Any laptop (macOS/Linux) | Kubernetes GPU node |
| GPU | No — CPU only | Yes — NVIDIA GPU |
| Command | `docker compose up --build` | `helm upgrade --install …` |
| URL | `http://localhost:8001` | `https://cellpose-poc.g007.imec.local` |

---

## Quick Start (Local — macOS / Linux, CPU)

**Prerequisites:** Docker Desktop (or Docker Engine + Compose plugin), 4 GB RAM free.

```bash
# 1. Clone
git clone <repo-url>
cd POC_version1

# 2. Create your local secrets file
cp .env.example .env
# Edit .env — set ADMIN_PASSWORD and POSTGRES_PASSWORD

# 3. Build and start
#    First run downloads Cellpose model weights (~500 MB) — takes a few minutes.
docker compose up --build

# 4. Open the app
open http://localhost:8001
```

**Register an account** at `http://localhost:8001/register` or follow the "Register here" link on the login page.

To stop and keep data:
```bash
docker compose down
```

To stop and wipe the database volume:
```bash
docker compose down -v
```

---

## Server Deployment (Kubernetes + GPU)

The Helm chart in `helm-chart/` deploys to a Kubernetes cluster with GPU support.

```
helm-chart/
├── Chart.yaml          # Chart metadata
├── values.yaml         # useGpu: true, ingress host, replica counts
└── templates/
    ├── deployment.yaml # Deployments for app + model + db
    ├── services.yaml   # ClusterIP services
    └── ingress.yaml    # Ingress at cellpose-poc.g007.imec.local
```

**Build the GPU-enabled model image before deploying:**

```bash
# Build with CUDA 12.1 PyTorch wheels baked in
docker build \
  --build-arg USE_CUDA=true \
  -t <registry>/cellpose-poc-model:latest \
  Model_container/

# Push to your cluster registry
docker push <registry>/cellpose-poc-model:latest
```

**Deploy with Helm:**

```bash
helm upgrade --install cellpose-poc ./helm-chart \
  --set image.model=<registry>/cellpose-poc-model:latest \
  --namespace cellpose --create-namespace
```

---

## Authentication

The platform uses per-user accounts backed by PostgreSQL.

| Action | URL |
|---|---|
| Log in | `http://localhost:8001/` |
| Register | `http://localhost:8001/register` |

- The **admin** account is seeded automatically at first startup using `ADMIN_PASSWORD` from your `.env` file.
- Admin users see all segmentation history; regular users see only their own records.
- Passwords are stored as bcrypt hashes — never in plaintext.

---

## Usage

1. **Log in** — use the admin account or register a new one
2. **Upload image** — drag and drop a PNG, TIFF, or JPEG (max 50 MB)
3. **Adjust parameters** using the sliders:
   - **Diameter** — expected cell diameter in pixels (0 = auto-detect)
   - **Flow threshold** — max flow error; higher = more cells (default 0.4)
   - **Cell probability threshold** — lower = more pixels counted as cells (default 0.0)
   - **Model** — `cyto3` (fast, U-Net) or `cpsam` (accurate, ViT-H SAM backbone)
4. **Click Segment** — results appear in seconds on GPU, minutes on CPU
5. **View results**:
   - Coloured overlay of detected cells
   - Summary: cell count, mean/median/std area, smallest/largest cell
   - Per-cell statistics table and size distribution histogram
6. **Download** — overlay PNG, `masks.npy` (NumPy int array), or statistics CSV
7. **Batch** tab — upload multiple images and download a ZIP of all results
8. **History** tab — view all past segmentation jobs

---

## API Reference

The Model Container exposes these endpoints on the internal Docker network (`http://model:8000`):

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/health` | Liveness probe — `{"status":"ok"}` when models are ready |
| `GET` | `/parameters` | JSON schema of all tunable segmentation parameters |
| `POST` | `/segment` | Segment an image; returns `masks.npy` binary |
| `POST` | `/auth/register` | Register a new user |
| `POST` | `/auth/login` | Validate credentials → `{"valid": true/false}` |
| `GET` | `/projects` | List segmentation history (filtered by `?user=` unless admin) |

**`POST /segment`** (multipart/form-data):

| Field | Type | Default | Description |
|---|---|---|---|
| `image` | file | required | PNG / TIFF / JPEG, max 50 MB |
| `model_type` | string | `cyto3` | `cyto3` or `cpsam` |
| `diameter` | float | auto | Expected cell diameter in pixels |
| `flow_threshold` | float | `0.4` | Flow field error threshold |
| `cellprob_threshold` | float | `0.0` | Cell probability threshold |

**Responses:**
- `200` — `application/octet-stream` — NumPy `.npy` mask array (int32, H×W)
- `422` — validation error (bad format, oversized file)
- `500` — segmentation error

---

## Project Structure

```
POC_version1/
├── App_container/
│   ├── app.py              # Gradio UI + FastAPI host (gr.mount_gradio_app)
│   ├── requirements.txt    # gradio, fastapi, httpx, numpy, Pillow, matplotlib
│   └── Dockerfile          # python:3.11-slim, port 8001
│
├── Model_container/
│   ├── cellpose_api/
│   │   └── app.py          # FastAPI — /health /parameters /segment /auth/* /projects
│   ├── requirements.txt    # fastapi, uvicorn, cellpose, psycopg2-binary, bcrypt
│   └── Dockerfile          # python:3.11-slim, ARG USE_CUDA=false, port 8000 (internal)
│
├── helm-chart/             # Kubernetes deployment with GPU support
│   ├── Chart.yaml
│   ├── values.yaml         # useGpu: true, ingress host
│   └── templates/
│
├── document/
│   ├── system_design.md    # Full architecture spec — 3-service stack, API contract
│   └── chapter3*.md        # Thesis chapter drafts
│
├── tests/
│   └── integration_test.py # End-to-end test against a running stack
│
├── .github/
│   ├── agents/             # Copilot agents: gradio-dev, model-dev, devops, docs
│   ├── instructions/       # system-design.instructions.md — enforces architecture rules
│   ├── plan.md             # Original phased plan
│   └── plan2.md            # Cleanup plan (implemented 2026-05-01)
│
├── compose.yaml            # Local CPU dev stack (3 services: app + model + db)
├── .env.example            # Template — copy to .env and fill in secrets
├── .gitignore
├── CHANGELOG.md            # All notable changes, Keep a Changelog format
└── README.md               # This file
```

---

## Configuration

| Variable | Service | Default | Description |
|---|---|---|---|
| `ADMIN_PASSWORD` | model | — | Admin account password (from `.env`) |
| `POSTGRES_PASSWORD` | model + db | — | Database password (from `.env`) |
| `MODEL_URL` | app | `http://model:8000/segment` | Segmentation endpoint |
| `MODEL_API_KEY` | app + model | `` (empty) | Optional API key for `/segment` |
| `ADMIN_USER` | app + model | `admin` | Username with full history access |
| `USE_GPU` | model | `false` | Set by Helm chart for server builds |
| `DATABASE_URL` | model | set in compose.yaml | PostgreSQL connection string |
| `GRADIO_SERVER_NAME` | app | `0.0.0.0` | Gradio bind address |

---

## Development

### Rebuild a single service

```bash
docker compose up --build app      # Gradio UI changes
docker compose up --build model    # FastAPI / Cellpose changes
```

### Run integration tests

```bash
# Requires the stack to be running
cd tests/
pip install -r requirements-integration.txt
pytest integration_test.py -v
```

### AI Agents (GitHub Copilot)

Specialized agents are in `.github/agents/`:

| Agent | Use for |
|---|---|
| `gradio-dev` | Gradio UI, callbacks, layout (`App_container/app.py`) |
| `model-dev` | FastAPI endpoints, Cellpose, Dockerfile (`Model_container/`) |
| `devops` | Docker Compose, networking, health checks, container logs |
| `docs` | README, CHANGELOG, `document/system_design.md` |

**Architecture rule:** All changes must conform to [document/system_design.md](document/system_design.md). See `.github/instructions/system-design.instructions.md` for enforcement rules.

---

## Status

| Feature | Status |
|---|---|
| Single-image segmentation (cyto3 + cpsam) | ✅ Done |
| Coloured overlay, cell count, area stats | ✅ Done |
| Downloadable overlay PNG, masks.npy, CSV | ✅ Done |
| Batch segmentation + ZIP download | ✅ Done |
| 3D z-stack (multi-frame TIFF) | ✅ Done |
| Per-user accounts + bcrypt auth | ✅ Done |
| Self-registration page (`/register`) | ✅ Done |
| Segmentation history (PostgreSQL) | ✅ Done |
| Local dev via `docker compose up --build` | ✅ Done |
| Server/GPU deploy via Helm chart | ✅ Done |

---

## License

To be determined.


