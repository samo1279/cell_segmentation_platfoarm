import asyncio
import io
import os
import numpy as np
import imageio.v3 as iio
from contextlib import asynccontextmanager
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import Response, JSONResponse
from cellpose import models
import logging
import psycopg2
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

USE_GPU = os.environ.get("USE_GPU", "false").lower() == "true"
DATABASE_URL = os.environ.get("DATABASE_URL")

_db_conn = None


def _get_db_conn():
    """Return a live psycopg2 connection, or None if DATABASE_URL is unset.

    The connection is kept as a module-level singleton and re-established
    automatically if it drops (e.g. PostgreSQL restart).
    autocommit=True avoids open-transaction side-effects on a long-lived
    connection.
    """
    global _db_conn
    if not DATABASE_URL:
        return None
    try:
        if _db_conn is None or _db_conn.closed != 0:
            _db_conn = psycopg2.connect(DATABASE_URL)
            _db_conn.autocommit = True
        return _db_conn
    except Exception as exc:
        logger.warning("DB connection unavailable: %s", exc)
        _db_conn = None
        return None


# Limit to one concurrent MODEL.eval() call.
# Running multiple heavy inference jobs in parallel on the same CPU cores
# causes memory-bandwidth thrashing and makes every request slower.
# Requests queue here instead of competing; health probes are unaffected
# because /health is a lightweight async function on the event loop.
_INFER_SEM = asyncio.Semaphore(1)

# Model is loaded during startup so uvicorn binds port 8000 immediately.
# /health returns 503 until loading completes; the readiness probe waits for 200.
MODEL = None  # kept for backward compatibility
MODELS: dict[str, object] = {"cyto3": None, "cpsam": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load both models in parallel using thread-pool executors so the event
    loop stays unblocked during the 30-90 s load.  /health returns 503 until
    both are ready, allowing readiness probes to report not-ready without
    killing the pod."""
    global MODEL, MODELS
    logger.info(f"Loading Cellpose models (gpu={USE_GPU})...")
    loop = asyncio.get_event_loop()

    def _load(name: str):
        m = models.CellposeModel(gpu=USE_GPU, pretrained_model=name)
        logger.info(f"Model '{name}' loaded successfully")
        return m

    cyto3_model, cpsam_model = await asyncio.gather(
        loop.run_in_executor(None, lambda: _load("cyto3")),
        loop.run_in_executor(None, lambda: _load("cpsam")),
    )
    MODELS["cyto3"] = cyto3_model
    MODELS["cpsam"] = cpsam_model
    MODEL = MODELS["cyto3"]  # backward-compat alias

    # --- DB setup (gracefully skipped when DATABASE_URL is unset) ---
    conn = _get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS projects (
                        id             SERIAL PRIMARY KEY,
                        project_name   TEXT,
                        image_filename TEXT,
                        timestamp      TIMESTAMPTZ DEFAULT NOW(),
                        model_used     TEXT,
                        cell_count     INT,
                        mask_path      TEXT
                    )
                    """
                )
            logger.info("DB table 'projects' ready")
        except Exception as exc:
            logger.warning("DB table setup failed: %s", exc)
    else:
        logger.info("DATABASE_URL not set — running without persistence")

    yield
    MODELS["cyto3"] = None
    MODELS["cpsam"] = None
    MODEL = None


app = FastAPI(title="Cellpose Segmentation API", lifespan=lifespan)

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB
MAX_DIMENSION = 8192
ALLOWED_CONTENT_TYPES = {"image/png", "image/tiff", "image/tif", "image/jpeg", "image/jpg"}
ALLOWED_EXTENSIONS = {".png", ".tiff", ".tif", ".jpeg", ".jpg"}


@app.get("/health")
async def health():
    # async def runs directly on the event loop — never queued in the thread pool.
    # This guarantees /health responds instantly even while MODEL.eval() is running.
    loaded = {name: (m is not None) for name, m in MODELS.items()}
    if not all(loaded.values()):
        return JSONResponse(status_code=503, content={"ok": False, "status": "loading", "models": loaded})
    return {"ok": True, "models": loaded, "gpu": USE_GPU}


@app.get("/parameters")
def parameters():
    return {
        "model_type": {
            "type": "string",
            "default": "cyto3",
            "options": ["cyto3", "cpsam"],
            "description": "cyto3: U-Net backbone, fast (5-30s on GPU). cpsam: ViT-H transformer, slow (2-20 min on GPU), best accuracy.",
        },
        "diameter": {
            "type": "float",
            "default": None,
            "min": 0.0,
            "max": 500.0,
            "description": "Expected cell diameter in pixels. Use None (0) for auto-detection.",
        },
        "flow_threshold": {
            "type": "float",
            "default": 0.4,
            "min": 0.0,
            "max": 1.0,
            "description": "Maximum allowed error of the flow fields. Higher values allow more cells.",
        },
        "cellprob_threshold": {
            "type": "float",
            "default": 0.0,
            "min": -6.0,
            "max": 6.0,
            "description": "Threshold on cell probability output. Lower values include more pixels as cells.",
        },
    }


@app.post("/segment")
async def segment(
    image: UploadFile = File(...),
    model_type: str = Form(default="cyto3"),
    diameter: float | None = Form(default=None),
    flow_threshold: float = Form(default=0.4),
    cellprob_threshold: float = Form(default=0.0),
):
    if model_type not in MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid model_type '{model_type}'. Must be one of: {list(MODELS.keys())}.",
        )
    selected_model = MODELS[model_type]
    if selected_model is None:
        raise HTTPException(status_code=503, detail="Model is still loading, please retry in a few seconds.")

    # --- Input validation ---
    ext = os.path.splitext(image.filename or "")[1].lower()
    content_type = (image.content_type or "").lower()
    if ext not in ALLOWED_EXTENSIONS and content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file format '{ext}'. Allowed: PNG, TIFF, JPEG.",
        )

    data = await image.read()

    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=422,
            detail=f"File too large ({len(data) // (1024*1024)} MB). Maximum allowed is 50 MB.",
        )

    try:
        img = iio.imread(io.BytesIO(data))
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Could not decode image: {str(e)}")

    if img.ndim >= 2:
        h, w = img.shape[:2]
        if h > MAX_DIMENSION or w > MAX_DIMENSION:
            raise HTTPException(
                status_code=422,
                detail=f"Image resolution {w}x{h} exceeds maximum {MAX_DIMENSION}x{MAX_DIMENSION}.",
            )

    logger.info(f"Processing image: {image.filename}, shape={img.shape}")
    logger.info(f"Using model: {model_type}")

    # --- Segmentation ---
    # Run model.eval() in a thread-pool executor so the async event loop stays
    # free to handle /health probes while inference runs (30–120 s on large images).
    # Without this, the event loop is blocked and /health times out → liveness kills the pod.
    # Explicitly pass channel_axis to avoid the `channels` deprecation warning in Cellpose v4.
    channel_axis = None if img.ndim == 2 else 2
    try:
        loop = asyncio.get_event_loop()
        async with _INFER_SEM:
            result = await loop.run_in_executor(
                None,
                lambda: selected_model.eval(
                    img,
                    diameter=diameter,
                    flow_threshold=flow_threshold,
                    cellprob_threshold=cellprob_threshold,
                    channel_axis=channel_axis,
                ),
            )
    except Exception as e:
        logger.error(f"Segmentation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Segmentation failed: {str(e)}")

    masks = result[0]
    cell_count = int(len(np.unique(masks)) - 1)
    logger.info(f"Segmentation complete. Found {cell_count} cells")

    # --- Persist job metadata (best-effort; never fails the request) ---
    conn = _get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO projects (image_filename, model_used, cell_count)"
                    " VALUES (%s, %s, %s)",
                    (image.filename, model_type, cell_count),
                )
        except Exception as exc:
            logger.warning("DB insert failed: %s", exc)

    buf = io.BytesIO()
    np.save(buf, masks.astype(np.int32))
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": "attachment; filename=masks.npy",
            # Echoes back which model actually ran so the client can confirm routing.
            "X-Model-Used": model_type,
        },
    )


@app.get("/projects")
def get_projects():
    """Return the last 100 segmentation records ordered by most-recent first.

    Returns 503 when the container is running without a database (DATABASE_URL
    not set), so callers can detect the degraded-mode case cleanly.
    """
    conn = _get_db_conn()
    if conn is None:
        return JSONResponse(
            status_code=503,
            content={"detail": "Database not configured. Set DATABASE_URL to enable persistence."},
        )
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, project_name, image_filename,
                       timestamp AT TIME ZONE 'UTC' AS timestamp,
                       model_used, cell_count, mask_path
                FROM projects
                ORDER BY timestamp DESC
                LIMIT 100
                """
            )
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        # Convert datetime objects to ISO 8601 strings for JSON serialisation
        for row in rows:
            if row.get("timestamp") is not None:
                row["timestamp"] = row["timestamp"].isoformat()
        return rows
    except Exception as exc:
        logger.error("DB query failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Database query failed: {str(exc)}")
