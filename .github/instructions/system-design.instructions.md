---
description: "Use when writing, modifying, or reviewing any code in this project. Enforces adherence to the system design document and requires documentation + changelog entries after every change."
applyTo: "**"
---

# System Design Compliance

All code changes MUST conform to the architecture defined in `document/system_design.md`. Read it before making changes.

## Architecture Constraints

- **Three services**: App Container (Gradio/FastAPI) + Model Container (FastAPI/Cellpose) + Database (PostgreSQL 16)
- **App Container**: Single `app.py` using `gr.mount_gradio_app`. No Django, no Flask, no Celery, no Redis
- **Model Container**: FastAPI with Cellpose. Must implement `GET /health`, `GET /parameters`, `POST /segment`, `POST /auth/register`, `POST /auth/login`, `GET /projects`
- **Model response contract**: `POST /segment` returns `masks.npy` as `application/octet-stream`
- **Internal network**: Model Container and Database are never exposed to the host — `expose`, not `ports`
- **Stateless results**: Segmentation outputs are ephemeral; users download what they need
- **Model weights**: Baked into Docker image at build time via `Dockerfile` `RUN` step
- **Two deployment paths**: `compose.yaml` = local CPU dev; `helm-chart/` = Kubernetes server with GPU

## Code Rules

- Do NOT add services beyond what the design document specifies (no Celery, no Redis, no CVAT/nuclio)
- Do NOT add dependencies not listed in the design document without updating it first
- Do NOT change the API contract (`/health`, `/parameters`, `/segment`, `/auth/*`, `/projects`) without updating the design document
- Do NOT add GPU device reservations to `compose.yaml` — GPU is Helm chart only
- If a change conflicts with the design document, update the design document FIRST, then implement

## After Every Change — Documentation Required

After completing any code modification, you MUST:

1. **Summarize** what changed and why (rationale)
2. **List files modified** with a one-line description per file
3. **Assess architecture impact**: does this change affect `system_design.md`? If yes, update it
4. **Append to CHANGELOG.md** following Keep a Changelog format

## CHANGELOG.md Format

Follow [Keep a Changelog](https://keepachangelog.com/) with these sections:

```markdown
## [version] - YYYY-MM-DD

### Added
- New features or files

### Changed
- Modifications to existing functionality

### Fixed
- Bug fixes

### Removed
- Removed features or files
```

Use `[Unreleased]` for changes not yet tagged with a version.

