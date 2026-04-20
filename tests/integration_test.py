"""
Integration test for the Cell Segmentation Platform — Phase 1D verification.

NOT part of CI. Run manually after `docker compose up --build`.

Usage (recommended — runs inside the Docker network so port 8000 is reachable):
    docker compose exec app python /tests/integration_test.py

Usage (if model port is temporarily mapped to host, e.g. for local dev):
    MODEL_URL=http://localhost:8000  APP_URL=http://localhost:8001  python tests/integration_test.py

Environment variables:
    MODEL_URL   Base URL of the Model Container  (default: http://model:8000)
    APP_URL     Base URL of the App Container    (default: http://localhost:8001)
    IMAGE_PATH  Path to test image               (default: media/test/001_img.png)
    TIMEOUT     Per-request timeout in seconds   (default: 300)
"""

import io
import os
import sys
import time
import pathlib

import httpx
import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
MODEL_URL = os.environ.get("MODEL_URL", "http://model:8000").rstrip("/")
APP_URL = os.environ.get("APP_URL", "http://localhost:8001").rstrip("/")

# Resolve relative to repo root regardless of cwd
_REPO_ROOT = pathlib.Path(__file__).parent.parent
IMAGE_PATH = pathlib.Path(os.environ.get("IMAGE_PATH", _REPO_ROOT / "media" / "test" / "001_img.png"))
TIMEOUT = int(os.environ.get("TIMEOUT", "300"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
WARN = "\033[93mWARN\033[0m"

_results: list[dict] = []


def _record(name: str, passed: bool, detail: str = "") -> None:
    status = PASS if passed else FAIL
    label = f"[{status}] {name}"
    if detail:
        label += f" — {detail}"
    print(label)
    _results.append({"name": name, "passed": passed, "detail": detail})


def _assert(name: str, condition: bool, detail: str = "") -> None:
    _record(name, condition, detail)
    if not condition:
        # Non-fatal; accumulate failures and report at the end.
        pass


# ---------------------------------------------------------------------------
# Test 1 — Model Container health
# ---------------------------------------------------------------------------
def test_model_health(client: httpx.Client) -> None:
    print("\n--- Test 1: Model Container health ---")
    try:
        r = client.get(f"{MODEL_URL}/health", timeout=TIMEOUT)
        _assert("GET /health → 200", r.status_code == 200, f"status={r.status_code}")
        if r.status_code == 200:
            body = r.json()
            _assert("ok: true", body.get("ok") is True, str(body))
    except httpx.ConnectError as exc:
        _record(
            "GET /health reachable",
            False,
            f"Connection refused: {exc}. "
            "Run this script inside the Docker network: docker compose exec app python /tests/integration_test.py",
        )


# ---------------------------------------------------------------------------
# Test 2 — App Container (Gradio UI) reachable
# ---------------------------------------------------------------------------
def test_app_reachable(client: httpx.Client) -> None:
    print("\n--- Test 2: App Container (Gradio) reachable ---")
    try:
        r = client.get(f"{APP_URL}/", timeout=30)
        _assert("GET / → 200", r.status_code == 200, f"status={r.status_code}")
    except httpx.ConnectError as exc:
        _record("GET / reachable", False, f"Connection refused: {exc}")


# ---------------------------------------------------------------------------
# Test 3 — POST /segment with cyto3 (default)
# ---------------------------------------------------------------------------
def _post_segment(
    client: httpx.Client,
    image_path: pathlib.Path,
    model_type: str = "cyto3",
    diameter: float = 30.0,
    flow_threshold: float = 0.4,
    cellprob_threshold: float = 0.0,
) -> tuple[httpx.Response, float]:
    with open(image_path, "rb") as fh:
        img_bytes = fh.read()

    t0 = time.perf_counter()
    r = client.post(
        f"{MODEL_URL}/segment",
        files={"image": (image_path.name, img_bytes, "image/png")},
        data={
            "model_type": model_type,
            "diameter": str(diameter),
            "flow_threshold": str(flow_threshold),
            "cellprob_threshold": str(cellprob_threshold),
        },
        timeout=TIMEOUT,
    )
    elapsed = time.perf_counter() - t0
    return r, elapsed


def _validate_segment_response(
    r: httpx.Response,
    elapsed: float,
    image_name: str,
    model_type: str,
) -> int:
    """Assert correctness of a /segment response and return cell count (or 0 on failure)."""
    _assert(f"POST /segment ({model_type}) → 200", r.status_code == 200, f"status={r.status_code}")
    if r.status_code != 200:
        _assert(f"POST /segment ({model_type}) body", False, r.text[:200])
        return 0

    # X-Model-Used header
    x_model = r.headers.get("x-model-used", "")
    _assert(
        f"X-Model-Used header present ({model_type})",
        bool(x_model),
        f"got: '{x_model}'",
    )
    _assert(
        f"X-Model-Used matches requested model ({model_type})",
        x_model == model_type,
        f"expected='{model_type}' got='{x_model}'",
    )

    # Content-Type
    ct = r.headers.get("content-type", "")
    _assert(
        f"Content-Type is octet-stream ({model_type})",
        "octet-stream" in ct,
        f"got: '{ct}'",
    )

    # Valid .npy content
    try:
        masks = np.load(io.BytesIO(r.content))
    except Exception as exc:
        _assert(f"Valid .npy content ({model_type})", False, str(exc))
        return 0

    _assert(
        f"masks is 2-D integer array ({model_type})",
        masks.ndim == 2 and np.issubdtype(masks.dtype, np.integer),
        f"shape={masks.shape} dtype={masks.dtype}",
    )

    cell_count = int(len(np.unique(masks)) - 1)  # 0 = background
    _assert(
        f"Non-zero cell count ({model_type})",
        cell_count > 0,
        f"cells={cell_count}",
    )

    # Summary row for final report
    _results.append(
        {
            "name": "__summary__",
            "image": image_name,
            "model": model_type,
            "cells": cell_count,
            "elapsed_s": round(elapsed, 1),
            "passed": True,
        }
    )
    return cell_count


def test_segment_cyto3(client: httpx.Client, image_path: pathlib.Path) -> None:
    print("\n--- Test 3: POST /segment (cyto3 — default) ---")
    if not image_path.exists():
        _record("Image file exists", False, str(image_path))
        return
    _record("Image file exists", True, str(image_path))
    r, elapsed = _post_segment(client, image_path, model_type="cyto3")
    _validate_segment_response(r, elapsed, image_path.name, "cyto3")


# ---------------------------------------------------------------------------
# Test 4 — POST /segment with cpsam
# ---------------------------------------------------------------------------
def test_segment_cpsam(client: httpx.Client, image_path: pathlib.Path) -> None:
    print("\n--- Test 4: POST /segment (cpsam) ---")
    if not image_path.exists():
        _record("Image file exists (cpsam)", False, str(image_path))
        return
    r, elapsed = _post_segment(client, image_path, model_type="cpsam")
    _validate_segment_response(r, elapsed, image_path.name, "cpsam")


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------
def _print_summary() -> int:
    print("\n" + "=" * 60)
    print("INTEGRATION TEST SUMMARY")
    print("=" * 60)

    # Segmentation rows
    seg_rows = [e for e in _results if e.get("name") == "__summary__"]
    if seg_rows:
        print(f"\n{'Image':<20} {'Model':<10} {'Cells':>6} {'Time (s)':>10}")
        print("-" * 50)
        for row in seg_rows:
            print(f"{row['image']:<20} {row['model']:<10} {row['cells']:>6} {row['elapsed_s']:>10.1f}")

    # Pass/fail counts
    checks = [e for e in _results if e.get("name") != "__summary__"]
    passed = sum(1 for e in checks if e["passed"])
    failed = sum(1 for e in checks if not e["passed"])

    print(f"\nChecks passed: {passed}")
    print(f"Checks failed: {failed}")

    if failed:
        print("\nFailed checks:")
        for e in checks:
            if not e["passed"]:
                print(f"  - {e['name']}: {e.get('detail', '')}")

    overall = failed == 0
    status = PASS if overall else FAIL
    print(f"\nOverall result: [{status}]")
    return 0 if overall else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> int:
    print("Cell Segmentation Platform — Integration Tests")
    print(f"  MODEL_URL  : {MODEL_URL}")
    print(f"  APP_URL    : {APP_URL}")
    print(f"  IMAGE_PATH : {IMAGE_PATH}")
    print(f"  TIMEOUT    : {TIMEOUT}s")

    with httpx.Client() as client:
        test_model_health(client)
        test_app_reachable(client)
        test_segment_cyto3(client, IMAGE_PATH)
        test_segment_cpsam(client, IMAGE_PATH)

    return _print_summary()


if __name__ == "__main__":
    sys.exit(main())
