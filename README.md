# Cell Segmentation Platform

AI-powered cell segmentation using [Cellpose](https://github.com/MouseLand/cellpose). Upload microscopy images, adjust parameters, and download segmentation masks — all from a browser, without sending data outside your network.

> **Thesis context**: This POC demonstrates a GDPR-compliant on-premise alternative to cloud-hosted tools such as the HuggingFace Cellpose Space, where image data never leaves the lab infrastructure.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Local Development — Docker Compose](#local-development--docker-compose)
3. [Server Deployment — Kubernetes + Helm](#server-deployment--kubernetes--helm)
4. [Authentication](#authentication)
5. [Usage](#usage)
6. [Configuration Reference](#configuration-reference)
7. [API Reference](#api-reference)
8. [Project Structure](#project-structure)
9. [Status](#status)

---

## Architecture Overview

Three independent services, one internal network:

```
Browser
  │  HTTPS
  ▼
Ingress (nginx + cert-manager TLS)          ← Kubernetes only
  │
  ▼
App Container  (Gradio + FastAPI, port 8001)
  │  HTTP (internal cluster network only)
  ▼
Model Container  (FastAPI + Cellpose, port 8000)
  │  psycopg2
  ▼
PostgreSQL  (port 5432)
```

| Service | Technology | Responsibility |
|---|---|---|
| **App Container** | Gradio 4 + FastAPI | UI, user auth (delegates to Model), file handling |
| **Model Container** | FastAPI + Cellpose | Segmentation inference, user accounts, job history |
| **Database** | PostgreSQL 16 | User accounts, segmentation history |

The Model Container and Database are **never exposed to the host** — internal-only services. All external traffic enters only through the App Container (local) or the Ingress (Kubernetes).

For the full architectural discussion see [document/architecture_guide.md](document/architecture_guide.md) and [document/system_design.md](document/system_design.md).

---

## Deployment Paths

| | Local development | Server / GPU |
|---|---|---|
| File | `compose.yaml` | `helm-chart/` |
| Machine | Any laptop (macOS/Linux/WSL2) | Kubernetes cluster with GPU |
| GPU | No — CPU only | Yes — NVIDIA GPU |
| Command | `docker compose up --build` | `helm upgrade --install …` via GitLab CI |
| URL | `http://localhost:8001` | `https://cellpose-poc.g007.imec.local` |

---

## Local Development — Docker Compose

### Prerequisites

- [Docker Desktop](https://docs.docker.com/get-docker/) ≥ 24 (macOS, Linux, or WSL2)
- At least 4 GB RAM available to Docker

### Quick Start

```bash
# 1. Clone the repository
git clone https://gitlab.gwdg.de/ostfalia-maschinenbau/gradio-cell-segmentation-thesis.git
cd gradio-cell-segmentation-thesis

# 2. Create your local environment file
cp .env.example .env
# Open .env and set ADMIN_PASSWORD and POSTGRES_PASSWORD to values of your choice

# 3. Build and start all three services
docker compose up --build

# 4. Open the app
open http://localhost:8001      # macOS
# or navigate to http://localhost:8001 in your browser
```

The first build downloads Python dependencies and the Cellpose model weights (~500 MB). Subsequent starts reuse the cached layers.

### First Login

1. Go to `http://localhost:8001`
2. Click **Create Account** and register a user
3. Click **Sign In** and log in with your credentials
4. You are now in the Gradio app — upload an image and click **Segment**

The admin account is seeded automatically at startup if `ADMIN_PASSWORD` is set in `.env`.

### Stopping and Cleaning Up

```bash
# Stop services (keeps data volumes)
docker compose down

# Stop AND delete all data (including the PostgreSQL volume)
docker compose down -v
```

### Rebuild a Single Service

```bash
docker compose up --build app      # After Gradio UI changes
docker compose up --build model    # After FastAPI / Cellpose changes
```

### CPU vs GPU

The `compose.yaml` uses **CPU-only PyTorch** (`USE_CUDA: "false"`). This works on any machine including macOS with Apple Silicon. Inference is slower (2–10 minutes per image). GPU acceleration is configured only in the Kubernetes Helm chart.

### Running Tests

```bash
# Unit tests (no Docker required — mocks Cellpose)
pip install -r Model_container/tests/requirements-test.txt
PYTHONPATH=Model_container/cellpose_api pytest Model_container/tests/ -v

# Integration tests (requires a running docker compose stack)
pip install -r tests/requirements-integration.txt
APP_URL=http://localhost:8001 pytest tests/ -v
```

---

## Server Deployment — Kubernetes + Helm

### Prerequisites

- MicroK8s cluster with `dns ingress cert-manager registry gpu` add-ons enabled
- GitLab runner tagged `a40gpu` registered on the GPU node
- GitLab CI/CD protected variables set (see [Configuration Reference](#configuration-reference))

### How the CI/CD Pipeline Works

Every push to `main` triggers:

```
test  →  build  →  deploy  →  verify
```

| Stage | What happens |
|---|---|
| **test** | Runs 31 unit tests using a stubbed Cellpose (no GPU needed) |
| **build** | Kaniko builds and pushes both Docker images to the MicroK8s registry |
| **deploy** | `helm upgrade --install` with a 12-minute timeout |
| **verify** | Dumps pod status, logs, and a live health check — always runs |

The model image is tagged by a **content hash** of `Model_container/` source files. If model code did not change, Kubernetes skips the 6.8 GB image pull entirely (`imagePullPolicy: IfNotPresent`).

### Manual Helm Deployment

```bash
helm upgrade --install cellpose-poc ./helm-chart \
  --set-string app.image.repository=localhost:32000/cellpose-poc-app \
  --set-string app.image.tag=<tag> \
  --set-string model.image.repository=localhost:32000/cellpose-poc-model \
  --set-string model.image.tag=<model-tag> \
  --set-string ingress.host=cellpose-poc.g007.imec.local \
  --set-string db.password=<secure-password> \
  --namespace cellpose-poc \
  --create-namespace \
  --wait \
  --timeout 15m
```

The `db.password` value is injected into a Kubernetes `Secret` and never stored in `values.yaml`.

### Accessing the Application

Once deployed:

```
https://cellpose-poc.g007.imec.local
```

TLS is provisioned automatically by cert-manager using the `ca-issuer` ClusterIssuer.

### Checking Deployment Status

```bash
kubectl get pods -n cellpose-poc
kubectl logs -n cellpose-poc -l app=cellpose-poc-model --tail=50
```

The model pod takes up to 2 minutes to become ready (loading Cellpose weights into GPU memory). The `startupProbe` allows up to 5 minutes.

---

## Authentication

- **Login** — `http://localhost:8001/sign-in`
- **Register** — `http://localhost:8001/register`

Passwords are stored as bcrypt hashes. The admin account is auto-seeded at startup when `ADMIN_PASSWORD` is set. Admin users see all segmentation history; regular users see only their own.

---

## Usage

1. **Sign in** or create a new account
2. **Upload image** — drag and drop a PNG, TIFF, or JPEG (max 50 MB)
3. **Adjust parameters** using the sliders:
   - **Diameter** — expected cell diameter in pixels (0 = auto-detect)
   - **Flow threshold** — max flow error; higher = more cells (default 0.4)
   - **Cell probability threshold** — lower = more pixels counted as cells (default 0.0)
   - **Model** — `cyto3` (fast, U-Net) or `cpsam` (accurate, SAM backbone)
4. **Click Segment** — results appear in seconds on GPU, minutes on CPU
5. **View results**: coloured overlay, cell count, mean/median area, size histogram
6. **Download** — overlay PNG, `masks.npy`, or statistics CSV
7. **Batch** tab — upload multiple images and download a ZIP of all results
8. **History** tab — view all past segmentation jobs

---

## Configuration Reference

### App Container

| Variable | Default | Description |
|---|---|---|
| `MODEL_URL` | `http://model:8000/segment` | Internal URL of the Model Container |
| `MODEL_API_KEY` | _(empty)_ | Optional API key for model calls |
| `ADMIN_USER` | `admin` | Username of the admin account |
| `GRADIO_SERVER_NAME` | `0.0.0.0` | Bind address for Gradio |

### Model Container

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | _(required)_ | PostgreSQL connection string |
| `USE_GPU` | `false` | Set `true` to use CUDA inference |
| `API_KEY` | _(empty)_ | API key required on `/segment`. Leave blank to disable. |
| `ADMIN_USER` | `admin` | Admin username to seed at startup |
| `ADMIN_PASSWORD` | _(empty)_ | Admin password. If set, admin account is created on startup. |

### GitLab CI Protected Variables

| Variable | Description |
|---|---|
| `DB_PASSWORD` | PostgreSQL password — injected into Kubernetes Secret at deploy time |
| `ADMIN_PASSWORD` | Admin account password passed to model container |

### `.env` File (local development)

```bash
cp .env.example .env
# Set ADMIN_PASSWORD and POSTGRES_PASSWORD
```

---

## API Reference

All endpoints are on the **Model Container** (internal only — not exposed to the internet).

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Returns 200 when models are ready, 503 while loading |
| `GET` | `/parameters` | Returns supported parameter ranges as JSON |
| `POST` | `/segment` | Segment an image; returns `masks.npy` as `application/octet-stream` |
| `POST` | `/auth/register` | Create a user. Body: `{"username": "...", "password": "..."}` |
| `POST` | `/auth/login` | Verify credentials. Returns `{"valid": bool}` |
| `GET` | `/projects` | Segmentation history. Optional `?user=username` filter. |

**`POST /segment`** (multipart/form-data):

| Field | Type | Default | Description |
|---|---|---|---|
| `image` | file | required | PNG / TIFF / JPEG, max 50 MB |
| `model_type` | string | `cyto3` | `cyto3` or `cpsam` |
| `diameter` | float | auto | Expected cell diameter in pixels |
| `flow_threshold` | float | `0.4` | Flow field error threshold |
| `cellprob_threshold` | float | `0.0` | Cell probability threshold |

---

## Project Structure

```
├── App_container/
│   ├── app.py               # FastAPI host + Gradio UI
│   ├── templates/           # HTML pages (landing, sign-in, register)
│   ├── Dockerfile
│   └── requirements.txt
│
├── Model_container/
│   ├── cellpose_api/
│   │   └── app.py           # FastAPI segmentation API
│   ├── tests/               # Unit tests (31 tests, no GPU required)
│   ├── Dockerfile
│   └── requirements.txt
│
├── helm-chart/              # Kubernetes deployment (server + GPU)
│   ├── templates/
│   │   ├── deployment.yaml
│   │   ├── services.yaml
│   │   ├── ingress.yaml
│   │   └── secrets.yaml     # K8s Secret for DB password
│   └── values.yaml
│
├── tests/                   # Integration tests
├── compose.yaml             # Local development (CPU)
├── .gitlab-ci.yml           # CI/CD pipeline
├── .env.example             # Environment variable template
├── CHANGELOG.md
└── document/
    ├── system_design.md     # System design specification
    └── architecture_guide.md  # Comprehensive architecture guide
```

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
| Self-registration page | ✅ Done |
| Segmentation history (PostgreSQL) | ✅ Done |
| Local dev via `docker compose up --build` | ✅ Done |
| Server/GPU deploy via Helm + GitLab CI | ✅ Done |
| Security headers + K8s Secret for DB password | ✅ Done |

---

## License

To be determined.


