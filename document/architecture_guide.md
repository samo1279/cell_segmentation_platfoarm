# Architecture Guide: Cell Segmentation Platform

A comprehensive walkthrough of the software architecture behind the Cell Segmentation Platform POC — from first principles to production Kubernetes deployment.

---

## Table of Contents

1. [Introduction and Motivation](#1-introduction-and-motivation)
2. [Monolith vs Microservices](#2-monolith-vs-microservices)
3. [Why Microservices for Cell Segmentation](#3-why-microservices-for-cell-segmentation)
4. [Containerisation with Docker](#4-containerisation-with-docker)
5. [Local Development with Docker Compose](#5-local-development-with-docker-compose)
6. [The Three-Service Design](#6-the-three-service-design)
7. [API Design and Contract](#7-api-design-and-contract)
8. [Network and Security Architecture](#8-network-and-security-architecture)
9. [Kubernetes Concepts and Cluster Setup](#9-kubernetes-concepts-and-cluster-setup)
10. [Helm: Parameterised Kubernetes Deployments](#10-helm-parameterised-kubernetes-deployments)
11. [GPU Inference in Kubernetes](#11-gpu-inference-in-kubernetes)
12. [CI/CD Pipeline](#12-cicd-pipeline)
13. [Trade-offs and Future Work](#13-trade-offs-and-future-work)

---

## 1. Introduction and Motivation

### What Problem Are We Solving?

Cell segmentation — the identification and delineation of individual cells in microscopy images — is a foundational step in quantitative biology. Researchers at Ostfalia and partner institutions routinely process large batches of fluorescence and bright-field microscopy images to measure cell area, count, morphology, and spatial arrangement.

Until recently, the dominant workflow was to run segmentation tools locally on a researcher's laptop. This created three compounding problems:

1. **Compute bottleneck**: Large TIFF images (2000×2000 pixels, 16-bit, multi-channel) take 5–20 minutes per image on a CPU. A batch of 100 images ties up a researcher's workstation for days.
2. **Reproducibility**: Different researchers running different versions of segmentation software — with different parameter sets, on different operating systems — produce results that cannot be meaningfully compared.
3. **Data governance**: Cloud-based tools like the HuggingFace Cellpose Space solve the compute problem but require uploading patient-adjacent biological imaging data to a third-party server, conflicting with GDPR obligations and institutional data governance policies.

The Cell Segmentation Platform addresses all three problems: it centralises compute on an on-premise GPU server, enforces consistent software versions via containers, and keeps all data within the institution's network.

### Design Philosophy

The platform is built around three principles:

- **Simplicity over ceremony**: Three services, no message queues, no caches, no service meshes. Everything needed for a viable research tool, nothing more.
- **Security by default**: Data never leaves the internal network. Passwords are hashed, secrets are injected at runtime, HTTP responses include security headers.
- **Two-speed deployment**: A laptop-friendly Docker Compose stack for development and testing; a Helm-managed Kubernetes deployment for production use with GPU acceleration.

---

## 2. Monolith vs Microservices

### The Monolith

A monolithic application packages all functionality — user interface, business logic, data access — into a single deployable unit. This is how most software begins. A researcher-written Python script that loads an image, runs Cellpose, and saves the result is a monolith: one process, one environment, one deployment step.

Monoliths have genuine advantages:
- Simple to develop and debug in the early stages
- No network latency between components
- Easy to test as a whole
- Trivial deployment (one process to start)

The problems emerge with scale. In a monolith, the Gradio UI, the Cellpose inference engine, and the database connection share a process. If you want to upgrade Cellpose from version 2 to version 3, you must also test and redeploy the UI. If inference consumes all available GPU memory, the UI becomes unresponsive. If the database password changes, the entire application restarts.

### Microservices

Microservices decompose an application into a set of independently deployable services, each responsible for a single capability. Martin Fowler and James Lewis, in their landmark 2014 article, described the style as "an approach to developing a single application as a suite of small services, each running in its own process and communicating with lightweight mechanisms" (Fowler & Lewis, 2014, *"Microservices"*, martinfowler.com).

The key properties of a microservices architecture are:
- **Single responsibility**: Each service does one thing and does it well (the Unix philosophy applied at the service level)
- **Independent deployability**: Services can be updated, scaled, and restarted without touching their neighbours
- **Technology heterogeneity**: Each service can use the programming language and framework best suited to its task
- **Failure isolation**: A crash in one service does not cascade to others

The Cloud Native Computing Foundation (CNCF) has further formalised this into the concept of *cloud-native applications*: "systems that are container-packaged, dynamically scheduled, and microservices-oriented" (CNCF, 2018, *Cloud Native Definition v1.0*).

### The Spectrum

It is important to recognise that monolith and microservices are endpoints on a spectrum, not a binary choice. The academic literature increasingly discusses *modular monoliths* and *service-oriented architectures* as intermediate positions. For this project, we chose a three-service decomposition — which is closer to the microservices end — but deliberately avoided fine-grained decomposition to keep operational complexity manageable for a two-person research team.

---

## 3. Why Microservices for Cell Segmentation

Given the trade-offs above, the decision to build this platform as three services rather than a single application was driven by concrete technical constraints.

### Constraint 1: Dependency Isolation

Cellpose has a complex and brittle dependency tree. Version 3 (`cyto3`, `cpsam`) requires PyTorch ≥ 2.0, which in GPU mode requires CUDA 12.x. The Gradio UI requires Python 3.11 and a set of web-facing packages (`fastapi`, `httpx`, `Pillow`, `matplotlib`). On a developer's macOS laptop, these two dependency trees conflict.

By putting Cellpose in its own container, the Model Container can install CUDA-enabled PyTorch wheels without affecting the App Container. The App Container can update Gradio without risking Cellpose compatibility.

### Constraint 2: Independent Scaling

In the Kubernetes production environment, inference is the bottleneck. A single GPU node (`imeca40.imec.local`) with four NVIDIA A40 cards can run four Cellpose inferences in parallel. The UI does not benefit from replication at the same rate — it is primarily I/O-bound (file uploads and downloads).

Kubernetes Horizontal Pod Autoscalers can scale the Model Container deployment based on GPU utilisation without scaling the App Container or the database.

### Constraint 3: Model Update Independence

Cellpose releases new model weights regularly. Updating the model weights requires rebuilding only the Model Container image (a 6.8 GB operation involving CUDA wheel downloads). The App Container image (~300 MB, no GPU dependencies) does not need to change. The CI/CD pipeline implements this explicitly: the model image tag is derived from a content hash of the `Model_container/` directory. If only UI code changed, the pipeline reuses the existing model image and Kubernetes skips the 6.8 GB pull.

### Constraint 4: Security Boundary

The segmentation endpoint handles raw image data. By isolating it behind an internal-only network, we ensure that the Model Container API is never directly reachable from outside the cluster. All external requests enter through the App Container, which applies rate limiting, authentication, and security headers before proxying to the model.

---

## 4. Containerisation with Docker

### What is a Container?

A Docker container is a lightweight, isolated runtime environment that packages an application with all of its dependencies. Unlike a virtual machine, a container shares the host operating system kernel. This makes containers start in milliseconds and consume a fraction of the resources of a VM.

Containers are built from *images*: layered, immutable file systems specified by a `Dockerfile`. Each instruction in a `Dockerfile` adds a layer. Layers are cached, so rebuilding an image after a small code change reuses all the earlier layers.

### The App Container Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

EXPOSE 8001
CMD ["python", "app.py"]
```

The pattern here is deliberate. `requirements.txt` is copied and installed *before* `app.py` is copied. This means that as long as `requirements.txt` does not change between commits, Docker reuses the cached pip install layer. A developer who changes only `app.py` experiences a rebuild that takes seconds, not minutes.

The `COPY templates/ templates/` instruction copies the three HTML template files (`landing.html`, `signin.html`, `register.html`) into the image. These pages are served by FastAPI before the Gradio authentication layer, so they must be inside the container.

The `EXPOSE 8001` instruction is documentation — it does not actually publish the port. Port publication happens at runtime via `docker compose` or Kubernetes `Service` definitions.

### The Model Container Dockerfile

The Model Container uses a two-stage build strategy controlled by a build argument:

```dockerfile
ARG USE_CUDA=false
FROM python:3.11-slim

RUN if [ "$USE_CUDA" = "true" ]; then \
      pip install torch --index-url https://download.pytorch.org/whl/cu121; \
    else \
      pip install torch --index-url https://download.pytorch.org/whl/cpu; \
    fi
```

For local development (`compose.yaml`), `USE_CUDA=false` installs a CPU-only PyTorch wheel (~800 MB). For the Kubernetes build (GitLab CI), `USE_CUDA=true` installs CUDA-enabled wheels. The same `Dockerfile` serves both use cases.

Cellpose model weights are **baked into the image at build time** via a `RUN` step that downloads and caches the `cyto3` weights during the Docker build. This adds ~500 MB to the image but ensures that the container is self-contained: it does not attempt to download weights at runtime, which would fail in an air-gapped cluster or under load.

### Image Tagging Strategy

- **App image**: Tagged with the short Git commit SHA (`$CI_COMMIT_SHORT_SHA`). Every commit to `main` produces a new tag like `b3f1a29`.
- **Model image**: Tagged with `model-<sha256>` where the hash is computed from the contents of `Model_container/`. This content-addressed tagging means the model image tag changes *only when model code changes*, regardless of how many UI commits happened since the last model change. Kubernetes's `imagePullPolicy: IfNotPresent` then skips the expensive re-pull automatically.

---

## 5. Local Development with Docker Compose

### What is Docker Compose?

Docker Compose is a tool for defining and running multi-container applications. A single `compose.yaml` file describes all the services, their networks, and their environment variables. `docker compose up --build` builds images, creates a shared network, and starts all containers in the correct order.

### The compose.yaml File

```yaml
services:
  app:
    build: ./App_container
    ports:
      - "8001:8001"         # Only the App Container is exposed to the host
    environment:
      - MODEL_URL=http://model:8000/segment
    depends_on:
      - model

  model:
    build: ./Model_container
    expose:
      - "8000"              # Internal only — not reachable from the host
    environment:
      - DATABASE_URL=postgresql://cellseg:${POSTGRES_PASSWORD:-cellseg}@db:5432/cellseg
      - USE_GPU=false
    depends_on:
      - db

  db:
    image: postgres:16-alpine
    expose:
      - "5432"              # Internal only
    environment:
      - POSTGRES_USER=cellseg
      - POSTGRES_PASSWORD=${POSTGRES_PASSWORD:-cellseg}
    volumes:
      - pgdata:/var/lib/postgresql/data

volumes:
  pgdata:
```

The key architectural point here is the distinction between `ports` and `expose`:

- `ports: "8001:8001"` maps host port 8001 to container port 8001. The service is reachable from the developer's browser.
- `expose: "8000"` registers port 8000 on the internal Docker network only. The service is reachable from other containers on the same network (e.g., `http://model:8000`) but not from the developer's host machine.

This mirrors the production architecture: in Kubernetes, the Model Container and Database are `ClusterIP` services — reachable only within the cluster, never from the internet.

### Service Discovery

Within the Compose network, each service is reachable by its service name as a hostname. The App Container connects to the Model Container using `http://model:8000`. Docker's embedded DNS resolves `model` to the model container's IP address. No IP addresses are hardcoded.

In Kubernetes, the same principle applies via CoreDNS: `http://cellpose-poc-model:8000` resolves to the model `Service`'s ClusterIP.

---

## 6. The Three-Service Design

### App Container

The App Container is the user-facing component. It combines two frameworks in a single process:

1. **FastAPI** provides the HTTP server and handles routes that must work before authentication: `/` (landing page), `/sign-in`, `/register`, `/auth/register` (registration proxy), `/healthz` (Kubernetes probe).
2. **Gradio** provides the ML interface — parameter sliders, file upload, result display — and is mounted inside FastAPI using `gr.mount_gradio_app(app, demo, path="/app")`. Gradio's built-in authentication layer protects the `/app` route.

The reason for this two-framework approach is architectural: Gradio's authentication system redirects unauthenticated users to `/app/login`, which returns a JSON response. If the user's browser visits `/app` without a session cookie, they receive `{"success": false}` instead of a sign-in form. The solution is to interpose FastAPI routes that serve proper HTML pages *before* the Gradio mount processes the request.

**Security headers middleware** is applied by FastAPI to every response:

```python
@app.middleware("http")
async def _security_and_routing_middleware(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "0"
    return response
```

These headers address OWASP Top 10 A05 (Security Misconfiguration):
- `X-Content-Type-Options: nosniff` — prevents MIME-type sniffing attacks
- `X-Frame-Options: DENY` — prevents clickjacking
- `Referrer-Policy: strict-origin-when-cross-origin` — limits referrer data leakage
- `X-XSS-Protection: 0` — modern guidance is to disable this header (which triggers legacy browser heuristics that can themselves be exploited) and rely on CSP instead

### Model Container

The Model Container is the computational heart of the system. It is a FastAPI application with the following endpoints:

- `GET /health` — returns 200 when the default model (`cyto3`) is loaded into memory. Returns 503 while loading. Used by Kubernetes `readinessProbe` and `startupProbe`.
- `GET /parameters` — returns the JSON schema of all tunable parameters so the UI can render appropriate sliders without hardcoding valid ranges.
- `POST /segment` — accepts a multipart form submission containing an image file and parameter values, runs Cellpose inference, and returns the resulting mask array as a NumPy `.npy` binary file (`application/octet-stream`).
- `POST /auth/register` — accepts a JSON body `{"username": "...", "password": "..."}`, validates the input, bcrypt-hashes the password, and inserts a row into the `users` table.
- `POST /auth/login` — verifies credentials against the database. Returns `{"valid": true}` or `{"valid": false}`.
- `GET /projects` — returns segmentation history from the `projects` table. Admin users see all records; regular users see only their own.

**Lazy model loading**: `cyto3` is loaded at startup so the first request is fast. `cpsam` (the larger SAM-backbone model) is loaded on first request and cached in memory. This reduces startup time while ensuring `cpsam` is available when needed.

### Database

PostgreSQL 16 stores two tables:

**`users`**
```sql
CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,    -- bcrypt hash
    is_admin BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**`projects`**
```sql
CREATE TABLE projects (
    id SERIAL PRIMARY KEY,
    username VARCHAR(50) NOT NULL,
    model_type VARCHAR(20),
    diameter REAL,
    flow_threshold REAL,
    cellprob_threshold REAL,
    cell_count INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);
```

The database is never accessible outside the internal network. No direct connection from the browser to the database is possible by design. All database access goes through the Model Container API, which validates inputs, enforces access control (admin vs regular user), and handles errors gracefully.

---

## 7. API Design and Contract

### RESTful Principles

The API follows REST conventions with some pragmatic adaptations:

- Resources are identified by path: `/projects`, `/auth/register`, `/health`
- HTTP methods convey intent: `GET` for reads, `POST` for writes and actions
- Responses use appropriate status codes: `200` (success), `400` (bad input), `401` (unauthorised), `422` (validation failure), `500` (server error), `503` (service unavailable)

The `/segment` endpoint is technically a *command* (not a resource CRUD operation), which is why it uses `POST` rather than the more REST-pure `PUT` pattern. This is a common and accepted deviation.

### The `POST /segment` Contract

The most important API contract is the segmentation endpoint. It accepts:

```
Content-Type: multipart/form-data

image:               <binary file>   # PNG, TIFF, or JPEG; max 50 MB
model_type:          "cyto3"         # or "cpsam"
diameter:            30.0            # float; 0.0 = auto-detect
flow_threshold:      0.4             # float; 0.0 to 3.0
cellprob_threshold:  0.0             # float; -6.0 to 6.0
username:            "alice"         # for audit log
```

And returns:

```
Content-Type: application/octet-stream
Content-Disposition: attachment; filename="masks.npy"

<NumPy .npy binary>
```

The mask array is a 2D integer array of shape `(H, W)`, where each unique non-zero integer identifies one cell. The App Container receives this binary, loads it with `numpy.load(io.BytesIO(content))`, and uses it to render the coloured overlay.

This binary contract was chosen over JSON because the masks for a 2000×2000 image contain four million integers. Serialising them to JSON would produce a ~30 MB string. The `.npy` binary format is ~16 MB and parses in milliseconds.

### Versioning

The current API is unversioned (`/segment`, not `/v1/segment`). For a POC with one consumer (the App Container), explicit versioning adds complexity without benefit. If the Model Container API is ever opened to external consumers, semantic versioning should be introduced at the URL path level.

---

## 8. Network and Security Architecture

### Network Topology

```
Internet
    │ HTTPS (443)
    ▼
nginx Ingress Controller
    │ cert-manager TLS termination
    │ HTTP (80) → HTTPS redirect
    ▼
App Container Service (ClusterIP: 8001)
    │ HTTP (8000) — internal cluster network only
    ▼
Model Container Service (ClusterIP: 8000)
    │ TCP (5432) — internal cluster network only
    ▼
PostgreSQL Service (ClusterIP: 5432)
```

TLS is terminated at the Ingress. All traffic between the Ingress and the App Container, and between all internal services, is plain HTTP over the private cluster network. This is the standard Kubernetes architecture and is explicitly endorsed by Kubernetes documentation: "It is not necessary to use TLS for inter-service communication within a cluster, since the cluster network itself is private and trusted."

### Authentication Flow

1. User visits `https://cellpose-poc.g007.imec.local`
2. App Container serves the landing page (`templates/landing.html`)
3. User navigates to `/sign-in`, receives the sign-in form (`templates/signin.html`)
4. Browser submits credentials via JavaScript `fetch('/app/login', ...)` with `credentials: 'include'`
5. Gradio processes the login request by calling the App Container's `_auth_fn` function
6. `_auth_fn` makes an HTTP POST to `http://model:8000/auth/login` with the credentials
7. Model Container queries PostgreSQL, computes `bcrypt.checkpw(password, stored_hash)`, returns `{"valid": true}` or `{"valid": false}`
8. On success, Gradio sets an `access-token` cookie and the browser is redirected to `/app/`
9. All subsequent requests to `/app/*` are validated by Gradio using the session cookie

### Password Security

Passwords are hashed with bcrypt using a work factor of 12 rounds:

```python
import bcrypt
hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))
```

bcrypt has three properties that make it well-suited for password storage:
1. **Adaptive cost**: The work factor can be increased as hardware gets faster without invalidating existing hashes
2. **Built-in salt**: Each hash includes a unique random salt, preventing rainbow table attacks
3. **Slow by design**: 12 rounds means ~300ms per verification on a modern CPU, which is imperceptible to a human logging in but makes brute-force attacks impractical

### Kubernetes Secret for Database Password

In the Kubernetes deployment, the database password is injected at deploy time and stored as a Kubernetes `Secret`:

```yaml
# helm-chart/templates/secrets.yaml
apiVersion: v1
kind: Secret
metadata:
  name: {{ .Release.Name }}-secrets
stringData:
  db-password: {{ .Values.db.password | quote }}
```

The `db.password` value is passed to Helm via `--set-string db.password=$DB_PASSWORD`, where `DB_PASSWORD` is a GitLab CI/CD *protected variable* — visible only to pipelines running on protected branches (i.e., `main`). The password is never written to `values.yaml` in the repository.

The Secret is referenced in the model container's environment:

```yaml
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ .Release.Name }}-secrets
      key: db-password
- name: DATABASE_URL
  value: "postgresql://{{ .Values.db.user }}:$(DB_PASSWORD)@..."
```

### OWASP Top 10 Mitigations

| OWASP Category | Mitigation in this project |
|---|---|
| A01 Broken Access Control | User/admin separation enforced in Model Container API; session cookies set `HttpOnly` by Gradio |
| A02 Cryptographic Failures | bcrypt for passwords; TLS at Ingress; no sensitive data in URLs |
| A03 Injection | Parameterised SQL queries via `psycopg2` (no string concatenation); input validated by FastAPI/Pydantic |
| A05 Security Misconfiguration | Security headers on all responses; DB/model not exposed to host; secrets via env vars |
| A06 Vulnerable Components | Pinned dependency versions in `requirements.txt`; minimal base images (`python:3.11-slim`, `postgres:16-alpine`) |
| A09 Security Logging | All requests logged; authentication events logged at INFO level |

---

## 9. Kubernetes Concepts and Cluster Setup

### Why Kubernetes?

Docker Compose runs containers on a single machine. When that machine fails, the application goes down. Kubernetes distributes containers across a cluster of machines, automatically rescheduling workloads on healthy nodes when failures occur.

For this project, Kubernetes provides three specific benefits:
1. **GPU scheduling**: The NVIDIA GPU operator and device plugin allow Kubernetes to schedule pods that require specific GPU resources (`nvidia.com/gpu: 1`), ensuring inference pods land on the GPU node.
2. **Rolling updates**: Deploying a new version of the model container does not cause downtime — Kubernetes creates new pods before terminating old ones.
3. **Health management**: `livenessProbe`, `readinessProbe`, and `startupProbe` allow Kubernetes to detect and restart unhealthy pods, and to withhold traffic from pods that are still initialising (e.g., loading Cellpose weights).

### Cluster Topology

The production cluster runs MicroK8s on two nodes:

| Node | Role | Resources |
|---|---|---|
| `imeca40.imec.local` | GPU workload | 4× NVIDIA A40 (48 GB VRAM each), `role=gpu` label |
| `imecfs1.imec.local` | Management | No GPU, `role=management` label |

The model container pod is scheduled exclusively on the GPU node via `nodeSelector`:

```yaml
nodeSelector:
  role: gpu
```

The app container and PostgreSQL can run on either node.

### Kubernetes Resources

**Deployment**: Describes the desired state of a set of pods — which image to run, how many replicas, resource limits, environment variables, health checks. The Kubernetes controller ensures the actual state matches the desired state at all times.

**Service**: Provides a stable virtual IP address (ClusterIP) and DNS name for a set of pods. Even as pods are created and destroyed, the Service IP stays constant. Internal services (`model`, `db`) use `ClusterIP` type, which makes them reachable only within the cluster.

**Ingress**: A set of routing rules that maps external HTTP/HTTPS requests to internal Services. The nginx Ingress Controller reads `Ingress` objects and reconfigures the nginx reverse proxy accordingly.

**ConfigMap and Secret**: Kubernetes-native ways to inject configuration and sensitive data into pods as environment variables or mounted files. `Secret` values are base64-encoded in `etcd` and accessible only to pods in the same namespace (subject to RBAC policies).

**PersistentVolumeClaim (PVC)**: A request for persistent storage. The PostgreSQL pod uses a PVC to store its data files, so database data survives pod restarts.

### Health Probes

The model container has three probes:

```yaml
startupProbe:
  httpGet:
    path: /health
    port: 8000
  failureThreshold: 30    # Allows up to 5 minutes for model load
  periodSeconds: 10

readinessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10

livenessProbe:
  httpGet:
    path: /health
    port: 8000
  initialDelaySeconds: 30
  periodSeconds: 30
```

The `startupProbe` is critical: loading `cyto3` into GPU memory takes 60–120 seconds. Without a startup probe, the `livenessProbe` would declare the pod unhealthy and restart it before it is ready, creating an infinite restart loop. The startup probe disables the liveness probe until the pod passes its first health check.

### cert-manager and TLS

cert-manager is a Kubernetes add-on that automates the provisioning and renewal of TLS certificates. It integrates with the cluster's CA (for internal/private deployments) or with ACME providers like Let's Encrypt (for public domains).

In this cluster, a `ClusterIssuer` of type `ca` is configured with the institution's root certificate. When the Helm chart creates an `Ingress` with the annotation `cert-manager.io/cluster-issuer: ca-issuer`, cert-manager automatically generates a signed TLS certificate and stores it as a Kubernetes `Secret`. The nginx Ingress Controller reads that Secret and serves HTTPS.

---

## 10. Helm: Parameterised Kubernetes Deployments

### What is Helm?

Helm is the Kubernetes package manager. A Helm *chart* is a collection of YAML templates with a `values.yaml` file that provides default values for template variables. Users can override values at deploy time with `--set` or `--values` flags.

Without Helm, deploying the same application to a development cluster (where image tags are `latest` and resource limits are small) and a production cluster (where image tags are pinned SHAs and resource limits are large) requires maintaining two separate copies of the YAML manifests. Helm solves this with a single parameterised template.

### Chart Structure

```
helm-chart/
├── Chart.yaml          # Chart metadata: name, version, description
├── values.yaml         # Default values
└── templates/
    ├── deployment.yaml # Parameterised Deployments for all three services
    ├── services.yaml   # ClusterIP Services
    ├── ingress.yaml    # Ingress with TLS
    └── secrets.yaml    # Kubernetes Secret for DB password
```

### Template Example

In `deployment.yaml`, the app image is referenced as:

```yaml
image: {{ .Values.app.image.repository }}:{{ .Values.app.image.tag }}
```

At deploy time, the CI/CD pipeline passes:
```bash
--set-string app.image.repository=localhost:32000/cellpose-poc-app
--set-string app.image.tag=b3f1a29
```

Helm renders the template, substituting the values, and applies the resulting YAML to the cluster.

### Why `stringData` in Secrets

The `secrets.yaml` template uses `stringData` rather than `data`:

```yaml
stringData:
  db-password: {{ .Values.db.password | quote }}
```

The difference: `data` requires base64-encoded values; `stringData` accepts plain strings and lets Kubernetes handle the encoding. This is safer in Helm templates because base64 encoding in a Go template (`{{ .Values.db.password | b64enc }}`) can introduce encoding bugs if the value contains special characters. Using `stringData` delegates the encoding responsibility to the Kubernetes API server.

### Helm Upgrade Strategy

The pipeline uses:
```bash
helm upgrade --install cellpose-poc ./helm-chart --wait --timeout 12m0s
```

- `upgrade --install` is idempotent: it runs `helm install` on the first deploy and `helm upgrade` on subsequent deploys.
- `--wait` blocks until all pods reach the `Ready` state or the timeout expires.
- `--timeout 12m0s` provides enough time for the model container to pull the ~6.8 GB image and load Cellpose weights into GPU memory.

The pipeline also includes:
```bash
helm rollback ${APP_NAME} --namespace ${APP_NAME} 2>/dev/null || true
```
This runs *before* `helm upgrade --install`. If a previous deploy was interrupted mid-operation, Helm leaves a release in `pending-upgrade` state that blocks future upgrades. The rollback command clears this lock. If no previous release exists, the rollback silently fails and the install proceeds normally.

---

## 11. GPU Inference in Kubernetes

### NVIDIA GPU Operator

The NVIDIA GPU Operator is a Kubernetes operator that automates the deployment of all software components needed to use NVIDIA GPUs in Kubernetes: drivers, container toolkit, device plugin, monitoring. Once installed, pods can request GPU resources with standard Kubernetes resource syntax.

### Requesting GPU Resources

In the model container deployment:

```yaml
resources:
  limits:
    nvidia.com/gpu: 1
```

This tells the Kubernetes scheduler to place this pod only on nodes that have at least one available NVIDIA GPU. The `nvidia.com/gpu` resource is registered by the NVIDIA device plugin, which runs as a DaemonSet on every GPU-capable node and advertises the available GPUs to the Kubernetes scheduler.

### GPU Memory Management

Each NVIDIA A40 has 48 GB of VRAM. The `cyto3` model requires approximately 4 GB; `cpsam` requires approximately 14 GB. With `nvidia.com/gpu: 1`, the pod has exclusive access to one GPU. No other pod can schedule to that GPU until this pod releases it.

For workloads that could benefit from running multiple models on one GPU (e.g., if inference is fast and memory allows), NVIDIA Multi-Instance GPU (MIG) or Multi-Process Service (MPS) can be used. This is outside the scope of the current POC.

### CPU Fallback

When `USE_GPU=false` (local development), Cellpose falls back to CPU inference automatically:

```python
model = models.Cellpose(gpu=os.getenv("USE_GPU", "false").lower() == "true",
                        model_type=DEFAULT_MODEL_TYPE)
```

CPU inference on the `cyto3` model takes approximately 2–5 minutes for a 1000×1000 image. This is acceptable for development and testing but not for production use.

---

## 12. CI/CD Pipeline

### Pipeline Stages

The GitLab CI pipeline is defined in `.gitlab-ci.yml` and consists of four stages:

```
test → build → deploy → verify
```

All stages run on a GitLab runner tagged `a40gpu`, which is registered on `imeca40.imec.local` — the GPU node. This ensures that the runner has access to the MicroK8s cluster, the internal container registry, and the GPU for any GPU-dependent operations (none currently, but available if needed).

### Test Stage

```yaml
test:
  stage: test
  image: python:3.11-slim
  script:
    - pip install -r Model_container/tests/requirements-test.txt
    - PYTHONPATH=Model_container/cellpose_api pytest Model_container/tests/ -v
```

The test suite (31 tests) uses a stubbed Cellpose that returns predictable mock data. This means the tests run in ~10 seconds without downloading model weights or requiring a GPU. The stub is activated by setting `CELLPOSE_STUB=true` in the test environment.

Tests cover: input validation, authentication logic, segmentation parameter bounds, API response shapes, and error handling.

### Build Stage

The build stage uses Kaniko — a tool for building container images from inside a Kubernetes cluster without requiring a Docker daemon. Kaniko reads the `Dockerfile`, builds the image layer by layer, and pushes directly to the registry.

**App image**: Built from `App_container/Dockerfile`, tagged `$CI_COMMIT_SHORT_SHA` (the 8-character Git commit hash).

**Model image**: Built from `Model_container/Dockerfile` with `--build-arg USE_CUDA=true`. Tagged `model-$(sha256sum Model_container/...)` — a hash of the model source files. If model files have not changed, the tag is identical to the previous deploy and Kubernetes does not re-pull the image.

### Deploy Stage

```yaml
deploy:
  stage: deploy
  script:
    - helm lint ./helm-chart
    - helm template ... >/tmp/rendered.yaml    # Dry-run render for CI log
    - helm rollback ... 2>/dev/null || true    # Clear any stuck pending state
    - helm upgrade --install ... --wait --timeout 12m0s
```

The `helm template` dry run renders the manifests without applying them, capturing the output in the CI job log for debugging purposes. This makes it easy to see exactly which YAML was applied in any given deploy.

### Verify Stage

```yaml
verify:
  stage: verify
  when: always    # Runs even if deploy fails
  script:
    - kubectl get pods -n $APP_NAME
    - kubectl get events -n $APP_NAME --sort-by='.lastTimestamp' | tail -20
    - curl -sf https://$APP_DOMAIN/healthz
```

The `when: always` directive is important: if the deploy fails (e.g., a pod crashes), the verify stage still runs and dumps the pod state and events to the CI log. This dramatically reduces the time to diagnose a failed deploy.

---

## 13. Trade-offs and Future Work

### What We Deliberately Did Not Build

**Message queue (Celery + Redis)**: Many similar systems use an asynchronous task queue for long-running inference jobs. The pattern is: App Container sends a task to Redis, Celery worker picks it up, result is stored in a result backend. This enables job progress tracking, retries, and scaling workers independently of the API.

We did not build this because:
- A single inference job on the A40 GPU takes 5–15 seconds, not minutes
- The Gradio UI already handles the async/sync boundary; the user sees a spinner during inference
- Adding Redis and Celery would add two more services, two more Docker images, and significant operational complexity for a 1–2 developer team
- The system design document explicitly prohibits adding Celery and Redis without a design update

If inference times grow (e.g., batch jobs, 3D z-stacks with many frames), a task queue would become necessary. The natural migration path is: add Redis + Celery workers, change the `/segment` endpoint to return a `job_id`, add a `GET /jobs/{job_id}` polling endpoint, update the Gradio UI to poll for results.

**Service mesh (Istio/Linkerd)**: A service mesh provides mTLS between services, circuit breaking, distributed tracing, and fine-grained traffic policies. For a three-service POC with two internal connections (App→Model, Model→DB), the operational overhead of a service mesh is not justified. The internal cluster network is trusted; PostgreSQL's SSL mode can be enabled if the Model→DB connection needs encryption.

**Horizontal autoscaling**: The model container currently runs as a single replica. Kubernetes `HorizontalPodAutoscaler` (HPA) could scale it based on GPU utilisation or custom metrics. This is straightforward to add but requires exposing GPU metrics via the NVIDIA DCGM Exporter and a Prometheus adapter — approximately one day of work.

### Known Limitations

**Single GPU bottleneck**: The current architecture assigns one GPU exclusively to the model container. While this prevents memory contention, it also means only one inference job can run at a time. Concurrent users experience queuing. For a research group of 5–10 users with occasional batch jobs, this is acceptable. For larger concurrent loads, the model container should be scaled to multiple replicas and a load balancer placed in front.

**Segmentation results are ephemeral**: Mask arrays are returned as immediate HTTP responses and not stored. Users who lose the downloaded file must re-run the segmentation. Storing results in the database (as S3-compatible object storage or a large binary column) would address this but significantly increases storage costs and backup complexity.

**No rate limiting**: The `/segment` endpoint is not rate-limited. A malicious or misconfigured client could submit hundreds of concurrent requests, exhausting GPU memory. Rate limiting should be added at the Ingress level (nginx's `limit_req_zone`) and optionally at the FastAPI level.

### Architectural Alternatives Considered

**FastAPI only (no Gradio)**: A pure REST API with a separate frontend (React, Vue) would be more standard and more maintainable long-term. Gradio was chosen because it reduces the frontend development burden to near zero for a research prototype, and because Gradio components (sliders, file uploads, image display) map directly onto the ML parameters without writing JavaScript.

**Single container**: All three services could run in one container using `supervisord`. This would simplify local development (one `docker run` command) at the cost of mixed concerns, shared failure domains, and inability to scale components independently. For a production system serving multiple research groups, independent scaling justifies the extra complexity.

**Kubernetes Operators for model lifecycle**: A custom Kubernetes Operator could manage Cellpose model version deployments (downloading new weights, validating them, updating the deployment). This is standard practice at ML platform teams (Kubeflow, Seldon, KServe implement variations of this). For a single-model POC, a custom operator is over-engineering.

### Recommended Next Steps

1. **Add rate limiting at the Ingress** (`nginx.ingress.kubernetes.io/limit-rps: "2"`)
2. **Introduce HPA for the model container** triggered by a custom `inference_queue_depth` metric
3. **Add Content Security Policy header** to the HTML templates to prevent XSS
4. **Enable PostgreSQL SSL mode** for the Model→DB connection
5. **Implement result storage** in an S3-compatible store (MinIO is already popular in MicroK8s deployments via `microk8s enable minio`)
6. **Formalise the API with OpenAPI documentation** — FastAPI generates this automatically; add `/docs` to the model container for developer convenience

---

## References

- Fowler, M. & Lewis, J. (2014). *Microservices*. martinfowler.com. https://martinfowler.com/articles/microservices.html
- Cloud Native Computing Foundation (2018). *CNCF Cloud Native Definition v1.0*. github.com/cncf/toc. https://github.com/cncf/toc/blob/main/DEFINITION.md
- Stringer, C. & Pachitariu, M. (2021). *Cellpose: a generalist algorithm for cellular segmentation*. Nature Methods, 18(1), 100–106.
- Pachitariu, M. & Stringer, C. (2022). *Cellpose 2.0: how to train your own model*. Nature Methods, 19(12), 1634–1641.
- Burns, B., Grant, B., Oppenheimer, D., Brewer, E. & Wilkes, J. (2016). *Borg, Omega, and Kubernetes*. ACM Queue, 14(1).
- OWASP Foundation (2021). *OWASP Top 10*. owasp.org. https://owasp.org/www-project-top-ten/
- Helm Project (2024). *Helm Documentation*. helm.sh. https://helm.sh/docs/
- Kubernetes Documentation (2024). *Kubernetes Concepts*. kubernetes.io. https://kubernetes.io/docs/concepts/
