# System Architecture Explanation
## Cell Segmentation Platform — Cellpose POC

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Three-Service Architecture](#2-three-service-architecture)
3. [Container Layers — App Container](#3-container-layers--app-container)
4. [Container Layers — Model Container](#4-container-layers--model-container)
5. [Container Layers — Database Container](#5-container-layers--database-container)
6. [How the Containers Communicate](#6-how-the-containers-communicate)
7. [Server-Side Architecture — Kubernetes](#7-server-side-architecture--kubernetes)
8. [What is Inside Kubernetes](#8-what-is-inside-kubernetes)
9. [What is Inside Each Pod](#9-what-is-inside-each-pod)
10. [CI/CD Pipeline — From Code to Running Pod](#10-cicd-pipeline--from-code-to-running-pod)
11. [End-to-End Flow — From Photo Input to Segmentation Result](#11-end-to-end-flow--from-photo-input-to-segmentation-result)
12. [Authentication Flow](#12-authentication-flow)

---

## 1. System Overview

The Cell Segmentation Platform is a web application that lets researchers upload microscopy images and automatically detect individual cells using the Cellpose deep-learning algorithm. The system is split into three independent containers that are deployed on a Kubernetes cluster (server) or via Docker Compose (local development).

```
┌───────────────────────────────────────────────────────────────────┐
│                         BROWSER (user)                            │
│              https://cellpose-poc.g007.imec.local                 │
└────────────────────────────┬──────────────────────────────────────┘
                             │  HTTPS (port 443)
                             ▼
┌───────────────────────────────────────────────────────────────────┐
│                    NGINX Ingress Controller                        │
│              (Kubernetes — routes external HTTPS traffic)          │
└────────────────────────────┬──────────────────────────────────────┘
                             │  HTTP :8001
                             ▼
┌─────────────────────────────────────────────┐
│           APP CONTAINER (Pod)               │
│  Gradio UI + FastAPI host — port 8001       │
│  Language: Python 3.11                      │
│  Runs on: management node                   │
└──────────────────┬──────────────────────────┘
                   │  HTTP :8000  (internal ClusterIP only)
                   ▼
┌─────────────────────────────────────────────┐
│          MODEL CONTAINER (Pod)              │
│  FastAPI + Cellpose — port 8000             │
│  Language: Python 3.11                      │
│  Runs on: GPU node (A40)                    │
└──────────────────┬──────────────────────────┘
                   │  TCP :5432  (internal ClusterIP only)
                   ▼
┌─────────────────────────────────────────────┐
│          DATABASE CONTAINER (Pod)           │
│  PostgreSQL 16-alpine — port 5432           │
│  Stores: users, segmentation history        │
│  Runs on: management node                   │
└─────────────────────────────────────────────┘
```

**Key design principle:** Only the App Container is reachable from the outside world. The Model Container and Database are internal — they have no public port, they can only be reached by other services inside the cluster.

---

## 2. Three-Service Architecture

| Service | Technology | Responsibility | Exposed to user? |
|---------|-----------|----------------|-----------------|
| App Container | Python 3.11, Gradio 4.x, FastAPI, httpx | Renders the web UI, handles login, forwards images to the model, renders results | Yes — via Ingress |
| Model Container | Python 3.11, FastAPI, Cellpose, PyTorch, psycopg2 | Runs the neural network, manages users in DB, writes segmentation history | No — ClusterIP only |
| Database | PostgreSQL 16-alpine | Stores user accounts (bcrypt hashed passwords) and segmentation job records | No — ClusterIP only |

---

## 3. Container Layers — App Container

The App Container is built from `App_container/Dockerfile`. A Docker image is made of layers stacked on top of each other. Each instruction in the Dockerfile creates one layer.

### Dockerfile layers (App Container)

```
Layer 1 — Base OS
   FROM python:3.11-slim
   ↳ Debian slim with Python 3.11 interpreter (~50 MB)
   ↳ Never changes

Layer 2 — Set working directory
   WORKDIR /app
   ↳ All subsequent paths are relative to /app

Layer 3 — Copy dependency list
   COPY requirements.txt .
   ↳ Only changes when requirements.txt changes
   ↳ By copying this before pip install, Docker can cache the next layer

Layer 4 — Install Python packages
   RUN pip install --no-cache-dir -r requirements.txt
   ↳ Installs: gradio, httpx, numpy, Pillow, matplotlib, pandas, imageio,
               tifffile, fastapi, uvicorn
   ↳ This layer is cached and NOT rebuilt unless requirements.txt changes
   ↳ Size: ~400 MB

Layer 5 — Copy application code
   COPY app.py .
   ↳ This layer changes on EVERY commit
   ↳ Because it is the LAST layer, all layers above it are reused from cache
   ↳ Size: ~30 KB

Layer 6 — Runtime metadata
   EXPOSE 8001
   CMD ["python", "app.py"]
   ↳ Declares port and start command
```

### What runs inside the App Container at runtime

When the container starts, Python executes `app.py`. This file does two things:

**1. Builds a FastAPI host application**
```python
from fastapi import FastAPI
fastapi_app = FastAPI()
```
FastAPI handles the `/register` page (plain HTML) and the `/` mount point for Gradio.

**2. Mounts the Gradio interface onto FastAPI**
```python
import gradio as gr
app = gr.mount_gradio_app(fastapi_app, demo, path="/")
```
This is the official Gradio API. The Gradio `demo` object contains all tabs (Single Image, Batch, History) and is served at the root path. FastAPI handles everything else (the `/register` route).

**3. Starts uvicorn**
```python
uvicorn.run(app, host="0.0.0.0", port=8001)
```
Uvicorn is the ASGI web server. It listens on port 8001 and handles every incoming HTTP request.

### App Container — internal software stack

```
┌──────────────────────────────────────────────────────┐
│                   uvicorn (ASGI server)               │
│                     port 8001                        │
├──────────────────────────────────────────────────────┤
│               FastAPI application                    │
│   GET  /register  →  HTML registration page          │
│   POST /          →  Gradio handles it               │
│   *    /          →  Gradio mount                    │
├──────────────────────────────────────────────────────┤
│               Gradio Blocks interface                │
│   Tab: Single Image  →  segment()                    │
│   Tab: Batch         →  batch_segment()              │
│   Tab: History       →  load_history()               │
│   Auth callback      →  _auth_fn()                   │
├──────────────────────────────────────────────────────┤
│               httpx HTTP client                      │
│   Sends images → Model Container :8000               │
│   Timeout: read=900s (15 min for large images)       │
└──────────────────────────────────────────────────────┘
```

---

## 4. Container Layers — Model Container

The Model Container is built from `Model_container/Dockerfile`. It is a much larger image (~6.8 GB) because it includes PyTorch and pre-downloaded model weights.

### Dockerfile layers (Model Container)

```
Layer 1 — Base OS
   FROM python:3.11-slim
   ↳ ~50 MB. Never changes.

Layer 2 — System packages
   RUN apt-get install curl build-essential
   ↳ curl: used by the HEALTHCHECK command inside the container
   ↳ build-essential: C compiler needed by some Python packages (e.g. psycopg2)
   ↳ Rarely changes (~200 MB)

Layer 3 — Copy dependency list
   COPY requirements.txt .
   ↳ Only changes when requirements.txt changes

Layer 4 — Install Python packages
   RUN pip install -r requirements.txt
   ↳ Installs: fastapi, uvicorn, python-multipart, packaging, cellpose,
               numpy, imageio, tifffile, psycopg2-binary, python-dotenv, bcrypt
   ↳ Cellpose pulls in CPU PyTorch as a transitive dependency
   ↳ Size: ~1 GB. Cached and reused unless requirements.txt changes.

Layer 5 — Install CUDA PyTorch (server only)
   RUN if [ "$USE_CUDA" = "true" ]; then
         pip install torch torchvision --index-url .../cu121
       fi
   ↳ Only runs when BUILD_ARG USE_CUDA=true (CI builds for GPU server)
   ↳ Replaces CPU wheels with CUDA 12.1 wheels
   ↳ Size: ~3.5 GB additional. Rarely changes.

Layer 6 — Download model weights (baked into image)
   RUN python -c "
       from cellpose import models
       models.CellposeModel(gpu=False, pretrained_model='cyto3')
       models.CellposeModel(gpu=False, pretrained_model='cpsam')
   "
   ↳ cyto3: ~200 MB U-Net weights (fast, general-purpose cell detection)
   ↳ cpsam: ~2.4 GB ViT-H (SAM transformer) weights (slow, highest accuracy)
   ↳ Weights are downloaded ONCE at build time and baked into the image.
   ↳ At runtime: no internet download needed, model loads from local disk.
   ↳ This layer NEVER changes (same weights forever).

Layer 7 — Copy application code
   COPY cellpose_api/app.py .
   ↳ This is the ONLY layer that changes on every commit.
   ↳ Because it is last, all 6 layers above are reused from cache.
   ↳ With Kaniko --cache=true: only this ~30 KB layer is rebuilt per commit.
   ↳ Build time: ~12 min cold → ~30 seconds with cache.

Layer 8 — Runtime metadata
   EXPOSE 8000
   HEALTHCHECK --interval=30s --start-period=90s ...
   CMD ["uvicorn", "app:app", "--timeout-keep-alive", "620"]
```

### Why --timeout-keep-alive 620?

The App Container sends HTTP requests with a 900-second read timeout. Uvicorn's default TCP keep-alive is only 5 seconds. Without the override, uvicorn would silently drop the TCP connection during a long inference job (e.g. cpsam on a large image). Setting it to 620 seconds ensures the connection stays alive during the full inference window.

### What runs inside the Model Container at runtime

```
┌──────────────────────────────────────────────────────────────────┐
│                   uvicorn (ASGI server)                          │
│                     port 8000                                    │
├──────────────────────────────────────────────────────────────────┤
│               FastAPI application                                │
│                                                                  │
│   GET  /health       → reports loading state + GPU flag         │
│   GET  /parameters   → returns model parameter metadata         │
│   POST /segment      → validates image, runs Cellpose, returns  │
│                         masks.npy as application/octet-stream   │
│   POST /auth/register → creates user account in DB             │
│   POST /auth/login    → verifies bcrypt password hash           │
│   GET  /projects      → returns segmentation history records    │
├──────────────────────────────────────────────────────────────────┤
│           asyncio.Semaphore(1) — inference queue                 │
│   Only ONE Cellpose eval() runs at a time.                       │
│   Concurrent requests queue here — no memory thrashing.         │
│   /health bypasses the semaphore (lightweight async, never        │
│   queued in thread pool).                                        │
├──────────────────────────────────────────────────────────────────┤
│               Cellpose models (in-memory after startup)          │
│   MODELS["cyto3"] — CellposeModel(pretrained_model='cyto3')     │
│   MODELS["cpsam"] — CellposeModel(pretrained_model='cpsam')     │
│   Both loaded in parallel at startup via asyncio.gather()        │
│   /health returns 503 until both models are ready               │
├──────────────────────────────────────────────────────────────────┤
│               psycopg2 — PostgreSQL client                       │
│   Singleton connection, auto-reconnects if dropped               │
│   autocommit=True (no open transactions on long-lived conn)      │
└──────────────────────────────────────────────────────────────────┘
```

### Model Container — startup sequence

When the container starts:
```
t=0s    uvicorn starts, port 8000 binds
t=0s    lifespan() coroutine begins
t=0-90s both Cellpose models load in parallel (thread pool executors)
        /health returns HTTP 503 during this period
t=0-90s Kubernetes readiness probe polls /health every 10s
        Pod is marked "Not Ready" — no traffic sent yet
t=90s   both models loaded
        DB connection established
        users table + projects table created (IF NOT EXISTS)
        admin account seeded (ON CONFLICT DO NOTHING — idempotent)
        /health returns HTTP 200
        Kubernetes marks pod "Ready"
        Traffic begins flowing
```

---

## 5. Container Layers — Database Container

The Database uses the official `postgres:16-alpine` image directly. No custom Dockerfile is needed.

```
postgres:16-alpine (official image)
   ├── Alpine Linux base (~5 MB)
   ├── PostgreSQL 16 binaries + libs
   ├── initdb entry script
   └── CMD: postgres -c config

Environment variables at runtime:
   POSTGRES_DB=cellseg        → creates database named "cellseg"
   POSTGRES_USER=cellseg      → creates role "cellseg"
   POSTGRES_PASSWORD=...      → sets password for that role

Tables (created by Model Container on startup, not by PostgreSQL itself):
   users:
     id            SERIAL PRIMARY KEY
     username      TEXT UNIQUE NOT NULL
     password_hash TEXT NOT NULL       ← bcrypt hash, NEVER plaintext
     is_admin      BOOLEAN
     created_at    TIMESTAMPTZ

   projects:
     id             SERIAL PRIMARY KEY
     image_filename TEXT
     timestamp      TIMESTAMPTZ
     model_used     TEXT
     cell_count     INT
     username       TEXT
```

**Why the Model Container creates the tables, not PostgreSQL?**
PostgreSQL's `initdb` scripts only run once on first start. In Kubernetes, the DB pod can restart independently of the Model Container. To keep the schema definition in one place (the Python code), the `CREATE TABLE IF NOT EXISTS` is run every time the Model Container starts. It is idempotent — running it twice has no effect.

---

## 6. How the Containers Communicate

### Local development (Docker Compose)

Docker Compose creates a private network called `cellpose-poc_default`. Every service gets a DNS hostname matching its service name.

```
[browser]
    ↓ http://localhost:8001
[app container]  hostname: app
    ↓ http://model:8000/segment   ← Docker DNS resolves "model" → container IP
[model container]  hostname: model
    ↓ postgresql://cellseg:pass@db:5432/cellseg
[db container]  hostname: db
```

The model and db containers use `expose:` (not `ports:`). This means the port is only reachable inside the Docker network — not from the host machine. The browser can only reach the app.

### Server (Kubernetes)

In Kubernetes, communication goes through Service objects (stable DNS names for pods):

```
[browser]
    ↓ https://cellpose-poc.g007.imec.local
[Ingress nginx]
    ↓ http://cellpose-poc-app:8001
[App Container Pod]
    ↓ http://cellpose-poc-model:8000/segment   ← Kubernetes DNS
[Model Container Pod]
    ↓ postgresql://cellseg:pass@cellpose-poc-db:5432/cellseg
[DB Container Pod]
```

The Kubernetes Services for the model and db are type `ClusterIP` — only reachable from within the cluster, never from outside.

### HTTP API contract

All communication between App Container and Model Container uses standard HTTP:

| Direction | Method | Path | Body | Response |
|-----------|--------|------|------|----------|
| App → Model | POST | `/segment` | multipart/form-data with image file + params | `masks.npy` as `application/octet-stream` |
| App → Model | POST | `/auth/login` | JSON `{username, password}` | JSON `{valid: bool, is_admin: bool}` |
| App → Model | POST | `/auth/register` | JSON `{username, password}` | JSON `{message}` |
| App → Model | GET | `/projects?user=X` | — | JSON array of history records |
| App → Model | GET | `/health` | — | JSON `{ok, models, gpu}` |
| Kubernetes → Model | GET | `/health` | — | HTTP 200 or 503 |

---

## 7. Server-Side Architecture — Kubernetes

The server runs **MicroK8s** — a lightweight single-node Kubernetes distribution. The cluster has two nodes:

```
┌─────────────────────────────────────────────────────────────────┐
│                    MicroK8s Cluster                             │
│                                                                 │
│  ┌──────────────────────────┐  ┌──────────────────────────┐    │
│  │   Management Node         │  │   GPU Node (A40)          │    │
│  │   role=management         │  │   nvidia.com/gpu.present  │    │
│  │                          │  │                          │    │
│  │  ┌────────────────────┐  │  │  ┌────────────────────┐  │    │
│  │  │  App Pod           │  │  │  │  Model Pod         │  │    │
│  │  │  (Gradio + FastAPI)│  │  │  │  (Cellpose GPU)    │  │    │
│  │  └────────────────────┘  │  │  └────────────────────┘  │    │
│  │  ┌────────────────────┐  │  │                          │    │
│  │  │  DB Pod            │  │  │  NVIDIA A40 GPU          │    │
│  │  │  (PostgreSQL 16)   │  │  │  allocated via           │    │
│  │  └────────────────────┘  │  │  nvidia.com/gpu: "1"     │    │
│  │  ┌────────────────────┐  │  └──────────────────────────┘    │
│  │  │  Ingress nginx     │  │                                   │
│  │  └────────────────────┘  │                                   │
│  └──────────────────────────┘                                   │
│                                                                 │
│  Registry: localhost:32000 (MicroK8s built-in)                  │
│  Namespace: cellpose-poc                                        │
└─────────────────────────────────────────────────────────────────┘
```

### Why are the pods on different nodes?

The Helm chart `deployment.yaml` uses `nodeSelector` to schedule pods:

```yaml
# App Container and DB: scheduled on management node (no GPU needed)
nodeSelector:
  role: management

# Model Container: scheduled on GPU node
nodeSelector:
  nvidia.com/gpu.present: "true"
tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
```

The `toleration` is required because GPU nodes are marked with a `taint` that prevents normal pods from being scheduled there. Only pods that explicitly tolerate the taint can run on the GPU node. This prevents non-GPU workloads from accidentally occupying GPU resources.

### GPU allocation

For PyTorch to use the GPU inside a Kubernetes pod, the GPU must be declared in both `requests` and `limits`:

```yaml
resources:
  requests:
    nvidia.com/gpu: "1"
  limits:
    nvidia.com/gpu: "1"
```

Without this, `torch.cuda.is_available()` returns `False` even when CUDA PyTorch is installed, because the NVIDIA device plugin has not mounted the GPU into the container.

---

## 8. What is Inside Kubernetes

Kubernetes is an orchestration system. It manages the lifecycle of containers across one or more machines. Here is what exists inside the cluster for this application:

### Namespace: `cellpose-poc`

All resources are isolated in a namespace called `cellpose-poc`. This is created automatically by `helm upgrade --install --create-namespace`.

### Deployments (3 total)

A Deployment is a Kubernetes object that says "keep N copies of this pod running". If a pod crashes, the Deployment controller starts a new one automatically.

```
cellpose-poc-app     replicas: 1  →  manages the App Pod
cellpose-poc-model   replicas: 1  →  manages the Model Pod
cellpose-poc-db      replicas: 1  →  manages the DB Pod
```

### Services (3 total)

A Service is a stable DNS name + load balancer for a set of pods. Pods have ephemeral IPs that change when they restart. Services have stable cluster-internal IPs.

```
cellpose-poc-app    type: (default ClusterIP)  port: 8001  → targeted by Ingress
cellpose-poc-model  type: ClusterIP             port: 8000  → internal only
cellpose-poc-db     type: ClusterIP             port: 5432  → internal only
```

### Ingress

The Ingress is the entry point for external traffic. It runs nginx inside the cluster and performs:
- TLS termination (HTTPS → HTTP inside cluster)
- Routes requests to `cellpose-poc-app:8001`
- Sets proxy body size to 55 MB (so large image uploads are not rejected by nginx before reaching Gradio)
- Sets proxy timeouts to 900 seconds (so long-running Cellpose inference is not cut off)

```
https://cellpose-poc.g007.imec.local
    ↓
Ingress (nginx)
    nginx.ingress.kubernetes.io/proxy-body-size: "55m"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
    ↓
cellpose-poc-app Service :8001
    ↓
App Pod
```

### Helm chart

Helm is a package manager for Kubernetes. Instead of writing 10 separate YAML files and applying them one by one, Helm templates are used. The chart is in `helm-chart/`:

```
helm-chart/
  Chart.yaml          — chart name, version
  values.yaml         — all configurable values (image tags, resources, credentials)
  templates/
    deployment.yaml   — 3 Deployment objects
    services.yaml     — 3 Service objects
    ingress.yaml      — 1 Ingress object
```

The CI pipeline deploys with:
```bash
helm upgrade --install cellpose-poc ./helm-chart \
  --set app.image.tag=$CI_COMMIT_SHORT_SHA \
  --set model.image.tag=$CI_COMMIT_SHORT_SHA \
  --wait --timeout 20m0s
```

`--wait` means the Helm command does not return until all pods are Ready (health probes passing). `--timeout 20m0s` gives the model pod up to 20 minutes — needed because Cellpose loads two large models (~2.6 GB) from disk at startup.

---

## 9. What is Inside Each Pod

A Pod is the smallest deployable unit in Kubernetes. It wraps one or more containers and provides them with a shared network namespace (they share an IP address and port space).

### App Pod

```
Pod: cellpose-poc-app-<hash>
Node: management node
IP: (ephemeral cluster IP)
Container: gradio
  Image: localhost:32000/cellpose-poc-app:<sha>
  Port: 8001
  CPU request: 250m  limit: 1000m
  Memory request: 256Mi  limit: 1Gi
  Environment:
    MODEL_URL=http://cellpose-poc-model:8000/segment

  startupProbe:  GET /  — 20 attempts × 5s = 100s max startup window
  livenessProbe: GET /  — restarts if fails 3 times × 15s
  readinessProbe: GET / — removed from Service endpoints while unhealthy
```

### Model Pod

```
Pod: cellpose-poc-model-<hash>
Node: GPU node
IP: (ephemeral cluster IP)
GPU: 1× NVIDIA A40 (allocated by device plugin)
Container: cellpose
  Image: localhost:32000/cellpose-poc-model:<sha>
  Port: 8000
  CPU request: 500m  limit: 8000m (8 cores)
  Memory request: 4Gi  limit: 64Gi
  GPU request: 1   limit: 1 (nvidia.com/gpu)
  Environment:
    USE_GPU=true
    DATABASE_URL=postgresql://cellseg:...@cellpose-poc-db:5432/cellseg
    ADMIN_USER=admin
    ADMIN_PASSWORD=OstfaliaAdmin2026

  startupProbe:  GET /health — 30 attempts × 10s = 300s max (5 min)
                 DISABLED liveness during this window — pod is never killed
  readinessProbe: GET /health — keeps pod out of service while loading
  livenessProbe: GET /health — kills pod if it becomes permanently unhealthy
```

**Why memory limit: 64Gi but no hard cap?**
The server has 1000 GB RAM. A hard memory limit would cause an OOMKill even when free RAM exists. The 64 GB is a soft cap. cpsam (ViT-H SAM) can use up to ~8 GB VRAM on the A40 GPU plus system RAM for intermediate activations on large images.

### DB Pod

```
Pod: cellpose-poc-db-<hash>
Node: management node
Container: postgres
  Image: postgres:16-alpine
  Port: 5432
  CPU request: 100m  limit: 500m
  Memory request: 256Mi  limit: 512Mi
  Volume: postgres_data (PersistentVolumeClaim)
    Mounted at: /var/lib/postgresql/data
    Data survives pod restarts

  readinessProbe: pg_isready -U cellseg
```

---

## 10. CI/CD Pipeline — From Code to Running Pod

The pipeline is defined in `.gitlab-ci.yml` and runs in 4 stages on every `git push`:

```
git push → GitLab
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 1: test                                              │
│                                                             │
│  unit-test-model:                                           │
│    image: python:3.11-slim                                  │
│    script:                                                  │
│      - pip install -r Model_container/tests/requirements.txt│
│      - pytest Model_container/tests/ --junit-xml=...        │
│    ↳ Runs FAST (no Cellpose, no torch)                      │
│    ↳ Cellpose and DB are stubbed/mocked in tests            │
│    ↳ Fails fast here before wasting time building images    │
└─────────────────────────────────────────────────────────────┘
              │ (only continues if tests pass)
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 2: build (runs in parallel)                          │
│                                                             │
│  build-app:                           build-model:          │
│    image: kaniko:v1.14.0-debug          (same kaniko image) │
│    /kaniko/executor                     /kaniko/executor     │
│      --context App_container              --context Model_container
│      --dockerfile .../Dockerfile          --dockerfile .../Dockerfile
│      --destination 10.136.94.110:         --build-arg USE_CUDA=true
│        32000/cellpose-poc-app:<sha>       --destination 10.136.94.110:
│      --cache=true                           32000/cellpose-poc-model:<sha>
│      --cache-repo ...app-cache            --cache=true
│      --insecure                           --cache-repo ...model-cache
│      --insecure-pull                      --insecure
│                                           --insecure-pull
│                                                             │
│  ↳ --cache=true: Kaniko checks registry cache before        │
│    executing each Dockerfile instruction.                   │
│    Unchanged layers are pulled from cache, not rebuilt.     │
│  ↳ Only Layer 7 (COPY app.py) rebuilds on normal commits.  │
│  ↳ First build: ~12 min. Subsequent builds: ~30-60 sec.    │
│                                                             │
│  Images are pushed to MicroK8s registry at 10.136.94.110:  │
│  32000 (node IP — kaniko pods cannot use localhost).        │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 3: deploy (only: main branch)                        │
│                                                             │
│  deploy:                                                    │
│    image: alpine/helm:3.14.0                                │
│    helm upgrade --install cellpose-poc ./helm-chart         │
│      --set app.image.tag=<sha>                              │
│      --set model.image.tag=<sha>                            │
│      --wait --timeout 20m0s                                 │
│                                                             │
│  ↳ Kubernetes pulls images from localhost:32000             │
│    (MicroK8s nodes use localhost to reach their local       │
│    registry daemon — different from the push address)       │
│  ↳ Rolling update: new pods start, probes pass,            │
│    old pods are terminated                                  │
│  ↳ --wait blocks until all pods are Ready                  │
└─────────────────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────────────────┐
│  Stage 4: verify (always runs, even if deploy fails)        │
│                                                             │
│  verify:                                                    │
│    image: bitnami/kubectl:latest                            │
│    - kubectl get pods -n cellpose-poc -o wide               │
│    - kubectl describe pod -l app=cellpose-poc-model         │
│    - kubectl logs -l app=cellpose-poc-model --tail=100      │
│    - Wait up to 3 min for model pod Ready                   │
│    - Health check via exec into app pod                     │
│    - Segment test: POST black 64×64 PNG → check masks shape │
│                                                             │
│  ↳ All Kubernetes state is dumped into CI job output.       │
│    OOMKill, probe failures, image pull errors all visible.  │
└─────────────────────────────────────────────────────────────┘
```

---

## 11. End-to-End Flow — From Photo Input to Segmentation Result

This section traces every step that happens when a user uploads an image and clicks **Segment**.

### Phase 0 — User opens the browser

```
Browser → HTTPS GET https://cellpose-poc.g007.imec.local/
         ↓
         Ingress nginx: TLS termination
         ↓
         App Pod → FastAPI → Gradio mount → serves HTML+JS UI
         ↓
         Browser renders Gradio interface (React-based)
         Gradio opens a WebSocket to /queue/join for event streaming
```

### Phase 1 — User uploads the image

```
User drags a PNG/TIFF/JPEG file onto the Gradio Image component.

Browser → multipart/form-data POST to /upload (Gradio internal endpoint)
         ↓
         App Container (Gradio) receives file bytes
         Stores temporarily in Gradio's temp directory
         Returns a temporary file path back to the browser (via WebSocket)
         
At this point the image is in memory on the App Container. 
The Model Container has NOT seen it yet.
```

### Phase 2 — User clicks "Segment"

```
Browser → WebSocket message to Gradio queue: {"fn_index": 0, "data": [image, ...params]}
         ↓
         Gradio event queue schedules the "segment" Python callback
         Gradio calls: segment(image_np, diameter, flow_threshold,
                               cellprob_threshold, model_type, opacity,
                               request=gr.Request)
```

The `segment()` function in `App_container/app.py` is called with:
- `image_np` — a numpy array of shape (H, W, 3), dtype uint8, RGB
- `diameter` — float, expected cell size in pixels (0 = auto-detect)
- `flow_threshold` — float 0.0–1.0, controls flow error tolerance
- `cellprob_threshold` — float -6.0–6.0, controls cell probability cutoff
- `model_type` — "cyto3" or "cpsam"
- `opacity` — float for overlay rendering
- `request.username` — the logged-in username (from Gradio's auth session)

### Phase 3 — App Container encodes and sends the image

```python
# Convert numpy array to PNG bytes
image_bytes = _encode_png(image_np)
# image_bytes: binary PNG, typically 50 KB – 5 MB

# POST to Model Container
resp = httpx.post(
    "http://cellpose-poc-model:8000/segment",
    files={"image": ("image.png", image_bytes, "image/png")},
    data={
        "flow_threshold": 0.4,
        "cellprob_threshold": 0.0,
        "model_type": "cyto3",
        "username": "alice",
    },
    timeout=httpx.Timeout(connect=10.0, write=60.0, read=900.0, pool=10.0),
)
```

The `read=900.0` timeout means the App Container will wait up to 15 minutes for the Model Container to respond. This is necessary because cpsam on large images can take 10–20 minutes.

### Phase 4 — Model Container receives and validates the image

```
POST /segment arrives at uvicorn → FastAPI router → segment() endpoint

Step 4a: API key check
   verify_api_key(x_api_key header)
   If API_KEY env var is set and header does not match → HTTP 401

Step 4b: Validate file extension and content type
   ext = os.path.splitext(image.filename)[1].lower()
   If ext not in {.png, .tiff, .tif, .jpeg, .jpg} → HTTP 422

Step 4c: Read all bytes
   data = await image.read()

Step 4d: Size check
   If len(data) > 50 MB → HTTP 422

Step 4e: Decode image
   img = imageio.v3.imread(io.BytesIO(data))
   → numpy array

Step 4f: Dimension check
   If height > 8192 or width > 8192 → HTTP 422
   (protects against extremely large images exhausting GPU VRAM)

Step 4g: Z-stack detection
   With tifffile, count pages in the TIFF.
   If pages > 1 → is_zstack = True, n_frames = count
   This enables per-slice 3D segmentation later.
```

### Phase 5 — Model Container runs Cellpose inference

```python
# Acquire semaphore — only 1 inference at a time
async with _INFER_SEM:
    masks = await loop.run_in_executor(None, _run_inference)
```

**Why `run_in_executor`?** FastAPI is async. Cellpose's `model.eval()` is a synchronous, CPU/GPU-blocking call that can take minutes. Calling it directly in an async function would block the entire event loop — `/health` would stop responding, Kubernetes would think the pod is dead. `run_in_executor` moves the blocking call to a thread pool, keeping the event loop free.

**What Cellpose does internally during `_run_inference()`:**

```
Step 5a: Image normalization
   Cellpose normalizes pixel values to [0, 1] internally.

Step 5b: Neural network forward pass
   cyto3 path (U-Net):
     Input: (H, W, channels) image tensor
     → Encoder: 4 downsampling stages (conv + BN + ReLU)
     → Bottleneck: highest-level abstract features
     → Decoder: 4 upsampling stages with skip connections
     → Output heads: (2) flow vectors (Δx, Δy) + (1) cell probability map
     Time on A40 GPU: 5–30 seconds
     Time on CPU: 2–10 minutes

   cpsam path (ViT-H SAM):
     Input: (H, W, channels) image tensor, resized to 1024×1024
     → ViT-H image encoder: 32 transformer blocks, 307M parameters
     → Cellpose decoder head: predicts flows + cell probability
     → Input resolution restored via bilinear upsampling
     Time on A40 GPU: 2–20 minutes
     Time on CPU: would take hours

Step 5c: Flow field integration (watershed-like)
   For each pixel predicted as "cell" (probability > cellprob_threshold):
   Follow the flow vectors (gradient descent on the predicted flow field)
   Pixels that converge to the same point belong to the same cell.
   This is Cellpose's key algorithm — it replaces classical watershed.
   flow_threshold: maximum allowed divergence of flow vectors.
   Higher = accept more cells; lower = reject ambiguous detections.

Step 5d: Output
   masks: numpy array, shape (H, W) or (Z, H, W) for z-stacks
   dtype: int32
   Values: 0 = background, 1 = first cell, 2 = second cell, ...
   Each unique non-zero integer is one cell instance.
```

### Phase 6 — Model Container saves to database and returns result

```python
# Save job record to PostgreSQL (best-effort — never fails the request)
conn = _get_db_conn()
if conn:
    cur.execute(
        "INSERT INTO projects (image_filename, model_used, cell_count, username)"
        " VALUES (%s, %s, %s, %s)",
        (image.filename, "cyto3", cell_count, "alice"),
    )

# Serialize masks to .npy format
buf = io.BytesIO()
np.save(buf, masks.astype(np.int32))
buf.seek(0)

# Return as binary response
return Response(
    content=buf.getvalue(),
    media_type="application/octet-stream",
    headers={
        "Content-Disposition": "attachment; filename=masks.npy",
        "X-Model-Used": "cyto3",    ← tells App Container which model ran
    },
)
```

The response is raw binary — not JSON. The masks array is serialized using NumPy's `.npy` format (a simple binary container with a header + raw data). Transferring binary is ~3× more efficient than encoding the same data as JSON.

### Phase 7 — App Container receives the masks and renders results

```python
# Deserialize masks from response body
masks = np.load(io.BytesIO(resp.content))
# masks: numpy (H, W) int32 array

# Count detected cells
labels = np.unique(masks)
labels = labels[labels != 0]  # exclude background (0)
cell_count = len(labels)

# Render colored overlay on top of original image
overlay_uint8 = _render_overlay(image_np, masks, opacity)
```

**`_render_overlay()` — how the colored overlay is created:**

```python
# For each unique cell label:
for i, label_id in enumerate(labels):
    color = matplotlib.colormaps["tab20"](i % 20)[:3]  # RGB from tab20 colormap
    mask_px = masks == label_id                          # boolean pixel mask for this cell
    # Alpha composite: blend original pixel with cell color
    overlay[mask_px] = original[mask_px] * (1 - opacity) + color * opacity
```

Each cell gets a distinct color from matplotlib's `tab20` colormap (20 distinguishable colors, cycling). The `opacity` slider (default 0.55) controls how much the color covers the original image.

**Statistics computation:**

```python
for label_id in labels:
    area_px = int(np.sum(masks == label_id))  # number of pixels belonging to this cell
    areas.append(area_px)
    stats_rows.append([int(label_id), area_px, round(area_px / total_pixels * 100, 3)])
```

**Histogram generation:**

```python
fig, ax = plt.subplots(figsize=(6, 3))
ax.hist(areas, bins=min(30, cell_count), color="#2E7D32", edgecolor="white")
ax.set_xlabel("Cell area (pixels)")
```

Matplotlib renders the histogram into a figure object. Gradio serializes it to PNG automatically for display in the browser.

### Phase 8 — Results are written to temp files and sent to browser

```python
# Write overlay PNG to temp file (Gradio serves static files by path)
overlay_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
Image.fromarray(overlay_uint8).save(overlay_tmp.name)

# Write masks array to temp file (for download)
masks_tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False)
np.save(masks_tmp.name, masks)

# Track for cleanup on next call
_pending_cleanup.extend([overlay_tmp.name, masks_tmp.name])

# Return to Gradio — these values are sent to the browser via WebSocket
return (
    overlay_path,   # → gr.Image: rendered as <img>
    summary,        # → gr.Textbox: "Model: cyto3 | 47 cells detected..."
    stats_rows,     # → gr.Dataframe: per-cell table
    fig,            # → gr.Plot: matplotlib histogram
    overlay_path,   # → gr.File: downloadable PNG
    masks_path,     # → gr.File: downloadable .npy
)
```

### Phase 9 — Browser displays the results

```
Gradio receives return values via WebSocket
↓
Updates DOM:
  - Overlay image: rendered in the output Image component
  - Summary textbox: cell count, mean/median/std area
  - Statistics table: per-cell ID, pixel area, percent area
  - Histogram: cell size distribution chart
  - Download buttons: overlay PNG, masks.npy

User can then:
  - Adjust opacity slider → re-renders overlay client-side
  - Download overlay PNG → the temp file from Phase 8
  - Download masks.npy → raw segmentation data for further analysis
  - Click "Download Statistics CSV" → export_csv() converts DataFrame to CSV
```

### Complete end-to-end timeline summary

| Time | What happens | Where |
|------|-------------|-------|
| t=0s | User clicks "Segment" | Browser |
| t=0s | Gradio queues the callback | App Container |
| t=0.1s | `_encode_png()` converts numpy → PNG bytes | App Container |
| t=0.1s | `httpx.post()` sends image to Model Container | Network (cluster) |
| t=0.2s | FastAPI validates image (size, format, dimensions) | Model Container |
| t=0.2s | Semaphore acquired (or waits if another job is running) | Model Container |
| t=0.2s | `run_in_executor()` dispatches to thread pool | Model Container |
| t=5–20s | Cellpose neural network forward pass (cyto3 on GPU) | Model Container GPU |
| t=5–20s | Flow field integration → cell masks | Model Container CPU |
| t=20s | DB INSERT for job record | Model Container → PostgreSQL |
| t=20s | `np.save()` → `.npy` bytes → HTTP response | Model Container |
| t=20s | App Container deserializes masks | App Container |
| t=20s | `_render_overlay()` composites colors | App Container |
| t=20s | `_compute_stats()` calculates areas | App Container |
| t=21s | `plt.hist()` renders histogram | App Container |
| t=21s | Temp files written | App Container disk |
| t=21s | Gradio returns to browser via WebSocket | App Container |
| t=21s | Browser updates all output components | Browser |
| **Total** | **~20 seconds (GPU, cyto3, normal image)** | |

For cpsam model on large images: 2–20 minutes GPU, potentially hours on CPU.

---

## 12. Authentication Flow

Authentication is entirely DB-backed. There is no session token or JWT — Gradio manages sessions internally using its built-in `auth=` callback.

### Login

```
Browser → POST /login (Gradio internal endpoint)
          data: {username: "alice", password: "secret"}
          ↓
          Gradio calls _auth_fn("alice", "secret")
          ↓
          App Container → POST http://cellpose-poc-model:8000/auth/login
                          JSON: {username: "alice", password: "secret"}
          ↓
          Model Container:
            1. SELECT password_hash, is_admin FROM users WHERE username='alice'
            2. bcrypt.checkpw("secret".encode(), password_hash.encode())
            3. Return: {"valid": true, "is_admin": false}
          ↓
          _auth_fn returns True → Gradio allows login
          Gradio stores username in session → available as request.username
          in all callback functions
```

### Registration

```
Browser → GET /register
          ↓
          FastAPI returns HTML form page

Browser → POST /register (JavaScript fetch from the HTML form)
          JSON: {username: "bob", password: "mypassword"}
          ↓
          App Container → POST http://cellpose-poc-model:8000/auth/register
          ↓
          Model Container:
            1. Validate username: regex ^[a-zA-Z0-9_]{3,50}$
            2. Validate password: len >= 8
            3. pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt())
            4. INSERT INTO users (username, password_hash, is_admin) VALUES (...)
            5. ON CONFLICT → HTTP 400 "Username already taken"
          ↓
          App Container returns success/error to browser as JSON
          Browser shows colored message box (green = success, red = error)
```

### Admin account seeding

The admin account is created automatically at Model Container startup — no manual SQL needed:

```python
# In lifespan() — runs once per container start
if ADMIN_USER and ADMIN_PASSWORD:
    pw_hash = bcrypt.hashpw(ADMIN_PASSWORD.encode(), bcrypt.gensalt()).decode()
    cur.execute("""
        INSERT INTO users (username, password_hash, is_admin)
        VALUES (%s, %s, TRUE)
        ON CONFLICT (username) DO NOTHING
    """, (ADMIN_USER, pw_hash))
```

`ON CONFLICT DO NOTHING` makes this idempotent — restarting the model pod does not overwrite an existing admin account.

---

*Document generated: April 29, 2026. Matches codebase at POC version 1.*
