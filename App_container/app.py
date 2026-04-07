import io
import tempfile
import numpy as np
import httpx
import gradio as gr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image

MODEL_URL = "http://model:8000/segment"


def segment(image, diameter, flow_threshold, cellprob_threshold):
    """Upload image to Model Container, return overlay + stats."""
    if image is None:
        raise gr.Error("Please upload an image first.")

    # Encode image as PNG bytes
    buf = io.BytesIO()
    Image.fromarray(image).save(buf, format="PNG")
    buf.seek(0)

    # Call Model Container
    try:
        resp = httpx.post(
            MODEL_URL,
            files={"image": ("image.png", buf.getvalue(), "image/png")},
            data={
                "diameter": diameter if diameter > 0 else "",
                "flow_threshold": flow_threshold,
                "cellprob_threshold": cellprob_threshold,
            },
            timeout=120.0,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise gr.Error(f"Segmentation failed: {e.response.text}")
    except httpx.ConnectError:
        raise gr.Error("Model container unavailable. Is it running?")

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
    overlay_path = tempfile.NamedTemporaryFile(suffix=".png", delete=False).name
    Image.fromarray(overlay_uint8).save(overlay_path)
    masks_path = tempfile.NamedTemporaryFile(suffix=".npy", delete=False).name
    np.save(masks_path, masks)

    return overlay_uint8, summary, stats_rows, fig, overlay_path, masks_path


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

demo.launch(server_name="0.0.0.0", server_port=8001)
