# Cell Segmentation Platform — POC v1

Browser-based cell segmentation tool for on-premise research labs. Upload microscopy images, tune Cellpose parameters, and receive a colored segmentation overlay, cell count, per-cell statistics, and downloadable results — without sending data outside your network.

> **Thesis context**: This POC demonstrates a GDPR-compliant on-premise alternative to cloud-hosted tools (e.g., the HuggingFace Cellpose Space), where image data never leaves the lab infrastructure.

---

## Architecture

Two Docker containers, one internal network:

```
Browser
  └─► App Container (Gradio, port 8001)
        └─► Model Container (FastAPI + Cellpose, internal only)
```

- **App Container** (`App_container/`): Single `app.py` — Gradio Blocks UI. Handles file upload, parameter sliders, overlay rendering, statistics, and downloads.
- **Model Container** (`Model_container/`): FastAPI wrapping Cellpose `cyto3`. Exposes `GET /health`, `GET /parameters`, `POST /segment`. Never port-mapped to the host.

See [improved_system_design.md](improved_system_design.md) for full architecture diagrams, API contract, and design decisions.

---

## Quick Start

**Prerequisites:** Docker Desktop (or Docker Engine + Compose plugin), 4 GB RAM.

```bash
# 1. Clone
git clone <repo-url>
cd POC_version1

# 2. Build and start (first build downloads Cellpose weights — ~500 MB, takes a few minutes)
docker compose up --build

# 3. Open the app
open http://localhost:8001
```

To stop:

```bash
docker compose down
```

---

## Usage

1. **Upload image** — drag and drop a PNG, TIFF, or JPEG microscopy image (max 50 MB, max 8192×8192 px)
2. **Adjust parameters** using the sliders:
   - **Diameter** — expected cell diameter in pixels (0 = auto-detect)
   - **Flow threshold** — max flow field error; higher = more cells detected (default 0.4)
   - **Cell probability threshold** — lower = more pixels counted as cells (default 0.0)
3. **Click Segment** — results appear in 5–60 seconds depending on image size
4. **View results**:
   - Colored overlay of detected cells on the original image
   - Summary: cell count, mean/median/std area, smallest/largest cell
   - Per-cell statistics table: Cell ID, area in pixels, area as % of image
   - Cell size distribution histogram
5. **Download** — overlay PNG or raw `masks.npy` (NumPy integer array, one label per cell)

---

## API Reference

The Model Container exposes three endpoints on its internal Docker network (`http://model:8000`):

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | Returns model name, GPU status, and `ok: true` |
| `GET` | `/parameters` | Returns JSON schema for all tunable parameters |
| `POST` | `/segment` | Accepts image + params, returns `masks.npy` binary |

**`POST /segment` request** (multipart/form-data):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `image` | file | required | PNG, TIFF, or JPEG, max 50 MB |
| `diameter` | float | auto | Expected cell diameter in pixels |
| `flow_threshold` | float | 0.4 | Flow field error threshold |
| `cellprob_threshold` | float | 0.0 | Cell probability threshold |

**Responses:**
- `200` — `application/octet-stream` — NumPy `.npy` mask array (int32, shape: H×W)
- `422` — `{"detail": "..."}` — validation error (bad format, oversized file)
- `500` — `{"detail": "..."}` — segmentation error

See [improved_system_design.md](improved_system_design.md) for the full API contract.

---

## Configuration

| Variable | Container | Default | Description |
|----------|-----------|---------|-------------|
| `USE_GPU` | model | `false` | Set to `true` to use CUDA GPU |
| `GRADIO_SERVER_NAME` | app | `0.0.0.0` | Gradio bind address |

To enable GPU, edit `docker-compose.yml`:

```yaml
  model:
    environment:
      - USE_GPU=true
```

---

## Project Structure

```
POC_version1/
├── App_container/
│   ├── app.py              # Gradio UI — upload, sliders, overlay, stats, downloads
│   ├── requirements.txt    # gradio, httpx, numpy, Pillow, matplotlib
│   └── Dockerfile          # python:3.11-slim, port 8001
├── Model_container/
│   ├── cellpose_api/
│   │   └── app.py          # FastAPI — /health, /parameters, /segment
│   ├── requirements.txt    # fastapi, uvicorn, cellpose, numpy, imageio, tifffile
│   └── Dockerfile          # python:3.11-slim, port 8000 (internal only)
├── media/
│   └── test/               # 68 microscopy image/mask pairs for validation
├── .github/
│   ├── agents/             # Custom AI agents: gradio-dev, model-dev, devops, docs
│   ├── instructions/       # system-design.instructions.md — enforces architecture compliance
│   └── plan.md             # Phased implementation plan (4 phases)
├── docker-compose.yml      # Two-service stack: app (port 8001) + model (internal)
├── improved_system_design.md  # Full architecture spec with Mermaid diagrams
└── CHANGELOG.md            # All changes, Keep a Changelog format
```

---

## Development

### Modify the Gradio UI
Edit `App_container/app.py`. Rebuild only the app container:

```bash
docker compose up --build app
```

### Modify the Model Container
Edit `Model_container/cellpose_api/app.py`. Rebuild the model container:

```bash
docker compose up --build model
```

**Do not change the API contract** (`/health`, `/parameters`, `/segment`) without updating `improved_system_design.md` first — see `.github/instructions/system-design.instructions.md`.

### Run integration tests

```bash
# Health check from inside the Docker network
docker compose exec app curl -s http://model:8000/health

# End-to-end segmentation test (synthetic image)
docker compose exec app python -c "
import httpx, io, numpy as np
from PIL import Image
img = np.random.randint(0, 255, (256, 256, 3), dtype=np.uint8)
buf = io.BytesIO()
Image.fromarray(img).save(buf, format='PNG')
resp = httpx.post('http://model:8000/segment',
    files={'image': ('test.png', buf.getvalue(), 'image/png')},
    data={'diameter': '30', 'flow_threshold': '0.4', 'cellprob_threshold': '0.0'},
    timeout=120)
masks = np.load(io.BytesIO(resp.content))
print(f'Status: {resp.status_code} | Masks: {masks.shape} | Cells: {len(np.unique(masks)) - 1}')
"
```

### AI Agents
Four specialized agents are available in `.github/agents/` for use with GitHub Copilot:

| Agent | Trigger | Scope |
|-------|---------|-------|
| `@gradio-dev` | Gradio UI changes | `App_container/app.py` |
| `@model-dev` | API / Cellpose changes | `Model_container/` |
| `@devops` | Docker, networking, testing | `docker-compose.yml`, Dockerfiles |
| `@docs` | Documentation updates | `README.md`, `CHANGELOG.md`, `improved_system_design.md` |

---

## Roadmap

| Phase | Goal | Status |
|-------|------|--------|
| **Phase 1 — Foundation** | Working 2-container MVP: single image upload, segment, overlay, stats, download | ✅ Complete |
| **Phase 2 — Enhanced Analysis** | Batch upload, model selection dropdown, CSV export, opacity slider | Planned |
| **Phase 3 — Annotation** | CVAT integration, PostgreSQL persistence, project management | Planned |
| **Phase 4 — Production** | Auth, TLS, multi-user, scaling, 3D segmentation | Planned |

See [.github/plan.md](.github/plan.md) for step-by-step task breakdown.

---

## License

To be determined.
