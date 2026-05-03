import csv
import io
import os
import pathlib
import tempfile
import time
import zipfile
import numpy as np
import imageio.v3 as iio
import httpx
import gradio as gr
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from PIL import Image
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------

_MODEL_BASE = os.getenv("MODEL_URL", "http://model:8000/segment").replace("/segment", "")
MODEL_URL = f"{_MODEL_BASE}/segment"
MODEL_PROJECTS_URL = f"{_MODEL_BASE}/projects"
MODEL_REGISTER_URL = f"{_MODEL_BASE}/auth/register"
MODEL_LOGIN_URL = f"{_MODEL_BASE}/auth/login"

MODEL_API_KEY: str | None = os.getenv("MODEL_API_KEY") or None
ADMIN_USER = os.getenv("ADMIN_USER", "admin")


# Tracks temp files from the previous call so they can be deleted at the start
# of the next call (after Gradio has already served them to the browser).
_pending_cleanup: list[str] = []
_pending_batch_cleanup: list[str] = []

_MODEL_TIMEOUT = httpx.Timeout(connect=10.0, write=60.0, read=900.0, pool=10.0)

# Gradio 5 only serves files from its own cache dir (GRADIO_TEMP_DIR).
# Writing our output files there avoids having to whitelist extra paths.
_GRADIO_TMP = os.environ.get("GRADIO_TEMP_DIR") or os.path.join(tempfile.gettempdir(), "gradio")
os.makedirs(_GRADIO_TMP, exist_ok=True)

# ---------------------------------------------------------------------------
# DB-backed Gradio auth callable
# ---------------------------------------------------------------------------

def _auth_fn(username: str, password: str) -> bool:
    """Called by Gradio for every login attempt.

    Delegates credential verification to the Model Container's /auth/login
    endpoint, which checks the bcrypt hash stored in PostgreSQL.
    Returns True to allow login, False to reject.
    """
    try:
        resp = httpx.post(
            MODEL_LOGIN_URL,
            json={"username": username, "password": password},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
        if resp.status_code == 200:
            return bool(resp.json().get("valid", False))
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _encode_png(image_np: np.ndarray) -> bytes:
    """Convert a numpy RGB array to PNG bytes."""
    buf = io.BytesIO()
    Image.fromarray(image_np).save(buf, format="PNG")
    return buf.getvalue()


def _call_model(image_bytes: bytes, diameter, flow_threshold, cellprob_threshold, model_type, username=None):
    """POST image to Model Container; return (masks_np, active_model_str)."""
    form_data: dict = {
        "flow_threshold": flow_threshold,
        "cellprob_threshold": cellprob_threshold,
        "model_type": model_type,
    }
    if diameter > 0:
        form_data["diameter"] = diameter
    if username:
        form_data["username"] = username
    headers = {"X-API-Key": MODEL_API_KEY} if MODEL_API_KEY else {}
    resp = httpx.post(
        MODEL_URL,
        files={"image": ("image.png", image_bytes, "image/png")},
        data=form_data,
        headers=headers,
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

def segment(image, diameter, flow_threshold, cellprob_threshold, model_type, opacity, request: gr.Request = None):
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

    username = getattr(request, "username", None) if request else None
    image_bytes = _encode_png(image)

    try:
        masks, active_model = _call_model(
            image_bytes, diameter, flow_threshold, cellprob_threshold, model_type,
            username=username,
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
    overlay_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=_GRADIO_TMP)
    overlay_path = overlay_tmp.name
    overlay_tmp.close()
    Image.fromarray(overlay_uint8).save(overlay_path)

    masks_tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False, dir=_GRADIO_TMP)
    masks_path = masks_tmp.name
    masks_tmp.close()
    np.save(masks_path, masks)

    _pending_cleanup.extend([overlay_path, masks_path])

    return overlay_path, summary, stats_rows, fig, overlay_path, masks_path


def _call_model_raw(raw_bytes: bytes, filename: str, mime: str, diameter, flow_threshold, cellprob_threshold, model_type, username=None):
    """POST raw file bytes to Model Container (used for 3D TIFF z-stacks to preserve all frames)."""
    form_data: dict = {
        "flow_threshold": flow_threshold,
        "cellprob_threshold": cellprob_threshold,
        "model_type": model_type,
    }
    if diameter > 0:
        form_data["diameter"] = diameter
    if username:
        form_data["username"] = username
    headers = {"X-API-Key": MODEL_API_KEY} if MODEL_API_KEY else {}
    resp = httpx.post(
        MODEL_URL,
        files={"image": (filename, raw_bytes, mime)},
        data=form_data,
        headers=headers,
        timeout=_MODEL_TIMEOUT,
    )
    resp.raise_for_status()
    active_model = resp.headers.get("x-model-used", model_type)
    masks = np.load(io.BytesIO(resp.content))
    return masks, active_model


def _render_zstack_slice(masks: np.ndarray, tiff_path: str, z_idx: int, model_name: str, n_slices: int, opacity: float):
    """Render a coloured overlay for one z-slice; return (overlay_path, summary_str)."""
    is_zstack = masks.ndim == 3
    if is_zstack:
        slice_masks = masks[z_idx]
        frames = iio.imread(tiff_path)
        frame = frames[z_idx] if frames.ndim >= 3 else frames
    else:
        slice_masks = masks
        frame = iio.imread(tiff_path)

    if frame.ndim == 2:
        frame_rgb = np.stack([frame, frame, frame], axis=-1)
    elif frame.shape[-1] >= 4:
        frame_rgb = frame[:, :, :3]
    else:
        frame_rgb = frame

    # Normalize to uint8 — handles 16-bit (uint16) microscopy TIFFs where values
    # can be 0-65535. Without normalization the image appears completely black.
    frame_rgb = frame_rgb.astype(np.float32)
    fmin, fmax = frame_rgb.min(), frame_rgb.max()
    if fmax > fmin:
        frame_rgb = (frame_rgb - fmin) / (fmax - fmin) * 255.0
    frame_uint8 = frame_rgb.astype(np.uint8)

    overlay_uint8 = _render_overlay(frame_uint8, slice_masks, opacity)
    cell_count = int(len(np.unique(slice_masks)) - 1)

    ov_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=_GRADIO_TMP)
    Image.fromarray(overlay_uint8).save(ov_tmp.name)
    ov_tmp.close()

    summary = (
        f"Model: {model_name} | Z-stack: {n_slices} slice(s) | "
        f"Slice {z_idx + 1}/{n_slices}: {cell_count} cells"
    )
    return ov_tmp.name, summary


# ---------------------------------------------------------------------------
# 3-D Z-stack segmentation
# ---------------------------------------------------------------------------

def segment_3d(tiff_file, diameter, flow_threshold, cellprob_threshold, model_type, opacity, request: gr.Request = None):
    """Send a raw multi-frame TIFF to the model; return per-slice overlay."""
    if tiff_file is None:
        raise gr.Error("Please upload a multi-frame TIFF file.")

    username = getattr(request, "username", None) if request else None
    file_path = tiff_file if isinstance(tiff_file, str) else tiff_file.name
    filename = os.path.basename(file_path)

    with open(file_path, "rb") as fh:
        raw_bytes = fh.read()

    try:
        masks, active_model = _call_model_raw(
            raw_bytes, filename, "image/tiff",
            diameter, flow_threshold, cellprob_threshold, model_type,
            username=username,
        )
    except httpx.HTTPStatusError as e:
        raise gr.Error(f"Segmentation failed (HTTP {e.response.status_code}): {e.response.text}")
    except httpx.TimeoutException:
        raise gr.Error("Segmentation timed out after 15 minutes.")
    except httpx.RequestError as e:
        raise gr.Error(f"Cannot reach model container ({type(e).__name__}).")

    n_slices = masks.shape[0] if masks.ndim == 3 else 1

    # Save full 3D masks for download
    masks_tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False, dir=_GRADIO_TMP)
    np.save(masks_tmp.name, masks)
    masks_tmp.close()

    overlay_path, summary = _render_zstack_slice(masks, file_path, 0, active_model, n_slices, opacity)
    slider_update = gr.update(maximum=n_slices - 1, value=0, visible=n_slices > 1)

    # Return masks_path twice: once for download File, once for gr.State
    return overlay_path, summary, masks_tmp.name, masks_tmp.name, file_path, slider_update


def navigate_zslice(z_idx, masks_path, tiff_path, opacity):
    """Re-render overlay when the user moves the z-slice slider."""
    if not masks_path or not tiff_path:
        return None, ""
    masks = np.load(masks_path)
    n_slices = masks.shape[0] if masks.ndim == 3 else 1
    overlay_path, summary = _render_zstack_slice(masks, tiff_path, int(z_idx), "–", n_slices, opacity)
    return overlay_path, summary


def export_csv(stats_df):
    """Export the per-cell stats Dataframe as a CSV file."""
    if stats_df is None or len(stats_df) == 0:
        raise gr.Error("No statistics to export — run segmentation first.")
    tmp = tempfile.NamedTemporaryFile(
        suffix=".csv", delete=False, mode="w", newline="", dir=_GRADIO_TMP
    )
    tmp.close()
    # gr.Dataframe passes a pandas DataFrame when used as input
    stats_df.to_csv(tmp.name, index=False)
    return tmp.name


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def load_history(request: gr.Request = None):
    """Fetch past segmentation jobs from GET /projects.

    Each user sees only their own segmentation records.
    """
    try:
        headers = {"X-API-Key": MODEL_API_KEY} if MODEL_API_KEY else {}
        username = getattr(request, "username", None) if request else None
        params = {} if not username or username == ADMIN_USER else {"user": username}
        resp = httpx.get(
            MODEL_PROJECTS_URL,
            headers=headers,
            params=params,
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
        resp.raise_for_status()
        data = resp.json()
        rows = [
            [
                entry.get("id", ""),
                entry.get("image_filename", ""),
                entry.get("model_used", ""),
                entry.get("cell_count", ""),
                entry.get("timestamp", ""),
            ]
            for entry in (data if isinstance(data, list) else [])
        ]
        return rows
    except Exception:
        return []


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
    request: gr.Request = None,
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

    username = getattr(request, "username", None) if request else None
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
                image_bytes, diameter, flow_threshold, cellprob_threshold, model_type,
                username=username,
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

        ov_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=_GRADIO_TMP)
        ov_path = ov_tmp.name
        ov_tmp.close()
        Image.fromarray(overlay_uint8).save(ov_path)

        msk_tmp = tempfile.NamedTemporaryFile(suffix=".npy", delete=False, dir=_GRADIO_TMP)
        msk_path = msk_tmp.name
        msk_tmp.close()
        np.save(msk_path, masks)

        overlay_entries.append((f"overlays/{stem}_overlay.png", ov_path))
        masks_entries.append((f"masks/{stem}_masks.npy", msk_path))
        _pending_batch_cleanup.extend([ov_path, msk_path])

        summary_rows.append([filename, active_model, cell_count, mean_area, elapsed])

    progress(1.0, desc="Building ZIP…")

    zip_tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False, dir=_GRADIO_TMP)
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

            # -------------------------------------------------------------- #
            # 3-D Z-Stack section (inside Single Image tab)                  #
            # -------------------------------------------------------------- #
            with gr.Accordion("3D Z-Stack (multi-frame TIFF)", open=False):
                gr.Markdown(
                    "Upload a **multi-frame TIFF** (z-stack). "
                    "The backend automatically detects the number of slices and segments each one independently. "
                    "Use the **Z-slice** slider to browse per-slice overlays after segmentation."
                )
                with gr.Row():
                    with gr.Column(scale=1):
                        zstack_file = gr.File(
                            file_types=[".tiff", ".tif"],
                            label="Upload multi-frame TIFF",
                        )
                        zstack_z_slider = gr.Slider(
                            minimum=0, maximum=0, step=1, value=0,
                            label="Z-slice", visible=False,
                        )
                        zstack_btn = gr.Button("Segment 3D Z-Stack", variant="primary")
                    with gr.Column(scale=2):
                        zstack_overlay = gr.Image(label="Z-slice overlay")
                        zstack_summary = gr.Textbox(label="Z-stack summary", lines=2)
                        zstack_masks_file = gr.File(label="Download 3D masks.npy (all slices)")

                # Hidden state components for slice navigation
                zstack_masks_state = gr.State(None)
                zstack_tiff_state = gr.State(None)

                zstack_btn.click(
                    fn=segment_3d,
                    inputs=[
                        zstack_file, diameter, flow_thresh, cellprob_thresh,
                        model_choice, opacity_slider,
                    ],
                    outputs=[
                        zstack_overlay, zstack_summary,
                        zstack_masks_file, zstack_masks_state,
                        zstack_tiff_state, zstack_z_slider,
                    ],
                )
                zstack_z_slider.change(
                    fn=navigate_zslice,
                    inputs=[zstack_z_slider, zstack_masks_state, zstack_tiff_state, opacity_slider],
                    outputs=[zstack_overlay, zstack_summary],
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

        # ------------------------------------------------------------------ #
        # Tab 3 — History                                                     #
        # ------------------------------------------------------------------ #
        with gr.Tab("History"):
            gr.Markdown(
                "Past segmentation jobs recorded by the Model Container. "
                "Click **Refresh** to fetch the latest data."
            )
            with gr.Row():
                history_refresh_btn = gr.Button("Refresh")
                history_load_btn = gr.Button("Load Selected")
            history_table = gr.Dataframe(
                label="Segmentation history",
                headers=["ID", "Image name", "Model", "Cell count", "Timestamp"],
                datatype=["number", "str", "str", "number", "str"],
                col_count=(5, "fixed"),
                interactive=False,
            )

            history_refresh_btn.click(
                fn=load_history,
                inputs=[],
                outputs=[history_table],
            )

demo.queue()

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# HTML page templates — loaded from templates/ at startup.
# Keeping HTML in separate files prevents large inline strings from
# obscuring the Python application logic.
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = pathlib.Path(__file__).parent / "templates"

def _load_template(name: str) -> str:
    return (_TEMPLATES_DIR / name).read_text(encoding="utf-8")

_LANDING_HTML = _load_template("landing.html")
_SIGNIN_HTML  = _load_template("signin.html")
_REGISTER_HTML = _load_template("register.html")

# ---------------------------------------------------------------------------
# FastAPI host application — define auth routes BEFORE Gradio mount.
# ---------------------------------------------------------------------------

app = FastAPI()


@app.get("/healthz", status_code=200)
async def healthz():
    """Simple, unauthenticated health check endpoint for Kubernetes probes."""
    return {"status": "ok"}


@app.get("/")
async def _landing_page():
    """Public landing page with Sign In / Register CTA buttons."""
    return HTMLResponse(_LANDING_HTML)


@app.get("/sign-in")
async def _signin_page():
    """Sign In form page."""
    return HTMLResponse(_SIGNIN_HTML)


@app.get("/register")
async def _register_page():
    """Registration form page."""
    return HTMLResponse(_REGISTER_HTML)


@app.post("/auth/register")
async def _register_proxy(request: Request):
    """Forward registration to Model Container's /auth/register."""
    try:
        body = await request.body()
        resp = httpx.post(
            MODEL_REGISTER_URL,
            content=body,
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        )
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception:
        return JSONResponse(
            {"detail": "Could not reach the authentication server. Please try again later."},
            status_code=503,
        )


# Mount Gradio at "/app" — protected by Gradio's built-in auth.
# After Gradio login succeeds, user is redirected to /app.
# Landing page at "/" is public (no auth required).
app = gr.mount_gradio_app(
    app,
    demo,
    path="/app",
    auth=_auth_fn,
    auth_message=(
        "New user? "
        "<a href='/register' style='color:#f97316;font-weight:600;text-decoration:underline'>"
        "Create an account</a>"
    ),
    max_file_size="50mb",
    allowed_paths=[_GRADIO_TMP],
)


# Gradio's auth middleware intercepts "/" for unauthenticated requests and
# redirects to the Gradio login page.  This middleware runs as the outermost
# wrapper (added last = runs first) so it always returns our custom landing
# page for the root path before Gradio's middleware ever sees the request.
@app.middleware("http")
async def _security_and_routing_middleware(request, call_next):
    path = request.url.path
    if path in ("/", ""):
        return HTMLResponse(_LANDING_HTML)
    # Intercept Gradio's built-in login page redirect to break the
    # /app <-> /app/login loop caused by stale/invalid session cookies.
    # Clear the stale cookies and send the user to our custom sign-in page.
    if path == "/app/login" and request.method == "GET":
        resp = RedirectResponse("/sign-in", status_code=302)
        resp.delete_cookie("access-token", path="/")
        resp.delete_cookie("access-token-unsecure", path="/")
        return resp
    response = await call_next(request)
    # Security headers — applied to every response.
    # Ref: https://owasp.org/www-project-secure-headers/
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "0"  # disabled in favour of CSP
    return response

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
