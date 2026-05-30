"""
Unit tests for the Cellpose Model Container FastAPI app.

Cellpose is stubbed out entirely — no GPU, no model weights, no 30-second load.
The stub CellposeModel.eval() returns a fake 2-D mask array so all inference
paths can be exercised in < 5 seconds total.

Run locally:
    cd Model_container
    PYTHONPATH=cellpose_api pytest tests/ -v --junit-xml=report.xml
"""

import io
import sys
import types

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Stub cellpose before the app module is imported.
# The real cellpose imports segment_anything at module level which requires
# torchvision/CUDA — none of which are available in a lightweight CI job.
# ---------------------------------------------------------------------------
_fake_cellpose_pkg = types.ModuleType("cellpose")
_fake_cellpose_models = types.ModuleType("cellpose.models")


class _FakeModel:
    """Minimal stand-in for cellpose.models.CellposeModel."""

    def __init__(self, *args, **kwargs):
        pass

    def eval(self, img, **kwargs):
        """Return a mask with one fake cell in the center quarter."""
        h, w = img.shape[:2]
        masks = np.zeros((h, w), dtype=np.int32)
        masks[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4] = 1
        flows = [np.zeros((2, h, w)), np.zeros((h, w)), np.zeros((h, w))]
        styles = np.zeros(256)
        return masks, flows, styles


_fake_cellpose_models.CellposeModel = _FakeModel
_fake_cellpose_pkg.models = _fake_cellpose_models
sys.modules["cellpose"] = _fake_cellpose_pkg
sys.modules["cellpose.models"] = _fake_cellpose_models

# Safe to import the FastAPI app now
import app as model_app  # noqa: E402  (Model_container/cellpose_api/app.py)
from fastapi.testclient import TestClient  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_png(width: int = 64, height: int = 64, grayscale: bool = False) -> bytes:
    """Return PNG bytes for a random test image."""
    from PIL import Image

    if grayscale:
        arr = np.random.randint(0, 255, (height, width), dtype=np.uint8)
        img = Image.fromarray(arr, mode="L")
    else:
        arr = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
        img = Image.fromarray(arr, mode="RGB")

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    """TestClient without context manager — lifespan never runs, no real Cellpose called."""
    fake = _FakeModel()
    monkeypatch.setitem(model_app.MODELS, "cyto3", fake)
    monkeypatch.setitem(model_app.MODELS, "cpsam", fake)
    monkeypatch.setattr(model_app, "MODEL", fake)
    yield TestClient(model_app.app)


# ---------------------------------------------------------------------------
# GET /health
# ---------------------------------------------------------------------------

class TestHealth:
    def test_ready_returns_200(self, client):
        r = client.get("/health")
        assert r.status_code == 200

    def test_ready_body_ok_true(self, client):
        body = client.get("/health").json()
        assert body["ok"] is True

    def test_ready_body_has_models_dict(self, client):
        body = client.get("/health").json()
        assert "models" in body
        assert body["models"]["cyto3"] is True
        assert body["models"]["cpsam"] is True

    def test_ready_body_has_gpu_field(self, client):
        body = client.get("/health").json()
        assert "gpu" in body

    def test_loading_returns_503_when_models_none(self, monkeypatch):
        monkeypatch.setitem(model_app.MODELS, "cyto3", None)
        monkeypatch.setitem(model_app.MODELS, "cpsam", None)
        c = TestClient(model_app.app)
        r = c.get("/health")
        assert r.status_code == 503

    def test_loading_body_ok_false(self, monkeypatch):
        monkeypatch.setitem(model_app.MODELS, "cyto3", None)
        monkeypatch.setitem(model_app.MODELS, "cpsam", None)
        c = TestClient(model_app.app)
        body = c.get("/health").json()
        assert body["ok"] is False

    def test_partial_optional_model_load_still_ready(self, monkeypatch):
        """Readiness depends on the default model; optional models lazy-load later."""
        monkeypatch.setitem(model_app.MODELS, "cyto3", _FakeModel())
        monkeypatch.setitem(model_app.MODELS, "cpsam", None)
        c = TestClient(model_app.app)
        r = c.get("/health")
        assert r.status_code == 200

    def test_default_model_missing_returns_503(self, monkeypatch):
        monkeypatch.setitem(model_app.MODELS, "cyto3", None)
        monkeypatch.setitem(model_app.MODELS, "cpsam", _FakeModel())
        c = TestClient(model_app.app)
        r = c.get("/health")
        assert r.status_code == 503


# ---------------------------------------------------------------------------
# GET /parameters
# ---------------------------------------------------------------------------

class TestParameters:
    def test_returns_200(self, client):
        assert client.get("/parameters").status_code == 200

    def test_has_required_keys(self, client):
        body = client.get("/parameters").json()
        for key in ("model_type", "diameter", "flow_threshold", "cellprob_threshold"):
            assert key in body, f"Missing key: {key}"

    def test_model_type_options_include_both_models(self, client):
        options = client.get("/parameters").json()["model_type"]["options"]
        assert "cyto3" in options
        assert "cpsam" in options

    def test_default_model_is_cyto3(self, client):
        assert client.get("/parameters").json()["model_type"]["default"] == "cyto3"


# ---------------------------------------------------------------------------
# POST /segment — input validation (422 paths)
# ---------------------------------------------------------------------------

class TestSegmentValidation:
    def test_missing_image_returns_422(self, client):
        r = client.post("/segment")
        assert r.status_code == 422

    def test_invalid_model_type_returns_422(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"model_type": "does_not_exist"},
        )
        assert r.status_code == 422

    def test_invalid_model_type_message_contains_name(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"model_type": "bad_model"},
        )
        assert "bad_model" in r.json()["detail"]

    def test_unsupported_format_txt_returns_422(self, client):
        r = client.post(
            "/segment",
            files={"image": ("file.txt", b"not an image", "text/plain")},
        )
        assert r.status_code == 422

    def test_valid_ext_but_bad_mime_returns_422(self, client):
        # .png extension but text/plain MIME -> rejected only under OR
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "text/plain")},
        )
        assert r.status_code == 422

    def test_bad_ext_but_valid_mime_returns_422(self, client):
        # .bin extension but image/png MIME (real PNG bytes) -> rejected only under OR
        r = client.post(
            "/segment",
            files={"image": ("file.bin", _make_png(), "image/png")},
        )
        assert r.status_code == 422

    def test_oversized_file_returns_422(self, client, monkeypatch):
        monkeypatch.setattr(model_app, "MAX_FILE_SIZE", 10)  # 10 bytes
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
        )
        assert r.status_code == 422

    def test_oversized_file_message_mentions_too_large(self, client, monkeypatch):
        monkeypatch.setattr(model_app, "MAX_FILE_SIZE", 10)
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
        )
        assert "too large" in r.json()["detail"].lower()

    def test_model_lazy_loads_when_missing(self, monkeypatch):
        monkeypatch.setitem(model_app.MODELS, "cyto3", None)
        monkeypatch.setitem(model_app.MODELS, "cpsam", _FakeModel())
        monkeypatch.setattr(model_app, "_load_model_sync", lambda name: _FakeModel())
        c = TestClient(model_app.app)
        r = c.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"model_type": "cyto3"},
        )
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /segment — success paths
# ---------------------------------------------------------------------------

class TestSegmentSuccess:
    def test_cyto3_returns_200(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"model_type": "cyto3"},
        )
        assert r.status_code == 200

    def test_cpsam_returns_200(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"model_type": "cpsam"},
        )
        assert r.status_code == 200

    def test_response_content_type_is_octet_stream(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
        )
        assert r.headers["content-type"] == "application/octet-stream"

    def test_response_body_is_valid_npy(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
        )
        masks = np.load(io.BytesIO(r.content))
        assert masks.ndim == 2

    def test_masks_shape_matches_input(self, client):
        png = _make_png(width=64, height=64)
        r = client.post(
            "/segment",
            files={"image": ("img.png", png, "image/png")},
        )
        masks = np.load(io.BytesIO(r.content))
        assert masks.shape == (64, 64)

    def test_x_model_used_header_cyto3(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"model_type": "cyto3"},
        )
        assert r.headers.get("x-model-used") == "cyto3"

    def test_x_model_used_header_cpsam(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"model_type": "cpsam"},
        )
        assert r.headers.get("x-model-used") == "cpsam"

    def test_default_model_type_is_cyto3(self, client):
        """When model_type is omitted the server should default to cyto3."""
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            # no model_type in form data
        )
        assert r.status_code == 200
        assert r.headers.get("x-model-used") == "cyto3"

    def test_grayscale_image_accepted(self, client):
        png = _make_png(grayscale=True)
        r = client.post(
            "/segment",
            files={"image": ("img.png", png, "image/png")},
        )
        assert r.status_code == 200

    def test_diameter_zero_means_auto_detect(self, client):
        """diameter=0 sent from the UI means auto — must not crash."""
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"diameter": "0"},
        )
        assert r.status_code == 200

    def test_custom_flow_threshold_accepted(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"flow_threshold": "0.8"},
        )
        assert r.status_code == 200

    def test_negative_cellprob_threshold_accepted(self, client):
        r = client.post(
            "/segment",
            files={"image": ("img.png", _make_png(), "image/png")},
            data={"cellprob_threshold": "-3.0"},
        )
        assert r.status_code == 200
