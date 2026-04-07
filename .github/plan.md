## Plan: Phased Implementation — Cell Segmentation Platform

**TL;DR**: 4 phases, from fixing the broken foundation → working MVP → enhanced features → production. Phase 1 is the POC deliverable. Each subsequent phase is additive — nothing breaks.

---

### Phase 1: Foundation (POC v1 — MVP)
*Goal: Working 2-container app that segments a single image and shows results.*

**Phase 1A — Fix Infrastructure** (blocks everything else)
1. Fix `docker-compose.yml` — rename service to `model`, fix build context → `./Model_container`, switch `ports` to `expose`, add healthcheck
2. Create `App_container/` directory — `app.py`, `requirements.txt`, `Dockerfile`
3. Verify both containers build; App Container reaches `http://model:8000/health`

**Phase 1B — Model Container Hardening** (*parallel with 1C*)
4. Add input validation — 50 MB max, format whitelist (PNG/TIFF/JPEG), max 8192×8192
5. Add `USE_GPU` env var — replace hardcoded `gpu=False`
6. Add `GET /parameters` endpoint — return parameter schema as JSON
7. Add `curl` to Dockerfile for healthcheck CMD
8. Add structured error responses (422/500 with `detail` field)

**Phase 1C — Gradio App Core UI** (*parallel with 1B, depends on 1A*)
9. Basic layout — `gr.Blocks` with image upload + 3 sliders + submit button
10. `segment()` callback — httpx POST to model, parse masks `.npy` response
11. Overlay rendering — colored labels alpha-composited on original image
12. Cell count summary — `gr.Textbox` with "N cells detected" + mean/median/std
13. Per-cell stats table — `gr.Dataframe` with Cell ID, area (px), area (%)
14. Histogram — matplotlib cell size distribution via `gr.Plot`
15. Download buttons — overlay PNG + raw masks `.npy` via `gr.File`

**Phase 1D — Integration & Verification** (*depends on 1B + 1C*)
16. End-to-end test with synthetic image (256×256 random)
17. End-to-end test with real image from `media/test/001_img.png`
18. Error handling tests — no image uploaded, model container down, oversized input
19. Create `README.md` — overview, quick start, usage, architecture link
20. Update `CHANGELOG.md` with all Phase 1 changes

**Relevant files:**
- `docker-compose.yml` — rewrite to match design doc
- `Model_container/cellpose_api/app.py` — add validation, `/parameters`, `USE_GPU`
- `Model_container/Dockerfile` — add `curl`, possibly update model to cpsam
- `App_container/app.py` — new file, ~100 lines from design doc
- `App_container/requirements.txt` — gradio, httpx, numpy, Pillow, matplotlib
- `App_container/Dockerfile` — new file, Python 3.11-slim
- `README.md` — new file
- `CHANGELOG.md` — update

**Agents:** `@devops` → 1A, 1D(16-18) | `@model-dev` →  1B| `@gradio-dev` → 1C | `@docs` → 1D(19-20)

---

### Phase 2: Enhanced Analysis
*Goal: Batch processing, model selection, improved UX.*

**Phase 2A — Batch Processing**
21. Multi-file upload (`gr.File(file_count="multiple")`)
22. Batch segmentation loop with `gr.Progress` bar
23. ZIP download for all overlays + masks
24. Batch summary statistics table

**Phase 2B — Model Selection**
25. Model dropdown querying `GET /parameters`
26. Dynamic slider generation from parameter schema response
27. Multi-model support (one container per model, same API contract)

**Phase 2C — UX Improvements**
28. Adjustable overlay opacity slider
29. CSV export for statistics
30. Original vs overlay comparison slider
31. Better loading/progress indicators

---

### Phase 3: Persistence & Annotation
*Goal: CVAT integration for annotation editing, database for history.*

32. Add CVAT service to docker-compose
33. CVAT serverless function calling Model Container (~50 lines)
34. PostgreSQL for project/image/result history
35. Volume mounts for image storage
36. Project management UI (list previous sessions)

---

### Phase 4: Production Readiness
*Goal: Multi-user auth, security hardening, scaling.*

37. User authentication + project-level permissions
38. API key auth for Model Container
39. TLS via Nginx reverse proxy
40. GDPR compliance documentation
41. Model container replicas + async task queue
42. 3D segmentation (Z-stack support)

---

### Current State Assessment

| Component | Status |
|-----------|--------|
| Model Container (app.py) | Working, needs hardening (Phase 1B) |
| App Container | **Not built** — directory doesn't exist |
| docker-compose.yml | **Broken** — wrong build context, wrong service name, model exposed to host |
| Test data (68 image pairs) | Available in `media/test/` |
| README | Missing |
| Design doc | Complete and thorough |
| Agent instructions | 4 agents + design compliance instruction in place |

**Start here:** Phase 1A (fix infrastructure) → then 1B and 1C in parallel → 1D to verify.
