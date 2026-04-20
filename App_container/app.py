import csv
import io
import os
import tempfile
import time
import zipfile
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
_pending_batch_cleanup: list[str] = []

_MODEL_TIMEOUT = httpx.Timeout(connect=10.0, write=60.0, read=900.0, pool=10.0)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _encode_png(image_np: np.ndarray) -> bytes:
    """Convert a numpy RGB array to PNG bytes."""
    buf = io.BytesIO()
    Image.fromarray(image_np).save(buf, format="PNG")
    return buf.getvalue()


def _call_model(image_bytes: bytes, diameter, flow_threshold, cellprob_threshold, model_type):
    """POST image to Model Container; return (masks_np, active_model_str)."""
    form_data: dict = {
        "flow_threshold": flow_threshold,
        "cellprob_threshold": cellprob_threshold,
        "model_type": model_type,
    }
    if diameter > 0:
        form_data["diameter"] = diameter
    resp = httpx.post(
        MODEL_URL,
        files={"image": ("image.png", image_bytes, "image/png")},
        data=form_data,
        timeout=_MODEL_TIMEOUT,
    )
    resp.raise_for_status()
    active_model = resp.headers.get("x-model-used", model_type)
    masks = np.load(io.BytesIO(resp.content))
    return masks, active_model


def _render_overlay(image_np: np.ndarray, masks: np.ndarray, opacity: float) -> np.ndarray:
    """Alpha-composite colored label mask over the original RGB image."""
    rgb = image_np
    if rgb.ndim == 2:
        rgb = np.stack([rgb, rgb, rgb], axis=-1)
    elif rgb.shape[2] == 4:
        rgb = rgb[:, :, :3]
    overlay = rgb.copy().astype(np.float32) / 255.0
    cmap = matplotlib.colormaps["tab20"]
    labels = np.unique(masks)
    labels = labels[labels != 0]
    for i, label_id in enumerate(labels):
        color = np.array(cmap(i % 20)[:3])
        mask_px = masks == label_id
        overlay[mask_px] = overlay[mask_px] * (1.0 - opacity) + color * opacity
    return (overlay * 255).astype(np.uint8)


def _compute_stats(masks: np.ndarray):
    """Return (areas list, stats_rows list-of-lists)."""
    labels = np.unique(masks)
    labels = labels[labels != 0]
    total_pixels = masks.shape[0] * masks.shape[1]
    areas, stats_rows = [], []
    for label_id in labels:
        area_px = int(np.sum(masks == label_id))
        areas.append(area_px)
        stats_rows.append([int(label_id), area_px, round(area_px / total_pixels * 100, 3)])
    return areas, stats_rows


# ---------------------------------------------------------------------------
# Single-image segmentation
# ---------------------------------------------------------------------------

def segment(image, diameter, flow_threshold, cellprob_threshold, model_type, opacity):
    """Upload image to Model Container, return overlay + stats."""
    global _pending_cleanup
    for _p in _pending_cleanup:
        try:
            os.unlink(_p)
        except OSError:
            pass
    _pending_cleanup.clear()

    if image is None:
        raise gr.Error("Please upload an image first.")

    image_bytes = _encode_png(image)

    try:
        masks, active_model = _call_model(
            image_bytes, diameter, flow_threshold, cellprob_threshold, model_type
        )
    except httpx.HTTPStatusError as e:
        raise gr.Error(
            f"Segmentation failed (HTTP {e.response.status_code}): {e.response.text}"
        )
    except httpx.TimeoutException:
        raise gr.Error(
            "Segmentation timed out after 15 minutes. The model is running on CPU — "
            "try enabling GPU or switching to the cyto3 model."
        )
    except httpx.RequestError as e:
        raise gr.Error(
            f"Cannot reach model container ({type(e).__name__}). "
            "It may still be starting — please wait 30 seconds and retry."
        )

    labels = np.unique(masks)
    labels = labels[labels != 0]
    cell_count = len(labels)

    overlay_uint8 = _render_overlay(image, masks, opacity)
    areas, stats_rows = _compute_stats(masks)

    # --- Summary ---
    areas_arr = np.array(areas) if areas else np.array([0])
    summary = (
        f"Model: {active_model} | {cell_count} cells detected\n"
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
    overlay_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    overlay_path = overlay_tmp.name
    overlay_tmp.close()
    Image.fromarray(overlay_uint8).save(overlay_path)

    masks_tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False)
    masks_path = masks_tmp.name
    masks_tmp.close()
    np.save(masks_path, masks)

    _pending_cleanup.extend([overlay_path, masks_path])

    return overlay_path, summary, stats_rows, fig, overlay_path, masks_path


def export_csv(stats_df):
    """Export the per-cell stats Dataframe as a CSV file."""
    if stats_df is None or len(stats_df) == 0:
        raise gr.Error("No statistics to export — run segmentation first.")
    tmp = tempfile.NamedTemporaryFile(
        suffix=".csv", delete=False, mode="w", newline=""
    )
    tmp.close()
    # gr.Dataframe passes a pandas DataFrame when used as input
    stats_df.to_csv(tmp.name, index=False)
    return tmp.name


# ---------------------------------------------------------------------------
# Batch segmentation
# ---------------------------------------------------------------------------

def batch_segment(
    files,
    diameter,
    flow_threshold,
    cellprob_threshold,
    model_type,
    opacity,
    progress=gr.Progress(),
):
    """Segment multiple images and return a summary table + ZIP of results."""
    global _pending_batch_cleanup
    for _p in _pending_batch_cleanup:
        try:
            os.unlink(_p)
        except OSError:
            pass
    _pending_batch_cleanup.clear()

    if not files:
        raise gr.Error("Please upload at least one image.")

    summary_rows = []
    overlay_entries: list[tuple[str, str]] = []  # (arcname, tmp_path)
    masks_entries: list[tuple[str, str]] = []

    total = len(files)
    for i, file_path in enumerate(files):
        # Gradio passes file paths as strings for multi-file uploads
        file_path = file_path if isinstance(file_path, str) else file_path.name
        filename = os.path.basename(file_path)
        stem = os.path.splitext(filename)[0]
        progress((i) / total, desc=f"Processing {filename} ({i + 1}/{total})…")
        t0 = time.time()

        try:
            img_pil = Image.open(file_path).convert("RGB")
            image_np = np.array(img_pil)
            image_bytes = _encode_png(image_np)
            masks, active_model = _call_model(
                image_bytes, diameter, flow_threshold, cellprob_threshold, model_type
            )
        except Exception as exc:
            summary_rows.append(
                [filename, model_type, "ERROR", "", round(time.time() - t0, 1)]
            )
            continue

        elapsed = round(time.time() - t0, 1)
        areas, _ = _compute_stats(masks)
        cell_count = len(areas)
        mean_area = round(float(np.mean(areas)), 1) if areas else 0.0

        overlay_uint8 = _render_overlay(image_np, masks, opacity)

        ov_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        ov_path = ov_tmp.name
        ov_tmp.close()
        Image.fromarray(overlay_uint8).save(ov_path)

        msk_tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False)
        msk_path = msk_tmp.name
        msk_tmp.close()
        np.save(msk_path, masks)

        overlay_entries.append((f"overlays/{stem}_overlay.png", ov_path))
        masks_entries.append((f"masks/{stem}_masks.npy", msk_path))
        _pending_batch_cleanup.extend([ov_path, msk_path])

        summary_rows.append([filename, active_model, cell_count, mean_area, elapsed])

    progress(1.0, desc="Building ZIP…")

    zip_tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    zip_path = zip_tmp.name
    zip_tmp.close()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for arcname, tmp_path in overlay_entries + masks_entries:
            zf.write(tmp_path, arcname=arcname)
    _pending_batch_cleanup.append(zip_path)

    return summary_rows, zip_path


# ---------------------------------------------------------------------------
# Gradio UI
# ---------------------------------------------------------------------------

_MODEL_INFO = (
    "cyto3 — U-Net backbone. Fast: 5–30s on GPU, 2–10 min on CPU. "
    "Good accuracy for most microscopy images.\n"
    "cpsam — ViT-H (SAM) backbone. Slow: 2–20 min on GPU. "
    "Best accuracy for difficult or low-contrast images."
)

with gr.Blocks(title="Cell Segmentation - Cellpose") as demo:
    gr.Markdown("# Cell Segmentation (Cellpose)")
    gr.Markdown("Upload a microscopy image, adjust parameters, and get segmentation results.")

    with gr.Tabs():
        # ------------------------------------------------------------------ #
        # Tab 1 — Single Image                                                #
        # ------------------------------------------------------------------ #
        with gr.Tab("Single Image"):
            with gr.Row():
                with gr.Column(scale=1):
                    img_input = gr.Image(type="numpy", label="Upload image")
                    diameter = gr.Slider(0, 200, value=30, step=1, label="Diameter (0 = auto)")
                    flow_thresh = gr.Slider(0, 1, value=0.4, step=0.05, label="Flow threshold")
                    cellprob_thresh = gr.Slider(
                        -6, 6, value=0.0, step=0.5, label="Cell probability threshold"
                    )
                    opacity_slider = gr.Slider(
                        0.1, 1.0, value=0.55, step=0.05, label="Overlay Opacity"
                    )
                    model_choice = gr.Radio(
                        choices=["cyto3", "cpsam"],
                        value="cyto3",
                        label="Model",
                        info=_MODEL_INFO,
                    )
                    submit_btn = gr.Button("Segment", variant="primary")

                with gr.Column(scale=2):
                    overlay_output = gr.Image(label="Segmentation overlay")
                    summary_box = gr.Textbox(label="Summary", lines=3)

            with gr.Row():
                stats_table = gr.Dataframe(
                    label="Per-cell statistics",
                    headers=["Cell ID", "Area (px)", "Area (%)"],
                    datatype=["number", "number", "number"],
                    col_count=(3, "fixed"),
                )
                histogram = gr.Plot(label="Size distribution")

            with gr.Row():
                overlay_file = gr.File(label="Download overlay PNG")
                masks_file = gr.File(label="Download masks.npy")
                csv_file = gr.File(label="Download statistics CSV")

            csv_btn = gr.Button("Download Statistics (CSV)")

            submit_btn.click(
                fn=segment,
                inputs=[
                    img_input, diameter, flow_thresh, cellprob_thresh,
                    model_choice, opacity_slider,
                ],
                outputs=[
                    overlay_output, summary_box, stats_table, histogram,
                    overlay_file, masks_file,
                ],
            )
            csv_btn.click(
                fn=export_csv,
                inputs=[stats_table],
                outputs=[csv_file],
            )

        # ------------------------------------------------------------------ #
        # Tab 2 — Batch                                                       #
        # ------------------------------------------------------------------ #
        with gr.Tab("Batch"):
            with gr.Row():
                with gr.Column(scale=1):
                    batch_files = gr.File(
                        file_count="multiple",
                        file_types=["image"],
                        label="Upload images",
                    )
                    batch_diameter = gr.Slider(
                        0, 200, value=30, step=1, label="Diameter (0 = auto)"
                    )
                    batch_flow_thresh = gr.Slider(
                        0, 1, value=0.4, step=0.05, label="Flow threshold"
                    )
                    batch_cellprob_thresh = gr.Slider(
                        -6, 6, value=0.0, step=0.5, label="Cell probability threshold"
                    )
                    batch_opacity = gr.Slider(
                        0.1, 1.0, value=0.55, step=0.05, label="Overlay Opacity"
                    )
                    batch_model = gr.Radio(
                        choices=["cyto3", "cpsam"],
                        value="cyto3",
                        label="Model",
                        info=_MODEL_INFO,
                    )
                    batch_btn = gr.Button("Run Batch", variant="primary")

                with gr.Column(scale=2):
                    batch_summary = gr.Dataframe(
                        label="Batch summary",
                        headers=["Filename", "Model", "Cell count", "Mean area (px)", "Time (s)"],
                        datatype=["str", "str", "number", "number", "number"],
                        col_count=(5, "fixed"),
                    )
                    batch_zip = gr.File(label="Download ZIP (overlays + masks)")

            batch_btn.click(
                fn=batch_segment,
                inputs=[
                    batch_files, batch_diameter, batch_flow_thresh,
                    batch_cellprob_thresh, batch_model, batch_opacity,
                ],
                outputs=[batch_summary, batch_zip],
            )

demo.queue()
demo.launch(server_name="0.0.0.0", server_port=8001)
