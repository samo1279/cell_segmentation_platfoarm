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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

USE_GPU = os.environ.get("USE_GPU", "false").lower() == "true"

# Model is loaded during startup so uvicorn binds port 8000 immediately.
# /health returns 503 until loading completes; the readiness probe waits for 200.
MODEL = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load the model in a thread-pool executor so the event loop stays
    unblocked during the 30-90 s load.  /health returns 503 until done,
    allowing readiness probes to report not-ready without killing the pod."""
    global MODEL
    logger.info(f"Loading Cellpose model (gpu={USE_GPU})...")
    loop = asyncio.get_event_loop()
    MODEL = await loop.run_in_executor(
        None,
        lambda: models.CellposeModel(gpu=USE_GPU, pretrained_model="cpsam"),
    )
    logger.info("Model loaded successfully")
    yield
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
    if MODEL is None:
        return JSONResponse(status_code=503, content={"ok": False, "status": "loading"})
    return {"ok": True, "model": "cpsam", "gpu": USE_GPU}


@app.get("/parameters")
def parameters():
    return {
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
    diameter: float | None = Form(default=None),
    flow_threshold: float = Form(default=0.4),
    cellprob_threshold: float = Form(default=0.0),
):
    if MODEL is None:
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

    # --- Segmentation ---
    # Run MODEL.eval() in a thread-pool executor so the async event loop stays
    # free to handle /health probes while inference runs (30–120 s on large images).
    # Without this, the event loop is blocked and /health times out → liveness kills the pod.
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: MODEL.eval(
                img,
                diameter=diameter,
                flow_threshold=flow_threshold,
                cellprob_threshold=cellprob_threshold,
            ),
        )
    except Exception as e:
        logger.error(f"Segmentation error: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Segmentation failed: {str(e)}")

    masks = result[0]
    logger.info(f"Segmentation complete. Found {len(np.unique(masks)) - 1} cells")

    buf = io.BytesIO()
    np.save(buf, masks.astype(np.int32))
    buf.seek(0)

    return Response(
        content=buf.getvalue(),
        media_type="application/octet-stream",
        headers={"Content-Disposition": "attachment; filename=masks.npy"},
    )
   