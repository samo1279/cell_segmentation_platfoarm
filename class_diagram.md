# Class Diagram — Cell Segmentation Platform

This document describes the full component model of the POC, including user roles, containers, services, data models, and their relationships.

---

## Full Class Diagram (Mermaid)

```mermaid
classDiagram

    %% ─────────────────────────────────────────────
    %% USER ROLES
    %% ─────────────────────────────────────────────

    class User {
        +String username
        +String password
        +login()
        +uploadImage()
        +runSegmentation()
        +downloadResults()
        +viewOwnHistory()
    }

    class AdminUser {
        +String username
        +String password
        +login()
        +viewAllHistory()
        +viewAnyUserData()
    }

    User <|-- AdminUser : extends

    %% ─────────────────────────────────────────────
    %% APP CONTAINER  (Gradio, port 8001)
    %% ─────────────────────────────────────────────

    class GradioApp {
        +String MODEL_URL
        +String MODEL_PROJECTS_URL
        +String MODEL_API_KEY
        +List~Tuple~ _AUTH_PAIRS
        +String ADMIN_USER
        +launch(auth, server_port)
    }

    class AuthConfig {
        +String APP_USERS_env
        +List~Tuple[str,str]~ _AUTH_PAIRS
        +parse(raw_users_string) List
    }

    class ModelHTTPClient {
        +String model_url
        +String api_key
        +Timeout _MODEL_TIMEOUT
        +call_model(image_bytes, params, username) Tuple
        +call_model_raw(raw_bytes, filename, mime, params, username) Tuple
        +get_projects(username, is_admin) List
    }

    class ImageRenderer {
        +encode_png(image_np) bytes
        +render_overlay(image_np, masks, opacity) ndarray
        +render_zstack_slice(masks, tiff_path, z_idx, model, n_slices, opacity) Tuple
        +compute_stats(masks) Tuple
    }

    class SegmentCallback {
        +segment(image, diameter, flow_thresh, cellprob_thresh, model_type, opacity, request) Tuple
        +segment_3d(tiff_file, diameter, flow_thresh, cellprob_thresh, model_type, opacity, request) Tuple
        +navigate_zslice(z_idx, masks_path, tiff_path, opacity) Tuple
        +batch_segment(files, params, opacity, progress, request) Tuple
        +export_csv(stats_df) str
        +load_history(request) List
    }

    class GradioUI {
        <<Gradio Blocks>>
        +Tab SingleImage
        +Tab Batch
        +Tab History
        +Accordion ZStack3D
        +Slider diameter
        +Slider flow_threshold
        +Slider cellprob_threshold
        +Slider opacity
        +Radio model_choice
        +Button submit_btn
        +Button batch_btn
        +Button zstack_btn
        +Button history_refresh_btn
        +Image overlay_output
        +Dataframe stats_table
        +Plot histogram
        +File overlay_file
        +File masks_file
        +File csv_file
        +Slider zstack_z_slider
        +State zstack_masks_state
        +State zstack_tiff_state
    }

    GradioApp *-- AuthConfig : configures
    GradioApp *-- GradioUI : renders
    GradioApp *-- SegmentCallback : registers
    SegmentCallback --> ModelHTTPClient : uses
    SegmentCallback --> ImageRenderer : uses
    GradioUI --> SegmentCallback : triggers callbacks

    %% ─────────────────────────────────────────────
    %% MODEL CONTAINER  (FastAPI, internal port 8000)
    %% ─────────────────────────────────────────────

    class FastAPIApp {
        +String API_KEY
        +Boolean USE_GPU
        +String DATABASE_URL
        +Semaphore _INFER_SEM
        +lifespan(app)
    }

    class APIKeyMiddleware {
        +String expected_key
        +verify_api_key(x_api_key_header) None
    }

    class CellposeModelRegistry {
        +Dict~str, CellposeModel~ MODELS
        +CellposeModel MODEL
        +load_all_models(use_gpu) void
        +get_model(model_type) CellposeModel
    }

    class CellposeModel {
        <<external: cellpose>>
        +String name
        +Boolean gpu
        +eval(img, diameter, flow_threshold, cellprob_threshold, channel_axis) Tuple
    }

    class SegmentEndpoint {
        +POST /segment
        +validate_input(image, model_type, diameter, flow_threshold, cellprob_threshold, username)
        +detect_zstack(data) Boolean
        +run_2d(frame) ndarray
        +run_inference(img, is_zstack, data) ndarray
        +persist_result(image_filename, model_type, cell_count, username)
        +returns masks_npy as application/octet-stream
    }

    class ProjectsEndpoint {
        +GET /projects
        +filter_by_user(username) List
        +filter_all() List
        +returns List~ProjectRecord~
    }

    class HealthEndpoint {
        +GET /health
        +returns dict ok, models, gpu
    }

    class ParametersEndpoint {
        +GET /parameters
        +returns dict of parameter schemas
    }

    FastAPIApp *-- APIKeyMiddleware : guards routes
    FastAPIApp *-- CellposeModelRegistry : manages
    FastAPIApp *-- SegmentEndpoint : serves
    FastAPIApp *-- ProjectsEndpoint : serves
    FastAPIApp *-- HealthEndpoint : serves
    FastAPIApp *-- ParametersEndpoint : serves
    SegmentEndpoint --> CellposeModelRegistry : calls eval
    SegmentEndpoint --> DatabaseService : writes
    ProjectsEndpoint --> DatabaseService : reads
    CellposeModelRegistry *-- CellposeModel : holds

    %% ─────────────────────────────────────────────
    %% DATABASE  (PostgreSQL 16, internal)
    %% ─────────────────────────────────────────────

    class DatabaseService {
        +String DATABASE_URL
        +psycopg2.connection _db_conn
        +get_conn() connection
        +reconnect_if_needed() void
    }

    class ProjectRecord {
        +int id
        +String project_name
        +String image_filename
        +DateTime timestamp
        +String model_used
        +int cell_count
        +String mask_path
        +String username
    }

    DatabaseService *-- ProjectRecord : stores

    %% ─────────────────────────────────────────────
    %% DATA FLOW RELATIONSHIPS
    %% ─────────────────────────────────────────────

    User --> GradioUI : interacts via browser
    AdminUser --> GradioUI : interacts via browser
    GradioUI --> AuthConfig : authenticates via login screen
    ModelHTTPClient --> FastAPIApp : HTTP POST /segment\n HTTP GET /projects
    FastAPIApp --> CellposeModel : runs inference
    FastAPIApp --> DatabaseService : stores ProjectRecord
```

---

## Component Descriptions

### User Roles

| Role | Access |
|------|--------|
| **User** | Can log in, upload images, run segmentation, download results, view **only their own** history |
| **AdminUser** | Same as User plus can view history for **all** users (username matches `ADMIN_USER` env var) |

### App Container (`App_container/app.py`)

| Component | Responsibility |
|-----------|---------------|
| `GradioApp` | Entry point — configures auth, launches Gradio on port 8001 |
| `AuthConfig` | Parses `APP_USERS` env var into `(username, password)` pairs for Gradio login |
| `ModelHTTPClient` | Sends HTTP requests to Model Container with `X-API-Key` header; forwards `username` as form field |
| `ImageRenderer` | Converts masks to coloured overlays, normalises 16-bit TIFFs, computes per-cell stats |
| `SegmentCallback` | All Python callbacks wired to Gradio buttons; extracts `request.username` for per-user isolation |
| `GradioUI` | Declares the three-tab Blocks layout: Single Image, Batch, History (+ 3D accordion) |

### Model Container (`Model_container/cellpose_api/app.py`)

| Component | Responsibility |
|-----------|---------------|
| `FastAPIApp` | Entry point — FastAPI application with lifespan model loading |
| `APIKeyMiddleware` | `verify_api_key` dependency — returns HTTP 401 when `API_KEY` env is set and header is wrong/missing |
| `CellposeModelRegistry` | Loads `cyto3` and `cpsam` models in parallel at startup; holds them in `MODELS` dict |
| `CellposeModel` | External Cellpose library — performs actual cell segmentation via `eval()` |
| `SegmentEndpoint` | `POST /segment` — validates input, detects z-stack, runs inference, writes to DB, returns `masks.npy` |
| `ProjectsEndpoint` | `GET /projects?user=<name>` — returns user's rows (or all rows when `user` is omitted for admin) |
| `HealthEndpoint` | `GET /health` — returns model load status; returns 503 while loading |
| `ParametersEndpoint` | `GET /parameters` — returns parameter schema (used to populate UI defaults) |

### Database (`postgres:16-alpine`, internal)

| Component | Responsibility |
|-----------|---------------|
| `DatabaseService` | Manages `psycopg2` singleton connection with auto-reconnect; skipped gracefully when `DATABASE_URL` unset |
| `ProjectRecord` | One row per segmentation job — stores `username` for per-user isolation |

---

## Request Flow — Single Image Segmentation

```mermaid
sequenceDiagram
    actor User
    participant Gradio as App Container<br/>(Gradio UI)
    participant Model as Model Container<br/>(FastAPI)
    participant DB as PostgreSQL

    User->>Gradio: Login (username + password)
    Gradio-->>User: Session granted

    User->>Gradio: Upload image + set params + click Segment
    Gradio->>Model: POST /segment<br/>X-API-Key: ***<br/>form: image, model_type, diameter,<br/>flow_threshold, cellprob_threshold,<br/>username=alice
    Model->>Model: Validate input (size, format, dimensions)
    Model->>Model: Detect 2D vs 3D z-stack
    Model->>Model: Run CellposeModel.eval()
    Model->>DB: INSERT INTO projects (... username='alice')
    Model-->>Gradio: masks.npy (application/octet-stream)<br/>X-Model-Used: cyto3
    Gradio->>Gradio: Render overlay + compute stats
    Gradio-->>User: Overlay image + summary + stats table + histogram

    User->>Gradio: Click Refresh in History tab
    Gradio->>Model: GET /projects?user=alice<br/>X-API-Key: ***
    Model->>DB: SELECT ... WHERE username = 'alice'
    DB-->>Model: alice's rows only
    Model-->>Gradio: JSON list of ProjectRecord
    Gradio-->>User: History table (alice's jobs only)
```

---

## Admin vs User Data Isolation

```mermaid
flowchart TD
    Login([User logs in]) --> CheckAdmin{username == ADMIN_USER?}
    CheckAdmin -- Yes --> FetchAll[GET /projects<br/>no ?user= filter<br/>returns ALL rows]
    CheckAdmin -- No --> FetchOwn[GET /projects?user=alice<br/>returns ONLY alice's rows]
    FetchAll --> HistoryAll[History tab shows all users' jobs]
    FetchOwn --> HistoryOwn[History tab shows only own jobs]
```

---

## Environment Variables Summary

| Variable | Container | Purpose |
|----------|-----------|---------|
| `APP_USERS` | App | Comma-separated `user:pass` pairs — enables Gradio login screen |
| `ADMIN_USER` | App | Username that bypasses per-user filter and sees all history |
| `MODEL_API_KEY` | App | Sent as `X-API-Key` header on every call to Model Container |
| `MODEL_URL` | App | URL of Model Container segment endpoint |
| `API_KEY` | Model | Expected `X-API-Key` value; blank = open dev mode |
| `USE_GPU` | Model | `true` / `false` — passed to `CellposeModel(gpu=...)` |
| `DATABASE_URL` | Model | PostgreSQL connection string; blank = no persistence |
