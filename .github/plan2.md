# Implementation Plan — POC v1 Cleanup

> **Status**: Implemented (2026-05-01)

## Problem Summary

The codebase had 11 issues that made it impossible to build locally and created dead code / misleading documentation:

| # | Problem | Impact |
|---|---|---|
| 1 | `Model_container/Dockerfile` started `FROM 10.136.94.110:32000/…` (private server IP) | Build fails everywhere else |
| 2 | Three docker-compose files with conflicting GPU/CPU configs | Confusing; only one is correct for local dev |
| 3 | `ADMIN_PASSWORD=" Ostfalia2025"` with leading space | Silent auth bug |
| 4 | CVAT service in compose but no nuclio runtime | Broken service definition |
| 5 | `tasks.py` — dead Celery code (no Celery in requirements) | Dead code noise |
| 6 | `cvat_serverless/` — dead nuclio serverless code | Dead code noise |
| 7 | App used internal undocumented `_gr_routes.App.create_app` | Breaks on every Gradio version bump |
| 8 | Login page shows plain text "Visit /register" — no link | Bad UX; users can't find registration |
| 9 | Two conflicting design docs; instructions file points to wrong one | Documentation drift |
| 10 | Binary `.npy` artifacts committed to repo | Repo bloat |
| 11 | `.env` not in `.gitignore`; no `.env.example` | Secrets could be committed |

---

## Architecture Decision: Two Deployment Paths

```
compose.yaml         →  Local developer machine (macOS/Linux, CPU)
helm-chart/          →  Production server (Kubernetes, NVIDIA GPU)
```

**Rule** (per system_design.md): Never add GPU device reservations to `compose.yaml`. Never use `docker compose` for the server — use Helm.

Ref: [Docker Compose official file naming](https://docs.docker.com/compose/compose-application-model/)

---

## Phase 1 — Model Container Dockerfile

**Decision**: Merge `Dockerfile.base` into `Dockerfile`. Use `ARG USE_CUDA=false` so the file works on any machine by default.

```
Local build (CPU):    docker compose up --build            # USE_CUDA=false (default)
Server image (GPU):   docker build --build-arg USE_CUDA=true …
```

**CUDA wheel swap** (Ref: [PyTorch Get Started](https://pytorch.org/get-started/locally/)):
```dockerfile
RUN if [ "$USE_CUDA" = "true" ]; then \
      pip install --force-reinstall torch torchvision \
        --index-url https://download.pytorch.org/whl/cu121; \
    fi
```

**Weight baking** — pre-download at build time so cold-start is ~0:
```dockerfile
RUN python -c "from cellpose import models; \
    models.CellposeModel(gpu=False, pretrained_model='cyto3'); \
    models.CellposeModel(gpu=False, pretrained_model='cpsam')"
```

**Files changed**: `Model_container/Dockerfile` (rewritten), `Model_container/Dockerfile.base` (deleted)

---

## Phase 2 — Single `compose.yaml`

**Decision**: Replace the three old compose files with one `compose.yaml` (official preferred name per Docker docs).

- CPU-only (`USE_CUDA: "false"` build-arg)
- 3 services: `app`, `model`, `db` (no CVAT)
- Secrets via `${ADMIN_PASSWORD}` and `${POSTGRES_PASSWORD}` read from `.env`
- Model Container uses `expose:` not `ports:` (internal only)
- Database uses `expose:` not `ports:` (internal only)

Ref: [Docker Compose `expose` vs `ports`](https://docs.docker.com/compose/compose-file/05-services/#expose)

**Files changed**: `compose.yaml` (new), `docker-compose.yml` + `docker-compose.cpu.yml` + `docker-compose.gpu.yml` (deleted)

---

## Phase 3 — Dead Code Removal

| File/Directory | Reason for deletion |
|---|---|
| `Model_container/Dockerfile.base` | Merged into `Dockerfile` |
| `Model_container/cellpose_api/tasks.py` | Celery tasks; Celery not in requirements, no Redis service |
| `Model_container/cvat_serverless/` | Nuclio serverless functions; no nuclio runtime in any service config |
| `results_masking/` | Binary `.npy` artifacts committed to git by mistake |

---

## Phase 4 — Official Gradio Mount API + Register Link

**Problem**: `app.py` used `gradio.routes._gr_routes.App.create_app(…)` — an internal private API not documented and not stable across versions. Also used `BaseHTTPMiddleware` from Starlette, which has known issues with streaming responses.

**Solution** (Ref: [Gradio `mount_gradio_app` docs](https://www.gradio.app/docs/gradio/mount_gradio_app)):
```python
from fastapi import FastAPI, Request

_fastapi_app = FastAPI()

@_fastapi_app.get("/register")
async def _register_page(): ...

@_fastapi_app.post("/auth/register")
async def _register_proxy(request: Request): ...

app = gr.mount_gradio_app(
    _fastapi_app,
    demo,
    path="/",
    auth=_auth_fn,
    auth_message=(
        "Cell Segmentation Platform — please log in.<br>"
        "No account yet? <a href='/register'>Register here</a>"
    ),
)
```

Key points:
- FastAPI routes defined before `gr.mount_gradio_app` take precedence over the Gradio mount at `"/"`
- `auth_message` renders as HTML — the `<a>` tag creates a clickable link on the login page
- `auth=callable` is the official pattern; callable receives `(username, password)` and returns `bool`

**Files changed**: `App_container/app.py`

---

## Phase 5 — Secrets and Documentation

### `.env.example` + `.gitignore`
- Create `.env.example` documenting `ADMIN_PASSWORD` and `POSTGRES_PASSWORD`
- Add `.env` to `.gitignore` so secrets are never committed

Ref: [Docker Compose environment variables](https://docs.docker.com/compose/how-tos/environment-variables/envvars-precedence/)

### `system_design.md`
- Replace `improved_system_design.md` + `improved_system_design_v2.md` with a single `system_design.md`
- Documents the 3-service architecture and both deployment paths in one place

### `.github/instructions/system-design.instructions.md`
- Update doc reference from `improved_system_design.md` → `system_design.md`
- Fix architecture description from "Two containers, no DB" → "Three services"
- Add explicit rule: no GPU in `compose.yaml`

### `CHANGELOG.md`
- Append a single cleanup entry covering all changes above

---

## Verification Checklist

After implementation, confirm:

- [ ] `grep -r "10.136.94.110" .` → zero results
- [ ] `grep -r "tasks\|celery\|redis" .` → zero results (outside `.git`)
- [ ] `ls results_masking/ 2>&1` → "No such file or directory"
- [ ] `cat compose.yaml` → no `nvidia` or `gpu` mentions
- [ ] `docker compose up --build` → starts cleanly on macOS
- [ ] `http://localhost:8001` → login page with "Register here" clickable link
- [ ] `http://localhost:8001/register` → registration form renders
- [ ] New account can register, log in, run segmentation
