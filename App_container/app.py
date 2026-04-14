import io
import os
import tempfile
import numpy as np
import httpx
import gradio as gr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image

MODEL_URL = os.getenv("MODEL_URL", "http://model:8000/segment")

# Tracks temp files from the previous call so they can be deleted at the start
# of the next call (after Gradio has already served them to the browser).
_pending_cleanup: list[str] = []


def segment(image, diameter, flow_threshold, cellprob_threshold):
    global _pending_cleanup
    # Clean up temp files written by the previous invocation.
    for _p in _pending_cleanup:
        try:
            os.unlink(_p)
        except OSError:
            pass
    _pending_cleanup.clear()
    """Upload image to Model Container, return overlay + stats."""
    if image is None:
        raise gr.Error("Please upload an image first.")

    # Encode image as PNG bytes
    buf = io.BytesIO()
    Image.fromarray(image).save(buf, format="PNG")
    buf.seek(0)

    # Call Model Container
    # Build form data; omit diameter entirely when 0 so FastAPI keeps it as None (auto-detect)
    form_data = {
        "flow_threshold": flow_threshold,
        "cellprob_threshold": cellprob_threshold,
    }
    if diameter > 0:
        form_data["diameter"] = diameter

    # cpsam uses a ViT-H backbone; on CPU-only nodes inference can take
    # 5-15 minutes for real microscopy images.  Use per-phase timeouts so
    # the long wait is only on the read phase, not on connect/write.
    _timeout = httpx.Timeout(connect=10.0, write=60.0, read=900.0, pool=10.0)
    try:
        resp = httpx.post(
            MODEL_URL,
            files={"image": ("image.png", buf.getvalue(), "image/png")},
            data=form_data,
            timeout=_timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise gr.Error(f"Segmentation failed (HTTP {e.response.status_code}): {e.response.text}")
    except httpx.TimeoutException:
        raise gr.Error("Segmentation timed out after 15 minutes. The model is running on CPU — try enabling GPU or switching to the cyto3 model.")
    except httpx.RequestError as e:
        raise gr.Error(f"Cannot reach model container ({type(e).__name__}). It may still be starting — please wait 30 seconds and retry.")

    # Parse masks
    masks = np.load(io.BytesIO(resp.content))
    labels = np.unique(masks)
    labels = labels[labels != 0]  # exclude background
    cell_count = len(labels)

    # --- Colored overlay ---
    # Ensure image is RGB (Gradio may pass grayscale 2-D or RGBA 4-channel)
    rgb = image
    if rgb.ndim == 2:
        rgb = np.stack([rgb, rgb, rgb], axis=-1)
    elif rgb.shape[2] == 4:
        rgb = rgb[:, :, :3]
    overlay = rgb.copy().astype(np.float32) / 255.0
    cmap = matplotlib.colormaps["tab20"]
    for i, label_id in enumerate(labels):
        color = np.array(cmap(i % 20)[:3])
        mask = masks == label_id
        overlay[mask] = overlay[mask] * 0.45 + color * 0.55
    overlay_uint8 = (overlay * 255).astype(np.uint8)

    # --- Per-cell stats ---
    total_pixels = masks.shape[0] * masks.shape[1]
    stats_rows = []
    areas = []
    for label_id in labels:
        area_px = int(np.sum(masks == label_id))
        areas.append(area_px)
        stats_rows.append({
            "Cell ID": int(label_id),
            "Area (px)": area_px,
            "Area (%)": round(area_px / total_pixels * 100, 3),
        })

    # --- Summary ---
    areas_arr = np.array(areas) if areas else np.array([0])
    summary = (
        f"{cell_count} cells detected\n"
        f"Mean area: {areas_arr.mean():.0f} px | "
        f"Median: {np.median(areas_arr):.0f} px | "
        f"Std: {areas_arr.std():.0f} px\n"
        f"Smallest: {areas_arr.min()} px | "
        f"Largest: {areas_arr.max()} px"
    )

    # --- Histogram ---
    fig, ax = plt.subplots(figsize=(6, 3))
    if cell_count > 0:
        ax.hist(areas, bins=min(30, cell_count), color="#2E7D32", edgecolor="white")
    ax.set_xlabel("Cell area (pixels)")
    ax.set_ylabel("Count")
    ax.set_title("Cell size distribution")
    fig.tight_layout()

    # --- Downloadable files ---
    # Use delete=False so the file persists while Gradio serves it to the browser.
    # Return overlay_path (a stable on-disk file) for gr.Image instead of a raw
    # numpy array — this avoids Gradio writing its own temp file that can be
    # swept by its cleanup cycle before the browser finishes fetching it
    # (which caused FileNotFoundError: /tmp/gradio/<hash>/... in logs).
    overlay_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    overlay_path = overlay_tmp.name
    overlay_tmp.close()
    Image.fromarray(overlay_uint8).save(overlay_path)

    masks_tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False)
    masks_path = masks_tmp.name
    masks_tmp.close()
    np.save(masks_path, masks)

    # Track for cleanup on the next call.
    _pending_cleanup.extend([overlay_path, masks_path])

    return overlay_path, summary, stats_rows, fig, overlay_path, masks_path


# --- Gradio UI ---
with gr.Blocks(title="Cell Segmentation - Cellpose") as demo:
    gr.Markdown("# Cell Segmentation (Cellpose cyto3)")
    gr.Markdown("Upload a microscopy image, adjust parameters, and get segmentation results.")

    with gr.Row():
        with gr.Column(scale=1):
            img_input = gr.Image(type="numpy", label="Upload image")
            diameter = gr.Slider(0, 200, value=30, step=1, label="Diameter (0 = auto)")
            flow_thresh = gr.Slider(0, 1, value=0.4, step=0.05, label="Flow threshold")
            cellprob_thresh = gr.Slider(-6, 6, value=0.0, step=0.5, label="Cell probability threshold")
            submit_btn = gr.Button("Segment", variant="primary")

        with gr.Column(scale=2):
            overlay_output = gr.Image(label="Segmentation overlay")
            summary_box = gr.Textbox(label="Summary", lines=3)

    with gr.Row():
        stats_table = gr.Dataframe(label="Per-cell statistics", headers=["Cell ID", "Area (px)", "Area (%)"])
        histogram = gr.Plot(label="Size distribution")

    with gr.Row():
        overlay_file = gr.File(label="Download overlay PNG")
        masks_file = gr.File(label="Download masks.npy")

    submit_btn.click(
        fn=segment,
        inputs=[img_input, diameter, flow_thresh, cellprob_thresh],
        outputs=[overlay_output, summary_box, stats_table, histogram, overlay_file, masks_file],
    )

demo.queue()
demo.launch(server_name="0.0.0.0", server_port=8001)
