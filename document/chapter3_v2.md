# Chapter 3 — Methodology

## 3.1 Methodological Aim and Platform Positioning

This chapter moves from the theoretical background presented in Chapter 2 to the concrete design of the platform developed for this thesis. The central contribution of this work is not the invention of a new segmentation model, nor the creation of a new training dataset. Instead, the contribution is the design of a usable software platform that wraps an existing, state-of-the-art cell segmentation model — Cellpose — and makes it accessible to biological researchers through a standard web browser, while ensuring that all image data remains under the control of the institution.

Chapter 2 established that Cellpose is an effective and well-documented model for fluorescence microscopy cell segmentation. The question this chapter addresses is therefore not how to improve the model itself, but how to build a system around it that meets the practical needs of a biological research group. These needs include browser-based access without any local software installation, support for sensitive or protected microscopy data, reproducible execution across different experiments, and the ability to process both single images and entire datasets in one workflow.

This chapter describes the methodology used to achieve those goals. It explains the requirements that were derived from the use case, the architectural decisions that were taken and why, the design of the request and response workflow, the reasoning behind the data protection strategy, the role of containerisation in ensuring reproducibility, and the distinction between what is implemented in the current proof of concept and what is designed as a future extension of the platform.


## 3.2 Requirements Derived from the Use Case

Before any design decisions were made, it was necessary to identify the concrete requirements of the target user group. The intended users of this platform are researchers in biology or biomedical fields who work with fluorescence microscopy images. They may have a strong domain knowledge in cell biology but limited or no experience with Python environments, command-line tools, or machine learning model APIs.

The first requirement is therefore **browser-based accessibility**. The platform must be reachable through a standard web browser without requiring the user to install any software locally. The user workflow should consist of uploading an image, selecting parameters through graphical controls, and receiving a segmentation result with visual feedback — all within a single web page.

The second requirement is **on-premise deployment**. Fluorescence microscopy images often contain data from ongoing research projects that have not yet been published. In some cases, the images originate from clinical or pre-clinical studies where patient-related information may be present. Sending such images to an external cloud service introduces risks related to data governance and confidentiality. The platform must therefore run entirely within the institution's own server infrastructure, so that images never leave the local network.

The third requirement is **GPU support without client-side hardware**. The Cellpose SAM model (cpsam), which offers the highest segmentation accuracy, requires substantial computational resources. Running inference on a standard laptop or desktop CPU can take between ten and fifteen minutes per image. The institution's server is equipped with an NVIDIA A40 GPU. The platform must be able to exploit this GPU for inference while the user interacts only through a browser.

The fourth requirement is **reproducibility**. A researcher who runs the same image with the same parameters on two different days should receive the same result. This requires that the model, its weights, and all runtime dependencies are fixed and consistent across sessions.

The fifth requirement is **segmentation history and project continuity**. Researchers frequently process many images across multiple sessions. A record of past segmentation jobs — including the image name, model used, cell count, and timestamp — allows researchers to trace their work and compare results over time.

The sixth requirement is **support for three-dimensional microscopy data**. Modern fluorescence microscopy frequently produces z-stacks, which are sequences of image slices taken at different depths through a biological sample. The platform must be capable of processing these multi-frame TIFF files and presenting the results in a navigable format.


## 3.3 Microservice-Oriented System Architecture

### 3.3.1 The Case Against a Monolithic Design

A straightforward approach to building this platform would be to create a single application that combines the user interface, the Cellpose model, and the database within one process and one container. This is known as a monolithic architecture. While it is simple to implement initially, a monolithic design presents several practical problems in the context of this platform.

The most significant problem is the size and complexity of the machine learning dependency. Cellpose depends on PyTorch and, when GPU support is enabled, on a full CUDA-enabled PyTorch installation. Together with the model weight files — approximately 2.4 gigabytes for the cpsam ViT-H checkpoint alone — the total dependency footprint is between six and eight gigabytes. If the user interface code and the inference code are packaged together, then every change to the user interface (for example, adding a new button or changing a label) requires rebuilding and redistributing a six-to-eight-gigabyte image. This makes iterative development very slow.

A second problem with a monolithic design is testability. Testing the user interface independently from the machine learning model requires either loading the full model (which takes 30 to 90 seconds and requires a GPU) or introducing fragile mocking within a single shared codebase. Keeping the two concerns in separate services makes it straightforward to write unit tests for the API endpoints using a lightweight stub of the model, without any GPU or model weights present.

A third problem is maintainability and replaceability. If the segmentation model needs to be updated to a newer version of Cellpose, or replaced with an entirely different model, a monolithic design requires modifying and retesting the entire application. In a separated design, the model service can be updated or replaced as long as it continues to expose the same API contract.

### 3.3.2 The Adopted Architecture

The platform adopted in this thesis follows a microservice-oriented design. The system is divided into three distinct services, each running in its own container, all within a shared private network that is not exposed to the outside world. Figure 3.1 illustrates the overall architecture.

The **App Container** is responsible for all user interaction. It presents the web interface, receives uploaded images, passes segmentation parameters, calls the Model Container over the internal network, renders the results, and displays the segmentation history. It has no direct knowledge of Cellpose, PyTorch, or any machine learning library. Its only connection to the inference layer is through well-defined HTTP calls.

The **Model Container** is responsible for all machine learning operations. It loads the Cellpose models at startup, validates incoming requests, runs inference, records job metadata to the database, and returns the segmentation result. It has no direct connection to the user's browser. It is never exposed on the host network.

The **Database** (PostgreSQL) stores user accounts with hashed passwords and the history of past segmentation jobs. It communicates only with the Model Container and is not reachable from outside the internal network.

This separation of responsibilities produces a clear security boundary: the only component that a user's browser communicates with is the App Container. The Model Container and the database are internal services. Even if a malicious payload were somehow injected through the browser, it could not directly interact with the inference engine or the database.

Looking at Figure 3.1, we can see how requests flow from the user's browser, through a TLS-terminating reverse proxy (nginx), to the App Container. The App Container then issues an internal HTTP request to the Model Container, which runs inference and returns the result. The external network has no visibility into the Model Container or the database.

### 3.3.3 Why This Is Better Than a Single Container for This Use Case

Beyond the general arguments for microservice design, there is a specific reason why this separation is particularly beneficial here. The user interface and the machine learning model have very different change frequencies. The user interface changes frequently as new features are added, layout is adjusted, or workflows are refined. The model and its dependencies change rarely — only when a new version of Cellpose is released or a new model architecture is added. With a two-stage Docker image build strategy, the heavy base image (containing PyTorch, Cellpose, and the model weights) is built once and reused across many code deployments. A typical code change to the App Container or the Model Container's Python source produces an image layer of approximately five megabytes, rather than requiring a rebuild of the full six-to-eight-gigabyte image.


## 3.4 From Cellpose Model to API-Based Model Service

### 3.4.1 Existing Access Methods for Cellpose

Cellpose can be used in several ways. The developers provide a graphical desktop application that allows users to open images, adjust parameters, and view results directly on their own machine. They also provide a command-line interface that can be called from a terminal with flags for model type, diameter, and other parameters. Finally, Cellpose can be used as a Python library by importing it directly into a script or notebook.

Each of these approaches has limitations for the use case described in Section 3.2. The desktop application requires local installation, including Python and all Cellpose dependencies, which is a significant barrier for non-technical users. The command-line interface requires the same installation and assumes that the user is comfortable working in a terminal. The Python library requires knowledge of Python and of the Cellpose API. None of these approaches is suitable for a researcher who simply wants to upload an image from their browser and receive a result.

There is also a hosted version of Cellpose available on HuggingFace Spaces, where users can run segmentation in a browser without any local installation. However, this service requires uploading microscopy images to external servers controlled by a third party. As discussed in Section 3.2, this is not acceptable when images may contain sensitive or unpublished research data.

### 3.4.2 The API-Based Approach

The design adopted in this thesis wraps Cellpose as an HTTP service. Instead of requiring users to install Cellpose locally or upload images to an external cloud, the model is deployed on the institution's own server and is accessible through a well-defined REST API. The API contract consists of three endpoints: a health check endpoint that indicates whether the model is ready to accept requests, a parameters endpoint that describes the available configuration options, and a segment endpoint that accepts an image file and returns the segmentation mask.

This approach changes the relationship between the user and the model in a fundamental way. The user does not need to know that Cellpose exists, what version it is, which Python packages it requires, or whether it is running on a CPU or a GPU. All of these concerns are handled by the platform. The user interacts only with the web interface.

The API design also means that the segmentation capability is no longer tied to a single graphical client. Any application that can send an HTTP request — another Python script, a data processing pipeline, a laboratory information system, or a different web interface — can call the same endpoint and receive the same result. This makes the model service reusable across different research workflows without any additional installation.

FastAPI was selected as the framework for the Model Container because it provides asynchronous request handling, which is essential for a service that may simultaneously receive new requests while a long-running inference job is in progress. Health check requests, for example, must always return quickly even when inference is occupying the compute resources. FastAPI allows these two concerns to be handled by separate execution paths within the same process.


## 3.5 Request and Response Workflow

### 3.5.1 Single Image Segmentation

The standard workflow for a single image begins when the user uploads an image file through the web interface. The supported formats are PNG, JPEG, and TIFF. The App Container receives the uploaded file and prepares it for transmission to the Model Container. The image is encoded as PNG bytes and included in a multipart form-data HTTP POST request to the internal segment endpoint. The request also carries four numerical parameters: the expected cell diameter in pixels (or zero to request automatic detection), the flow threshold, the cell probability threshold, and the choice of segmentation model.

The Model Container receives the request, validates the file format and size, and then decodes the image into a numerical array. It selects the requested model — either the cyto3 U-Net model or the cpsam ViT-H model — and runs inference. Inference is serialised so that only one job runs at a time, preventing memory contention when the server is shared. When inference completes, the resulting mask array is serialised and returned in the HTTP response body as binary data, accompanied by a header indicating which model was actually used.

The App Container receives the response, deserialises the mask array, and uses it to produce three outputs for the user. First, it renders a colour overlay by compositing coloured mask regions over the original image. Each detected cell receives a distinct colour. Second, it computes per-cell statistics including the area in pixels and the percentage of the total image area. Third, it generates a histogram showing the distribution of cell sizes across the image. All three outputs are displayed immediately in the web interface. The user can also download the overlay image as a PNG file and the raw mask array as a NumPy file for use in further analysis.

### 3.5.2 Three-Dimensional Z-Stack Segmentation

For multi-frame TIFF files representing z-stacks, the workflow differs in one important respect. A z-stack TIFF contains multiple image planes, each representing a different focal depth. The App Container sends the raw TIFF file bytes to the Model Container without prior decoding, so that all frames are preserved. The Model Container detects the number of frames using the tifffile library, which provides unambiguous access to frame counts for all TIFF variants. It then runs two-dimensional segmentation on each frame independently and stacks the resulting mask arrays into a three-dimensional output of shape (frames, height, width).

The App Container receives the three-dimensional mask array and allows the user to navigate between frames using a slider control that appears after segmentation completes. The overlay for each selected frame is rendered on demand by reading the corresponding plane from the original TIFF file. Because fluorescence microscopy images are frequently acquired as 16-bit images with pixel values in the range of zero to 65,535, the App Container normalises pixel values to the standard eight-bit display range before rendering the overlay.

### 3.5.3 Batch Segmentation

The batch workflow accepts multiple image files in a single submission. The App Container processes each file sequentially, sending one HTTP request per image to the Model Container and collecting the results. After all files have been processed, the results are assembled into a summary table showing the filename, model used, detected cell count, mean cell area, and processing time for each image. The overlay images and mask arrays for all processed images are packaged into a ZIP archive that the user can download in a single click.

### 3.5.4 Segmentation History

After every successful segmentation, the Model Container records the job metadata — the image filename, the model used, the cell count, and a timestamp — in the PostgreSQL database. The App Container's History tab displays a table of past segmentation jobs. Users see only their own records. An administrator account with elevated privileges can view the records of all users. This allows a research group to track how many images have been processed, which models were used, and what results were obtained over time.


## 3.6 Data Protection and On-Premise Deployment Logic

### 3.6.1 The Data Governance Problem with External Cloud Services

When a researcher uploads a microscopy image to an external cloud service, the data leaves the institution's network and is processed on servers that the institution does not control. This raises several concerns. The cloud provider has access to the image data and, depending on the terms of service, may store, inspect, or use it. If the image originates from a clinical study, it may contain information associated with a patient. Even when the direct patient link is absent, unpublished microscopy data represents proprietary research that the institution may not wish to share with any third party before publication.

The General Data Protection Regulation (GDPR) in the European Union, and equivalent regulations in other jurisdictions, impose obligations on organisations that process personal data. While not all microscopy images constitute personal data in the legal sense, research institutions that handle clinical samples or participant-linked biological material have a responsibility to ensure that data processing takes place under controlled and documented conditions. Relying on an external cloud service for routine image analysis may conflict with these obligations, particularly when data processing agreements are not in place with the cloud provider.

### 3.6.2 How the Platform Supports On-Premise Data Control

The platform developed in this thesis is designed so that all image data remains within the institution's own infrastructure. The images are uploaded by the user's browser to the App Container, which runs on the institution's server. The App Container passes the image over the internal network to the Model Container, which also runs on the same server. The result is returned through the same path. At no point is the image transmitted to any external network address.

The database that stores segmentation history also runs on the institution's server. The institution therefore controls not only the image data but also the record of which images were processed, when, by whom, and with what results. This is important for research accountability and for any internal data management audit.

It is important to note that the platform does not automatically guarantee GDPR compliance. GDPR compliance is a legal and organisational matter that depends on policies, agreements, documentation, and the specific nature of the data involved. What the platform does provide is a technical infrastructure that supports GDPR-oriented deployment: all data storage and processing can remain under institutional control, no third-party network calls are made during the segmentation workflow, and the system is designed to be deployed on servers managed by the institution itself.

This approach is also relevant for research institutions that operate in environments with strict network security policies, such as hospitals or defence-related research laboratories, where outbound data transmission to cloud services may be restricted or prohibited by policy.

### 3.6.3 User Authentication and Access Control

The platform implements user authentication to ensure that only registered members of a research group can access the segmentation tool. Each user creates an account with a username and password. Passwords are stored in the database using the bcrypt hashing algorithm, which means that the actual password is never stored anywhere in the system and cannot be recovered even by an administrator. Authentication is verified through the Model Container's login endpoint, which checks the submitted password against the stored hash. The App Container itself holds no passwords and no credentials; it delegates all authentication to the Model Container.

An administrator account can be configured through environment variables. The administrator can view the segmentation history of all users, which is useful for monitoring usage and verifying that the platform is functioning correctly across the research group.


## 3.7 Reproducibility and Controlled Runtime Environment

### 3.7.1 The Problem of Environment Variability

A common challenge in computational biology is that results produced by a software tool on one machine may not be reproducible on another machine if the software versions, operating system libraries, or runtime configurations differ. Cellpose has undergone several major versions, and the behaviour of its segmentation algorithms differs between versions. A result produced with Cellpose version 2 may not match a result produced with Cellpose version 3 on the same image. For scientific reproducibility, it is necessary to fix the exact version of every dependency.

### 3.7.2 Containerisation as a Reproducibility Strategy

The platform uses Docker containers to ensure that the runtime environment is fully specified and consistent. Each container is built from a `Dockerfile` that specifies the exact base operating system image, the Python version, and the exact versions of all installed packages. Model weights are downloaded and embedded into the container image at build time. When the container starts, no network access is required to fetch weights or packages; everything is already present inside the image.

This means that the Model Container that runs today is byte-for-byte identical to the Model Container that will run next month, assuming the same image is used. The segmentation results for a given image and parameter set are therefore deterministic across time and across different server configurations, as long as the same container image is used.

Two Docker Compose files are provided for local development: one that enables GPU acceleration for servers with a CUDA-capable NVIDIA GPU, and one that falls back to CPU-only mode for machines without a GPU. Both files use the same container images; the difference is only in the resource allocation configuration. This allows developers to run the platform on a standard laptop during development and deploy the same images to the GPU server for production use.

### 3.7.3 Kubernetes Deployment for Production

For the production deployment on the university server, the platform is orchestrated using Kubernetes, specifically MicroK8s — a lightweight Kubernetes distribution suitable for single-node or small-cluster deployments. Kubernetes provides several capabilities that are important for a service that must be reliably available to a research group.

The Kubernetes deployment configuration specifies the resource requirements and limits for each container. The Model Container requests a dedicated NVIDIA A40 GPU, four gigabytes of RAM at minimum, and up to sixty-four gigabytes of RAM to accommodate large images and the cpsam model's memory usage. These resource declarations ensure that Kubernetes schedules the model pod on a node with a GPU and that the container is not terminated due to memory limits during inference.

Kubernetes also provides health check mechanisms called startup probes, liveness probes, and readiness probes. The startup probe for the Model Container allows up to five minutes for the model weights to load before the container is considered healthy. This is important because the cpsam ViT-H weights require between thirty and ninety seconds to load from disk, and without this allowance, the Kubernetes health check would kill the container before it is ready. Once the container is healthy, the liveness probe monitors it periodically and will restart it only if it stops responding, not during normal startup.

A Helm chart packages all the Kubernetes configuration into a parameterisable deployment unit. Helm allows the image registry address, image tag, domain name, GPU count, and other environment-specific values to be specified once and applied consistently across the entire deployment. A single `helm upgrade` command is sufficient to update all three containers to a new version.

### 3.7.4 Continuous Integration and Deployment

The repository includes a GitLab CI/CD pipeline that automates the process of testing, building, and deploying the platform whenever a change is pushed to the main branch. The pipeline consists of five stages.

In the first stage, a suite of unit tests runs against the Model Container API. These tests use a lightweight stub of the Cellpose model — a fake implementation that returns a fixed mask array without loading any weights — so that the tests complete in under five seconds without requiring a GPU. The tests verify the behaviour of the health endpoint, the parameters endpoint, the segment endpoint for multiple image formats, the authentication endpoints, and the history endpoint.

In the second stage, the heavy base Docker image (containing PyTorch, Cellpose, and the model weights) is rebuilt, but only when the dependency file or the base Dockerfile has changed. Because this image is six to eight gigabytes, rebuilding it on every commit would be unnecessarily slow. By constraining the rebuild to dependency changes only, the GPU node's image cache remains warm for all normal code changes.

In the third stage, the thin code-only Docker images are built for both the App Container and the Model Container. These images inherit from the existing base image and add only the Python source files, producing layers of approximately five megabytes each.

In the fourth stage, the Helm chart is applied to the Kubernetes cluster with a thirty-minute timeout to accommodate the case where the base image cache is cold and the full image must be pulled.

In the fifth stage, a verification job checks that the deployed pods are running, that the Model Container's health endpoint returns a successful response, and that a simple segmentation request completes successfully.


## 3.8 Extensibility and Future Platform Functions

The current proof of concept implements the core segmentation workflow described in Sections 3.5 and 3.6. Several additional capabilities have been designed as extensions to this core and are described in the extended system design document (v2), but are not part of the current implementation. This section describes these planned features so that the scope boundaries of the proof of concept are clear.

**Annotation refinement.** The v2 design includes integration with CVAT (Computer Vision Annotation Tool), an open-source annotation platform. In this planned extension, a CVAT container would run alongside the existing containers, sharing the same internal network. A serverless function within CVAT would call the Model Container's segment endpoint to pre-populate annotations automatically, and users could then review and correct the automatically generated boundaries before saving them. This would close the loop between automated segmentation and expert validation, which is often needed in biological research where the model may make errors on unusual cell morphologies.

**Model registry.** The current design supports two fixed model variants (cyto3 and cpsam). A planned extension is a model registry pattern in which new models can be registered and made available through the parameters endpoint without modifying the core application code. This would allow the platform to support future Cellpose versions or entirely different segmentation models through configuration rather than code changes.

**Project management.** The current history implementation records individual segmentation jobs. A future extension would organise jobs into projects, allowing a researcher to group all images from a single experiment together, add notes, and compare results within the project.

**Shared storage volumes.** The v2 design introduces shared Docker volumes for images and results. This would allow the App Container and the Model Container to share access to raw image files and mask outputs without transmitting the data as HTTP request and response bodies. For very large images or batch jobs, this would reduce network overhead within the container network.

These extensions are described here as design intentions. The choice not to implement them in the proof of concept was deliberate: the focus of this thesis is on demonstrating that the core architecture is sound, that the API contract is usable, and that the on-premise deployment model is practical. Additional features would be appropriate for a subsequent development phase.


## 3.9 Representative Test Inputs

Evaluating the platform requires microscopy images with known or verifiable cell structures. Rather than acquiring new experimental data, the methodology relies on publicly available microscopy datasets that are commonly used in the cell segmentation literature. The Broad Bioimage Benchmark Collection and the Cell Tracking Challenge datasets provide fluorescence microscopy images across diverse cell types, magnifications, and imaging conditions. These datasets have associated ground-truth annotations that make it possible to assess whether the platform produces results consistent with those of expert annotators.

For the purpose of this thesis, test images were drawn from these public sources. No proprietary or sensitive research data was used. The test images include single-frame PNG and TIFF files at standard resolutions, as well as multi-frame TIFF z-stacks, to exercise both the two-dimensional and three-dimensional segmentation pathways. The selection covers images where cyto3 is expected to perform well (standard cytoplasm segmentation) and images where cpsam is expected to provide better results (low-contrast or overlapping cells).

A formal evaluation of segmentation quality — for example, intersection-over-union scores against ground truth annotations — is presented in Chapter 4.


## 3.10 Chapter Summary

This chapter has described the methodology underlying the Cell Segmentation Platform. The design was motivated by a set of requirements derived from the biological research use case: browser-based access without local installation, on-premise data control for GDPR-oriented deployments, GPU-accelerated inference without client-side hardware, and reproducible results across sessions.

The architectural response to these requirements is a microservice-oriented system in which the user-facing application and the machine learning inference service run as separate containers. This separation isolates the heavy machine learning dependencies, allows the two services to be developed and deployed independently, and establishes a clear security boundary. The model service exposes Cellpose not as a desktop application or a command-line tool but as an HTTP API, making it accessible from any browser-based client and reusable across different research workflows.

The request and response workflow supports single-image segmentation, three-dimensional z-stack segmentation, and batch processing. Results are returned as visual overlays, statistics tables, and downloadable files. Segmentation history is persisted in a PostgreSQL database that remains within the institution's network.

Reproducibility is ensured through Docker containerisation, which fixes the exact model version and all runtime dependencies inside the container image. Model weights are baked into the image at build time so that container startup requires no network access. Kubernetes orchestration with health probes, resource limits, and a CI/CD pipeline automates reliable deployment on the university server.

Chapter 4 presents the implementation details and the evaluation of the platform against the requirements established here.
