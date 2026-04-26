"""Celery task definitions for asynchronous Cellpose segmentation.

Each task runs inside a Celery worker process that loads the Cellpose model
lazily on first use.  The FastAPI process (app.py) only enqueues tasks and
polls results — it never runs model.eval() directly.

3-D z-stack support: if the uploaded TIFF has multiple frames (detected via
imageio.v3.improps), each slice is segmented independently and the resulting
per-slice masks are stacked into a (Z, H, W) array before serialisation.
"""

import io
import os
import logging

import numpy as np
import imageio.v3 as iio
import tifffile
from celery import Celery
from cellpose import models

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Celery application
# ---------------------------------------------------------------------------

CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://redis:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://redis:6379/0")

celery_app = Celery(
    "cellpose_tasks",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
)
celery_app.conf.update(
    task_serializer="pickle",
    result_serializer="pickle",
    accept_content=["pickle"],
    # Results are only needed for the polling window; expire after 1 hour.
    result_expires=3600,
)

# ---------------------------------------------------------------------------
# Per-worker model cache (lazy-loaded on first task execution)
# ---------------------------------------------------------------------------

USE_GPU = os.environ.get("USE_GPU", "false").lower() == "true"

_task_models: dict[str, object] = {}


def _get_task_model(model_type: str):
    """Load and cache a CellposeModel in the worker process.

    Models are loaded once per worker process and reused across tasks,
    avoiding the 30-90 s initialisation cost on every request.
    """
    if _task_models.get(model_type) is None:
        logger.info("Worker: loading Cellpose model '%s' (gpu=%s)", model_type, USE_GPU)
        _task_models[model_type] = models.CellposeModel(
            gpu=USE_GPU, pretrained_model=model_type
        )
        logger.info("Worker: model '%s' loaded", model_type)
    return _task_models[model_type]


# ---------------------------------------------------------------------------
# Segmentation task
# ---------------------------------------------------------------------------


@celery_app.task(bind=True, name="cellpose_tasks.run_segmentation")
def run_segmentation(
    self,
    image_bytes: bytes,
    model_type: str,
    diameter,
    flow_threshold: float,
    cellprob_threshold: float,
) -> bytes:
    """Run Cellpose segmentation synchronously inside a Celery worker.

    Supports:
    - 2-D images: (H, W) greyscale or (H, W, C) colour
    - 3-D z-stacks: multi-frame TIFF detected via imageio.v3.improps;
      each slice is segmented and results stacked into a (Z, H, W) array.

    Returns the mask array serialised as a NumPy .npy binary blob so the
    polling endpoint can return it as ``application/octet-stream``.
    """
    selected_model = _get_task_model(model_type)
    img = tifffile.imread(io.BytesIO(image_bytes))  # reads all frames correctly

    # ------------------------------------------------------------------
    # 3-D z-stack detection
    # ------------------------------------------------------------------
    is_zstack = False
    try:
        with tifffile.TiffFile(io.BytesIO(image_bytes)) as tif:
            is_zstack = len(tif.pages) > 1
    except Exception:
        pass  # fall back to 2-D path

    if is_zstack:
        logger.info("Worker: z-stack detected with %d frames", img.shape[0])
        slice_masks = []
        for z_idx in range(img.shape[0]):
            z_slice = img[z_idx]
            ch_ax = None if z_slice.ndim == 2 else 2
            result = selected_model.eval(
                z_slice,
                diameter=diameter,
                flow_threshold=flow_threshold,
                cellprob_threshold=cellprob_threshold,
                channel_axis=ch_ax,
            )
            slice_masks.append(result[0])
        masks = np.stack(slice_masks, axis=0)  # (Z, H, W)
        logger.info("Worker: 3-D segmentation complete, shape=%s", masks.shape)
    else:
        ch_ax = None if img.ndim == 2 else 2
        result = selected_model.eval(
            img,
            diameter=diameter,
            flow_threshold=flow_threshold,
            cellprob_threshold=cellprob_threshold,
            channel_axis=ch_ax,
        )
        masks = result[0]
        cell_count = int(len(np.unique(masks)) - 1)
        logger.info("Worker: 2-D segmentation complete, cells=%d", cell_count)

    buf = io.BytesIO()
    np.save(buf, masks.astype(np.int32))
    return buf.getvalue()
