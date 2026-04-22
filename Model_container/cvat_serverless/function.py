"""CVAT nuclio serverless function — Cellpose cell segmentation.

Receives a base64-encoded image from CVAT's nuclio runtime, forwards it to the
Model Container at http://model:8000/segment, and converts the returned
masks.npy into CVAT polygon annotation objects.

Environment variables (all optional — defaults match the Docker Compose network):
  MODEL_URL                 Full URL of POST /segment  (default: http://model:8000/segment)
  DEFAULT_MODEL_TYPE        cyto3 | cpsam              (default: cyto3)
  DEFAULT_DIAMETER          Cell diameter in px; 0 = auto-detect (default: 0)
  DEFAULT_FLOW_THRESHOLD    0.0–1.0                    (default: 0.4)
  DEFAULT_CELLPROB_THRESHOLD  -6.0–6.0                 (default: 0.0)
"""

import base64
import io
import json
import os

import numpy as np
import requests
from skimage import measure

MODEL_URL = os.environ.get("MODEL_URL", "http://model:8000/segment")
DEFAULT_MODEL_TYPE = os.environ.get("DEFAULT_MODEL_TYPE", "cyto3")
DEFAULT_DIAMETER = float(os.environ.get("DEFAULT_DIAMETER", "0"))
DEFAULT_FLOW_THRESHOLD = float(os.environ.get("DEFAULT_FLOW_THRESHOLD", "0.4"))
DEFAULT_CELLPROB_THRESHOLD = float(os.environ.get("DEFAULT_CELLPROB_THRESHOLD", "0.0"))


def _masks_to_cvat_polygons(masks: np.ndarray) -> list:
    """Convert an integer label mask to a list of CVAT polygon annotation dicts.

    Each unique non-zero label becomes one polygon using the longest contour
    of that cell's binary mask.  Points are returned as a flat (x, y, x, y, …)
    list as expected by the CVAT annotation format.
    """
    results = []
    for label in np.unique(masks):
        if label == 0:
            continue  # skip background
        binary = (masks == label).astype(np.uint8)
        contours = measure.find_contours(binary, 0.5)
        if not contours:
            continue
        # Use the longest contour (outer boundary)
        contour = max(contours, key=len)
        # find_contours returns (row, col) → convert to flat (x, y, …) list
        points = contour[:, ::-1].flatten().tolist()
        if len(points) < 6:  # need at least 3 vertices (3 × (x, y))
            continue
        results.append(
            {
                "confidence": 1.0,
                "label": "cell",
                "points": points,
                "type": "polygon",
            }
        )
    return results


def init_context(context):
    """Called once by the nuclio runtime before the first request."""
    context.logger.info(
        "Cellpose CVAT serverless function initialised. MODEL_URL=%s", MODEL_URL
    )


def handler(context, event):
    """Entry point called by the nuclio runtime for each auto-annotation request.

    Expected event body (JSON):
      {
        "image": "<base64-encoded PNG/JPEG/TIFF bytes>",
        "threshold": <optional float, mapped to flow_threshold>
      }

    Returns a JSON array of CVAT polygon annotation objects.
    """
    try:
        body = event.body
        if isinstance(body, (bytes, bytearray)):
            body = json.loads(body)

        image_bytes = base64.b64decode(body["image"])
        flow_threshold = float(body.get("threshold", DEFAULT_FLOW_THRESHOLD))

        response = requests.post(
            MODEL_URL,
            files={"image": ("image.png", io.BytesIO(image_bytes), "image/png")},
            data={
                "model_type": DEFAULT_MODEL_TYPE,
                "diameter": DEFAULT_DIAMETER,
                "flow_threshold": flow_threshold,
                "cellprob_threshold": DEFAULT_CELLPROB_THRESHOLD,
            },
            timeout=300,
        )
        response.raise_for_status()

        masks = np.load(io.BytesIO(response.content))
        annotations = _masks_to_cvat_polygons(masks)

        return context.Response(
            body=json.dumps(annotations),
            headers={},
            content_type="application/json",
            status_code=200,
        )

    except Exception as exc:
        context.logger.error("Handler error: %s", exc)
        return context.Response(
            body=json.dumps({"error": str(exc)}),
            headers={},
            content_type="application/json",
            status_code=500,
        )
