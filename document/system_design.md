# System Design — Cell Segmentation Platform (POC v1)

## Overview

A two-container web application for automated cell segmentation using [Cellpose](https://github.com/MouseLand/cellpose).
Users upload microscopy images, adjust segmentation parameters, and download results (mask overlays, `.npy` masks, CSV statistics).

---

## Deployment Paths

| Aspect | Docker Compose (local) | Helm chart (server) |
|---|---|---|
| File | `compose.yaml` | `helm-chart/` |
| Target | Developer laptop / macOS | Kubernetes GPU node |
| GPU | No — CPU-only | Yes — NVIDIA GPU via node selector |
| `USE_CUDA` build-arg | `false` (default) | `true` |
| Command | `docker compose up --build` | `helm upgrade --install …` |
| Ingress | `localhost:8001` | `cellpose-poc.g007.imec.local` |
| Secrets | `.env` file (from `.env.example`) | Kubernetes `Secret` resources |

**Rule**: never add GPU device reservations to `compose.yaml`. Never use `docker compose` to deploy to the server — use Helm.

---

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│  User's browser                                          │
│  http://localhost:8001   (local)                         │
│  https://cellpose-poc.g007.imec.local  (server)          │
└────────────────────────┬────────────────────────────────┘
                         │  HTTP (Ingress / port 8001)
         ┌───────────────▼───────────────┐
         │  App Container                │
         │  FastAPI + Gradio 4.x         │
         │  gr.mount_gradio_app at "/"   │
         │  /register  /auth/register    │
         │  Port 8001                    │
         └───────────────┬───────────────┘
                         │  HTTP (internal Docker/K8s network)
         ┌───────────────▼───────────────┐
         │  Model Container              │
         │  FastAPI + Cellpose           │
         │  GET  /health                 │
         │  GET  /parameters             │
         │  POST /segment                │
         │  POST /auth/login             │
         │  POST /auth/register          │
         │  GET  /projects               │
         │  Port 8000 (internal only)    │
         └───────────────┬───────────────┘
                         │  TCP 5432
         ┌───────────────▼───────────────┐
         │  PostgreSQL 16-alpine         │
         │  DB: cellseg                  │
         │  Port 5432 (internal only)    │
         └───────────────────────────────┘
```

---

## Services

### App Container (`App_container/`)

| Property | Value |
|---|---|
| Image base | `python:3.11-slim` |
| Framework | FastAPI + Gradio 4.x |
| Exposed port | `8001` |
| Entry point | `app.py` |

**Responsibilities**
- Serves the Gradio UI at `/` via `gr.mount_gradio_app`
- Serves the HTML registration form at `GET /register`
- Proxies `POST /auth/register` to the Model Container
- All Gradio segmentation callbacks call the Model Container via `httpx`

**Auth flow**
- Login: Gradio `auth=` callback POSTs to `/auth/login` on the Model Container
- The login page's `auth_message` contains an HTML link to `/register`

### Model Container (`Model_container/`)

| Property | Value |
|---|---|
| Image base | `python:3.11-slim` |
| Framework | FastAPI + Cellpose |
| Port | `8000` (internal — `expose`, not `ports`) |
| Entry point | `cellpose_api/app.py` |

**API contract**

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Liveness/readiness probe — returns `{"status":"ok"}` when models are loaded |
| `GET` | `/parameters` | Returns accepted segmentation parameter names and defaults |
| `POST` | `/segment` | Accepts `multipart/form-data` with `image` file + params; returns `masks.npy` as `application/octet-stream` |
| `POST` | `/auth/register` | Register a new user (bcrypt hash stored in PostgreSQL) |
| `POST` | `/auth/login` | Validate credentials; returns `{"valid": true/false}` |
| `GET` | `/projects` | List past segmentation records (filtered by `?user=` unless admin) |

**Model weights**
Baked into the Docker image at build time. Both `cyto3` and `cpsam` weights are pre-downloaded in the `Dockerfile` `RUN` step so cold-start time is minimized.

**GPU flag**
`USE_CUDA=true` build-arg replaces CPU torch wheels with CUDA 12.1 wheels (used when building the server image before a Helm deploy).

### Database (`postgres:16-alpine`)

| Property | Value |
|---|---|
| Database | `cellseg` |
| User | `cellseg` |
| Password | `${POSTGRES_PASSWORD}` from `.env` |
| Port | `5432` (internal only) |
| Volume | `postgres_data` (named volume — persists between `docker compose down` calls) |

**Schema** (created by `cellpose_api/app.py` at startup)

- `users(id, username, password_hash)` — bcrypt hashed passwords
- `projects(id, username, image_filename, model_used, cell_count, timestamp)` — segmentation history

---

## Security

- Passwords stored as bcrypt hashes (via `bcrypt` library)
- Secrets (`ADMIN_PASSWORD`, `POSTGRES_PASSWORD`) loaded from `.env` (not committed to git)
- Model Container port is never exposed to the host — `expose: ["8000"]` only
- Database port is never exposed to the host
- No hardcoded credentials in any source file

---

## Data Flow — Single Image Segmentation

```
Browser → (POST image + params) → App Container
App Container → (httpx POST /segment) → Model Container
Model Container → Cellpose inference → masks.npy bytes
Model Container → (record in PostgreSQL)
Model Container → (return masks.npy octet-stream) → App Container
App Container → (overlay render + stats) → Browser
```

---

## Helm Chart (`helm-chart/`)

Deploys to Kubernetes with GPU support.

| Setting | Value |
|---|---|
| `useGpu` | `true` |
| Node selector | `nvidia.com/gpu.present: "true"` |
| Resource limit | `nvidia.com/gpu: 1` |
| Ingress | `cellpose-poc.g007.imec.local` |

The Helm chart uses the pre-built server image (built locally with `USE_CUDA=true` and pushed to the cluster registry before `helm upgrade`).

---

## Dependency Decisions

| Package | Container | Reason |
|---|---|---|
| `gradio` | App | UI framework |
| `fastapi` + `uvicorn` | Both | ASGI server; App uses it as host for `gr.mount_gradio_app` |
| `httpx` | App | Async-capable HTTP client for Model Container calls |
| `cellpose` | Model | Segmentation engine |
| `psycopg2-binary` | Model | PostgreSQL driver |
| `bcrypt` | Model | Secure password hashing |
| `python-dotenv` | Model | Load `.env` inside container for local dev |
| `numpy`, `Pillow`, `imageio`, `tifffile` | Both | Image I/O and array handling |

**Not used** (removed from codebase): Celery, Redis, CVAT/nuclio serverless.
