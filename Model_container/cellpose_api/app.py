import asyncio
import io
import os
import re
import numpy as np
import imageio.v3 as iio
import tifffile
import bcrypt
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, Header, UploadFile, File, Form, HTTPException
from fastapi.responses import Response, JSONResponse
from pydantic import BaseModel
from cellpose import models
import logging
import psycopg2
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

USE_GPU = os.environ.get("USE_GPU", "false").lower() == "true"
DATABASE_URL = os.environ.get("DATABASE_URL")
API_KEY: str | None = os.environ.get("API_KEY") or None
DEFAULT_MODEL_TYPE = os.environ.get("DEFAULT_MODEL_TYPE", "cyto3")

# ---------------------------------------------------------------------------
# Pydantic request bodies for auth endpoints
# ---------------------------------------------------------------------------

class _AuthRegisterRequest(BaseModel):
    username: str
    password: str


class _AuthLoginRequest(BaseModel):
    username: str
    password: str

_db_conn = None
_MODEL_LOAD_LOCKS: dict[str, asyncio.Lock] = {}


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
    """Validate X-API-Key header. Skipped when API_KEY env var is unset (dev mode)."""
    if API_KEY is not None and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# Model is loaded during startup so uvicorn binds port 8000 immediately.
# /health returns 503 until loading completes; the readiness probe waits for 200.
MODEL = None  # kept for backward compatibility
MODELS: dict[str, object] = {"cyto3": None, "cpsam": None}


def _load_model_sync(name: str):
    """Load one Cellpose model in a worker thread."""
    logger.info("Loading Cellpose model '%s' (gpu=%s)...", name, USE_GPU)
    model = models.CellposeModel(gpu=USE_GPU, pretrained_model=name)
    logger.info("Model '%s' loaded successfully", name)
    return model


async def _ensure_model_loaded(name: str):
    """Return a loaded model, loading it lazily on first use."""
    global MODEL
    if name not in MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid model_type '{name}'. Must be one of: {list(MODELS.keys())}.",
        )
    if MODELS[name] is not None:
        return MODELS[name]

    lock = _MODEL_LOAD_LOCKS.setdefault(name, asyncio.Lock())
    async with lock:
        if MODELS[name] is None:
            loop = asyncio.get_running_loop()
            MODELS[name] = await loop.run_in_executor(None, lambda: _load_model_sync(name))
            if name == "cyto3":
                MODEL = MODELS[name]
        return MODELS[name]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load only the default model at startup; optional models lazy-load on demand.

    Loading every model during startup blocks Uvicorn from accepting health
    probes. cpsam is therefore loaded lazily when a request selects it.
    """
    global MODEL, MODELS
    if DEFAULT_MODEL_TYPE not in MODELS:
        raise RuntimeError(f"Invalid DEFAULT_MODEL_TYPE={DEFAULT_MODEL_TYPE!r}")

    MODELS[DEFAULT_MODEL_TYPE] = await asyncio.get_running_loop().run_in_executor(
        None, lambda: _load_model_sync(DEFAULT_MODEL_TYPE)
    )
    if DEFAULT_MODEL_TYPE == "cyto3":
        MODEL = MODELS["cyto3"]

    # --- DB setup (gracefully skipped when DATABASE_URL is unset) ---
    conn = _get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                # Users table — passwords are bcrypt-hashed, never stored in plain text
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        id            SERIAL PRIMARY KEY,
                        username      TEXT UNIQUE NOT NULL,
                        password_hash TEXT NOT NULL,
                        created_at    TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS projects (
                        id             SERIAL PRIMARY KEY,
                        project_name   TEXT,
                        image_filename TEXT,
                        timestamp      TIMESTAMPTZ DEFAULT NOW(),
                        model_used     TEXT,
                        cell_count     INT,
                        mask_path      TEXT,
                        username       TEXT
                    )
                    """
                )
                # Migrations for pre-existing tables
                cur.execute(
                    "ALTER TABLE projects ADD COLUMN IF NOT EXISTS username TEXT"
                )
            logger.info("DB tables ready")

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

# ---------------------------------------------------------------------------
# Auth endpoints  — no API-key guard; called by the App Container auth flow
# ---------------------------------------------------------------------------

_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,50}$")


@app.post("/auth/register")
def auth_register(req: _AuthRegisterRequest):
    """Create a new user account.

    - Username: 3–50 characters, letters / digits / underscore only.
    - Password: minimum 8 characters.
    - Returns 400 if the username is taken or the input is invalid.
    - Returns 503 when no database is configured.
    """
    if not _USERNAME_RE.match(req.username):
        raise HTTPException(
            status_code=400,
            detail="Username must be 3–50 characters (letters, digits, underscore only).",
        )
    if len(req.password) < 8:
        raise HTTPException(
            status_code=400,
            detail="Password must be at least 8 characters.",
        )

    conn = _get_db_conn()
    if conn is None:
        raise HTTPException(
            status_code=503,
            detail="Database not configured. Set DATABASE_URL to enable user accounts.",
        )

    pw_hash = bcrypt.hashpw(req.password.encode(), bcrypt.gensalt()).decode()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                (req.username, pw_hash),
            )
    except psycopg2.IntegrityError:
        raise HTTPException(status_code=400, detail="Username is already taken.")
    except Exception as exc:
        logger.error("Registration DB error: %s", exc)
        raise HTTPException(status_code=500, detail="Internal server error during registration.")

    logger.info("New user registered: '%s'", req.username)
    return {"message": f"Account '{req.username}' created. You can now log in."}


@app.post("/auth/login")
def auth_login(req: _AuthLoginRequest):
    """Verify credentials. Returns {valid: bool}."""
    conn = _get_db_conn()

    if conn is None:
        return {"valid": False}

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT password_hash FROM users WHERE username = %s",
                (req.username,),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.error("Login DB error: %s", exc)
        return {"valid": False}

    if not row:
        return {"valid": False}

    pw_hash = row[0]
    valid = bcrypt.checkpw(req.password.encode(), pw_hash.encode())
    return {"valid": bool(valid)}


@app.get("/health")
async def health():
    # Health is OK when the default model is ready. Optional models lazy-load later.
    loaded = {name: (m is not None) for name, m in MODELS.items()}
    ready = loaded.get(DEFAULT_MODEL_TYPE, False)
    body = {
        "ok": ready,
        "models": loaded,
        "default_model": DEFAULT_MODEL_TYPE,
        "gpu": USE_GPU,
    }
    if not ready:
        body["status"] = "loading"
        return JSONResponse(status_code=503, content=body)
    return body


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
    username: str | None = Form(default=None),
):
    if model_type not in MODELS:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid model_type '{model_type}'. Must be one of: {list(MODELS.keys())}.",
        )

    # --- Input validation ---
    ext = os.path.splitext(image.filename or "")[1].lower()
    content_type = (image.content_type or "").lower()
    if ext not in ALLOWED_EXTENSIONS or content_type not in ALLOWED_CONTENT_TYPES:
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
        logger.warning("Image decode failed (filename=%s): %s", os.path.basename(image.filename or ""), e)
        raise HTTPException(status_code=422, detail="Invalid image format or corrupted file.")

    if img.ndim >= 2:
        h, w = img.shape[:2]
        if h > MAX_DIMENSION or w > MAX_DIMENSION:
            raise HTTPException(
                status_code=422,
                detail=f"Image resolution {w}x{h} exceeds maximum {MAX_DIMENSION}x{MAX_DIMENSION}.",
            )

    logger.info("Processing image: %s, shape=%s", os.path.basename(image.filename or ""), img.shape)
    logger.info(f"Using model: {model_type}")
    selected_model = await _ensure_model_loaded(model_type)

    # --- 3-D z-stack detection ---
    # Multi-frame TIFFs are segmented slice-by-slice; results stacked as (Z, H, W).
    # Single-frame images fall through to standard 2-D inference.
    is_zstack = False
    n_frames = 1
    try:
        with tifffile.TiffFile(io.BytesIO(data)) as tif:
            n_frames = len(tif.pages)
            is_zstack = n_frames > 1
    except Exception:
        pass

    if is_zstack:
        logger.info(f"3-D z-stack detected: {n_frames} frames")

    # --- Segmentation ---
    channel_axis = None if img.ndim == 2 else 2

    def _run_2d(frame):
        ch_ax = None if frame.ndim == 2 else 2
        res = selected_model.eval(
            frame,
            diameter=diameter,
            flow_threshold=flow_threshold,
            cellprob_threshold=cellprob_threshold,
            channel_axis=ch_ax,
        )
        return res[0]  # masks only

    def _run_inference():
        if is_zstack:
            frames = tifffile.imread(io.BytesIO(data))  # reads all frames correctly
            slices = [_run_2d(frames[i]) for i in range(frames.shape[0])]
            return np.stack(slices, axis=0)  # (Z, H, W)
        else:
            return _run_2d(img)

    try:
        loop = asyncio.get_event_loop()
        async with _INFER_SEM:
            masks = await loop.run_in_executor(None, _run_inference)
    except Exception as e:
        logger.error(f"Segmentation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Segmentation failed: {str(e)}")

    cell_count = int(len(np.unique(masks)) - 1)
    logger.info(f"Segmentation complete. Found {cell_count} cells")

    # --- Persist job metadata (best-effort; never fails the request) ---
    conn = _get_db_conn()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO projects (image_filename, model_used, cell_count, username)"
                    " VALUES (%s, %s, %s, %s)",
                    (image.filename, model_type, cell_count, username),
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
            "X-Model-Used": model_type,
        },
    )


@app.get("/projects", dependencies=[Depends(verify_api_key)])
def get_projects(user: str | None = None):
    """Return the last 100 segmentation records ordered by most-recent first.

    Pass ``?user=alice`` to restrict results to a single user.

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
            if user:
                cur.execute(
                    """
                    SELECT id, project_name, image_filename,
                           timestamp AT TIME ZONE 'UTC' AS timestamp,
                           model_used, cell_count, mask_path, username
                    FROM projects
                    WHERE username = %s
                    ORDER BY timestamp DESC
                    LIMIT 100
                    """,
                    (user,),
                )
            else:
                cur.execute(
                    """
                    SELECT id, project_name, image_filename,
                           timestamp AT TIME ZONE 'UTC' AS timestamp,
                           model_used, cell_count, mask_path, username
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
