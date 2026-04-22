import asyncio
import hashlib
import io
import os
import numpy as np
import imageio.v3 as iio
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, UploadFile, File, Form, HTTPException
from fastapi.responses import Response, JSONResponse
from cellpose import models
from celery.result import AsyncResult
import logging
import psycopg2
from dotenv import load_dotenv
from tasks import run_segmentation, celery_app as _celery_app

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

USE_GPU = os.environ.get("USE_GPU", "false").lower() == "true"
DATABASE_URL = os.environ.get("DATABASE_URL")
API_KEY: str | None = os.environ.get("API_KEY")

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


async def verify_api_key(x_api_key: str = Header(default=None)) -> None:
    """Validate X-API-Key header against the API_KEY env var.

    When API_KEY is unset the check is skipped so the container works in
    local/dev mode without authentication.
    """
    if API_KEY is not None and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


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
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id          SERIAL PRIMARY KEY,
                        action      TEXT        NOT NULL,
                        image_hash  TEXT        NOT NULL,
                        timestamp   TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                # GDPR: purge project records older than 30 days on every startup
                cur.execute(
                    "DELETE FROM projects WHERE timestamp < NOW() - INTERVAL '30 days'"
                )
            logger.info("DB tables ready; stale projects purged (>30 days)")
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


@app.post("/segment", dependencies=[Depends(verify_api_key)])
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

    logger.info("Enqueuing segmentation: file=%s model=%s", image.filename, model_type)

    # Compute SHA-256 of raw bytes for GDPR-safe audit logging (no filename stored)
    image_hash = hashlib.sha256(data).hexdigest()

    # Dispatch to the Celery worker; inference and 3-D z-stack detection run there
    task = run_segmentation.delay(data, model_type, diameter, flow_threshold, cellprob_threshold)

    # --- GDPR audit log (best-effort; never fails the request) ---
    conn = _get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO audit_log (action, image_hash) VALUES (%s, %s)",
                    ("segment", image_hash),
                )
        except Exception as exc:
            logger.warning("Audit log insert failed: %s", exc)

    return JSONResponse(status_code=202, content={"job_id": task.id})


@app.get("/segment/{job_id}", dependencies=[Depends(verify_api_key)])
async def get_segment_result(job_id: str):
    """Poll the result of an async segmentation job.

    Returns 202 while the job is pending/running; returns the masks.npy binary
    (``application/octet-stream``) once the task completes successfully.
    """
    result = AsyncResult(job_id, app=_celery_app)
    if result.state in ("PENDING", "STARTED", "RETRY"):
        return JSONResponse(
            status_code=202,
            content={"status": result.state.lower(), "job_id": job_id},
        )
    if result.state == "SUCCESS":
        mask_bytes: bytes = result.get()
        return Response(
            content=mask_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=masks.npy"},
        )
    # FAILURE or unknown terminal state
    error_detail = str(result.result) if result.result else "Unknown error"
    raise HTTPException(status_code=500, detail=f"Segmentation failed: {error_detail}")


@app.get("/projects", dependencies=[Depends(verify_api_key)])
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
