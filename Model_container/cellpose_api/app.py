import io
import numpy as np
import imageio.v3 as iio
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from cellpose import models
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Cellpose Segmentation API")

logger.info("Loading Cellpose model...")
MODEL = models.CellposeModel(gpu=False, pretrained_model="cyto3") 
logger.info(f"Model Architecture:\n{MODEL.net}")
logger.info("Model loaded successfully")

@app.get("/health")
def health():
    return {"ok": True, "model": "cyto3", "gpu": False}

@app.post("/segment")
async def segment(
    image: UploadFile = File(...),
    diameter: float | None = Form(default=None),
    flow_threshold: float = Form(default=0.4),
    cellprob_threshold: float = Form(default=0.0),
):
    try:
        logger.info(f"Processing image: {image.filename}")
        
        data = await image.read()
        img = iio.imread(data)
        logger.info(f"Image shape: {img.shape}")

        result = MODEL.eval(
            img,
            diameter=diameter,
            flow_threshold=flow_threshold,
            cellprob_threshold=cellprob_threshold,
            channels=[0, 0],
        )
        
        masks = result[0]
        logger.info(f"Segmentation complete. Found {len(np.unique(masks))-1} cells")

        buf = io.BytesIO()
        np.save(buf, masks.astype(np.int32))
        buf.seek(0)
        
        return Response(
            content=buf.getvalue(), 
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename=masks.npy"}
        )
    except Exception as e:
        logger.error(f"Error processing image: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
   