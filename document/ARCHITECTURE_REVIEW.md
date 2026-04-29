# Architecture Review & Problems

**Date**: 2026-04-29  
**Purpose**: Document current state, identify problems, explain fixes based on official standards

---

## Current State Analysis

### ❌ PROBLEM 1: Dockerfile Cannot Build

**File**: `Model_container/Dockerfile`

**What it tries to do:**
```dockerfile
ARG BASE_IMAGE=10.136.94.110:32000/cellpose-poc-model-base:stable
FROM ${BASE_IMAGE}
COPY cellpose_api/app.py .
COPY cellpose_api/tasks.py .
```

**Problems:**
1. ❌ `Dockerfile.base` does NOT exist (two-layer build is incomplete)
2. ❌ `tasks.py` does NOT exist (was deleted, only `app.py` exists)
3. ❌ Hardcoded private server IP `10.136.94.110:32000` won't work on developer machines

**Impact:**
- `docker compose up --build` **FAILS** (passes `USE_CUDA` but Dockerfile expects `BASE_IMAGE`)
- GitLab CI build **FAILS** (references `Dockerfile.base` that doesn't exist)
- Cannot build image locally or on server

---

### ❌ PROBLEM 2: Build Arg Mismatch

**compose.yaml** (line 46):
```yaml
args:
  USE_CUDA: "false"
```

**Dockerfile** (line 6):
```dockerfile
ARG BASE_IMAGE=10.136.94.110:32000/...
```

**Problem:** compose.yaml passes `USE_CUDA`, Dockerfile expects `BASE_IMAGE`

**Official Docker Standard** ([docs.docker.com/engine/reference/builder/#arg](https://docs.docker.com/engine/reference/builder/#arg)):
- Build args must match between `docker build --build-arg` and `ARG` in Dockerfile
- Multi-stage builds should use `FROM base AS stage` pattern, not external base images

---

### ❌ PROBLEM 3: Helm Chart Missing Admin Credentials

**compose.yaml** (lines 58-59):
```yaml
- ADMIN_USER=admin
- ADMIN_PASSWORD=${ADMIN_PASSWORD}
```

**helm-chart/values.yaml**:
```yaml
db:
  password: cellseg
# ❌ NO admin section
```

**helm-chart/templates/deployment.yaml** (model container):
```yaml
env:
  - name: USE_GPU
  - name: DATABASE_URL
  # ❌ Missing ADMIN_USER
  # ❌ Missing ADMIN_PASSWORD
```

**Problem:** Admin account won't seed on Kubernetes deployment

**Official Kubernetes Standard** ([kubernetes.io/docs/concepts/configuration/secret/](https://kubernetes.io/docs/concepts/configuration/secret/)):
- Sensitive data should be in values.yaml or Secrets
- All containers in a deployment should have consistent env vars

---

## Why These Problems Exist

### Original Intent: Two-Layer Build for Speed

The Dockerfile comments explain the goal:
> "Code-only layer — built every commit, pulls in seconds. The heavy base (CUDA torch + weights) is in Dockerfile.base"

**Official Docker Pattern** ([docs.docker.com/build/building/multi-stage/](https://docs.docker.com/build/building/multi-stage/)):
- ✅ Multi-stage builds reduce image size
- ✅ Layer caching speeds up rebuilds
- ❌ But: relying on external base image (`FROM ${BASE_IMAGE}`) requires the base to exist in a registry

**Problem:** `Dockerfile.base` was deleted or never created, breaking the entire build chain.

---

## Solution Options

### Option A: Restore Two-Layer Build (Advanced)

**Create `Model_container/Dockerfile.base`:**
```dockerfile
FROM python:3.11-slim
ARG USE_CUDA=false
RUN apt-get update && apt-get install -y curl build-essential
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN if [ "$USE_CUDA" = "true" ]; then \
      pip install --force-reinstall torch torchvision \
        --index-url https://download.pytorch.org/whl/cu121; \
    fi
RUN python -c "from cellpose import models; \
    models.CellposeModel(gpu=False, pretrained_model='cyto3'); \
    models.CellposeModel(gpu=False, pretrained_model='cpsam')"
```

**Update `Model_container/Dockerfile`:**
```dockerfile
ARG BASE_IMAGE=localhost:32000/cellpose-poc-model-base:stable
FROM ${BASE_IMAGE}
COPY cellpose_api/app.py .
# Remove tasks.py line
```

**Pros:**
- Faster builds after first base image is built
- Base can be tagged `:stable` and reused

**Cons:**
- Requires building and pushing base image to registry first
- Local development can't just `docker compose up` (base not in local Docker)
- More complex CI pipeline (two build stages)

---

### Option B: Single Self-Contained Dockerfile (Recommended)

**Replace `Model_container/Dockerfile`:**
```dockerfile
FROM python:3.11-slim
ARG USE_CUDA=false

RUN apt-get update && apt-get install -y curl build-essential && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN if [ "$USE_CUDA" = "true" ]; then \
      pip install --no-cache-dir --force-reinstall torch torchvision \
        --index-url https://download.pytorch.org/whl/cu121; \
    fi

RUN python -c "from cellpose import models; \
    models.CellposeModel(gpu=False, pretrained_model='cyto3'); \
    models.CellposeModel(gpu=False, pretrained_model='cpsam')"

COPY cellpose_api/app.py .
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=5 \
    CMD curl -f http://localhost:8000/health || exit 1
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-keep-alive", "620"]
```

**Pros:**
- Works everywhere (local Mac, Linux, CI, server)
- No external dependencies
- Official Docker best practice ([docs.docker.com/develop/dev-best-practices/](https://docs.docker.com/develop/dev-best-practices/))
- Matches what `compose.yaml` expects (`USE_CUDA` arg)

**Cons:**
- Every build includes dependencies (~2-3 min first build, but Docker layer caching helps)

---

## Fix for Helm Chart

**Add to `helm-chart/values.yaml`:**
```yaml
# --- Authentication ---
admin:
  username: admin
  password: OstfaliaAdmin2026
```

**Update `helm-chart/templates/deployment.yaml` (model container env section):**
```yaml
env:
  - name: PYTHONUNBUFFERED
    value: "1"
  - name: USE_GPU
    value: {{ .Values.model.useGpu | quote }}
  - name: DATABASE_URL
    value: "postgresql://{{ .Values.db.user }}:{{ .Values.db.password }}@{{ .Release.Name }}-db:{{ .Values.db.port }}/{{ .Values.db.name }}"
  - name: ADMIN_USER
    value: {{ .Values.admin.username | quote }}
  - name: ADMIN_PASSWORD
    value: {{ .Values.admin.password | quote }}
```

**Why:** Matches `compose.yaml` pattern, follows Kubernetes ConfigMap/Secret best practices

---

## Official Standards References

### Docker
- **Multi-stage builds**: https://docs.docker.com/build/building/multi-stage/
- **ARG and ENV**: https://docs.docker.com/engine/reference/builder/#arg
- **Best practices**: https://docs.docker.com/develop/dev-best-practices/

### Kubernetes
- **Environment variables**: https://kubernetes.io/docs/tasks/inject-data-application/define-environment-variable-container/
- **Secrets**: https://kubernetes.io/docs/concepts/configuration/secret/
- **ConfigMaps**: https://kubernetes.io/docs/concepts/configuration/configmap/

### Gradio
- **gr.mount_gradio_app**: https://www.gradio.app/guides/sharing-your-app#mounting-within-another-fastapi-app
- **Authentication**: https://www.gradio.app/guides/sharing-your-app#authentication

---

## Recommended Action

**Option B** (single self-contained Dockerfile) is recommended because:

1. ✅ Works with existing `compose.yaml` (already passes `USE_CUDA`)
2. ✅ No external dependencies
3. ✅ Follows official Docker best practices
4. ✅ Simpler CI pipeline
5. ✅ Docker layer caching still speeds up rebuilds

**Add Helm credentials** to match `compose.yaml` and `.env.example` patterns.

---

## Why This Review Was Needed

You were correct — I was trying to "fix" things without understanding your deployment strategy. The two-layer build (`Dockerfile.base` + `Dockerfile`) is a valid optimization, but:

- `Dockerfile.base` doesn't exist, so builds fail
- `tasks.py` doesn't exist, so COPY fails
- Build args don't match between local and CI

This document explains:
- ✅ What exists
- ✅ What's broken
- ✅ Why it's broken
- ✅ How to fix it (with official standard references)
- ✅ Trade-offs of each approach

**Decision is yours:** restore two-layer build OR switch to self-contained Dockerfile.
