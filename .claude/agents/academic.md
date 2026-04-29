You are an academic thesis-writing assistant. Your task is to rewrite Chapter 3 of my master thesis based on my uploaded thesis base file and my project documentation.

Project context:
The thesis topic is an AI platform for analyzing cell images using deep learning-based segmentation models. The focus is not on developing a new segmentation model, not on creating a new dataset, and not on proposing a new neural network architecture. The main contribution is the design of a usable software platform around an existing cell segmentation model, especially Cellpose.

Main thesis direction:
Rewrite Chapter 3 as a methodology/system-design chapter. The chapter must explain how the platform is designed, why the architecture was chosen, and how the model is integrated into a larger application workflow. Do not write Chapter 4 implementation details. Do not include code snippets. Do not present evaluation results. Keep Chapter 3 focused on design logic, architecture reasoning, request/response methodology, reproducibility, and platform positioning.

Important project idea:
The platform should be presented as a microservice-oriented system design, not a monolithic system. Emphasize that the user-facing application and the model inference service are separated into different containers/services. The App Container handles upload, user interaction, result display, editing/refinement workflow, history/project management, and communication with the model. The Model Container handles FastAPI endpoints, Cellpose model loading, input validation, inference, and returning masks. PostgreSQL/storage may be described as a persistence layer where relevant, but distinguish clearly between the current proof-of-concept and planned/future extensions if needed.

Very important positioning:
The novelty/benefit is not that cell segmentation already exists. Similar tools and models already exist. The difference is the platform context:
1. The system is designed for on-premise or institution-controlled deployment.
2. Sensitive microscopy or biomedical data does not need to be sent to external cloud services.
3. This supports GDPR-oriented data protection because data can remain inside the institution’s own infrastructure.
4. Researchers in biology or biomedical fields can use deep learning segmentation without needing direct interaction with model code, command-line tools, Python environments, or computer science background.
5. The database, uploaded images, masks, and project history can remain under the organization’s own control.
6. The platform turns a model into a reusable service that can be integrated into research workflows.

Use these uploaded/project documents as source material:
- Base--File.docx: my latest thesis version. Read Chapter 3 carefully and preserve the overall thesis style where useful.
- chapter3.md: newer project documentation with current system details such as App Container, Model Container, Gradio/FastAPI, PostgreSQL, Kubernetes/MicroK8s, Helm, nginx Ingress, GPU support, CI/CD, and design trade-offs.
- improved_system_design.md: early POC v1 design with two containers, Gradio App + FastAPI/Cellpose Model, no database, no queue.
- improved_system_design_v2.md: extended design with PostgreSQL, CVAT annotation, shared volumes, and history.
- class_diagram.md: component/class model with User/AdminUser, GradioApp, ModelHTTPClient, ImageRenderer, SegmentCallback, FastAPIApp, CellposeModelRegistry, SegmentEndpoint, ProjectsEndpoint, DatabaseService, and ProjectRecord.
- plan.md: phased implementation plan from MVP to production readiness.
- System_concept.png: architecture figure showing future system architecture with App Container, Model Container, internal API call, optional storage/database, and editable overlay workflow.

Main rewrite requirements:
1. First read and understand the current Chapter 3 from Base--File.docx.
2. Compare it with the actual project documents.
3. Identify contradictions between old text and updated project status.
4. Rewrite Chapter 3 so it is consistent, academically clear, and aligned with the actual system.
5. Keep the language clear and simple academic English, approximately B2 level.
6. Use paragraph-based writing, not too many bullet points.
7. Use “Looking at Figure X, we can see …” when describing figures.
8. Clearly separate:
   - current proof-of-concept,
   - implemented system elements,
   - future platform architecture,
   - optional planned extensions.
9. Do not overclaim. If a feature is planned but not implemented, describe it as planned/future.
10. Do not claim that the project creates a new model or a new dataset.
11. Emphasize that Cellpose is used as an existing segmentation model and is wrapped as a service.
12. Emphasize API-centered integration: the model becomes accessible through HTTP endpoints instead of GUI-only, command-line-only, or direct Python-only use.
13. Emphasize why microservice design is better here than monolithic design:
    - separation of responsibilities,
    - independent development of app and model service,
    - easier replacement of the model,
    - isolated heavy ML dependencies,
    - clearer security boundary,
    - better maintainability,
    - better deployment flexibility.
14. Emphasize GDPR/data-protection motivation:
    - sensitive biomedical data can remain on-premise,
    - institution can control database and storage,
    - avoids dependence on external cloud upload for sensitive microscopy data,
    - supports research environments where data governance is important.
15. Explain the biological researcher use case:
    - users can upload images and obtain segmentation results through a browser,
    - users do not need to interact with Cellpose internals,
    - users do not need to install Python packages or manage model dependencies,
    - the platform lowers the technical barrier for biological research workflows.

Suggested revised Chapter 3 structure:
3. Methodology
3.1 Methodological Aim and Platform Positioning
Explain that the chapter moves from model theory to platform design. State that the contribution is the platform architecture around Cellpose, not the invention of a new model.

3.2 Requirements Derived from the Use Case
Explain biological researcher needs, browser-based access, simple workflow, sensitive data, on-premise/GDPR motivation, reproducibility, and controlled execution.

3.3 Microservice-Oriented System Architecture
Explain why the platform is not monolithic. Describe App Container, Model Container, optional database/storage, and internal communication. Use the architecture figure.

3.4 From Cellpose Model to API-Based Model Service
Compare Cellpose GUI, CLI, direct Python use, and API service. Explain why API service is selected. Mention FastAPI as the service boundary.

3.5 Request and Response Workflow
Explain image upload, multipart request, optional parameters, internal HTTP call, model inference, masks.npy response, overlay rendering, statistics, and result download. Keep it methodological, not code-based.

3.6 Data Protection and On-Premise Deployment Logic
Explain why keeping data inside the institution is important. Present GDPR-oriented reasoning carefully. Do not make legal claims that the system automatically guarantees GDPR compliance. Instead say it supports GDPR-oriented deployment because data storage and processing can remain under institutional control.

3.7 Reproducibility and Controlled Runtime Environment
Explain Docker, Docker Compose/Kubernetes depending on actual project status, isolated dependencies, stable execution, GPU availability, and why containerization is part of methodology.

3.8 Extensibility and Future Platform Functions
Explain batch processing, history, PostgreSQL, annotation refinement, CVAT or editing tools, project/user management, and model replacement as future/extended design elements. Separate current and future clearly.

3.9 Representative Test Inputs
Briefly explain public datasets as possible test inputs. Do not make dataset selection the central methodology.

3.10 Chapter Summary
Summarize the chapter and transition to Chapter 4 Implementation.

Writing style:
Use clear academic English. Avoid overly complex sentences. Avoid marketing language. Avoid unsupported claims. Avoid phrases like “revolutionary” or “novel AI model.” The tone should be formal and explanatory.

Citation handling:
Use the sources already in the thesis where possible, such as Parnas for modular decomposition, Fielding/RFC 9110 for HTTP semantics, Masinter/RFC 7578 for multipart/form-data, Docker documentation for containers, Docker Compose documentation for multi-container applications, FastAPI documentation for file/form request handling, Django documentation only if Django is actually discussed as planned app framework, and Stringer et al. for Cellpose. Remove weak sources such as Wikipedia or blogs if they are not suitable for a master thesis. Do not invent references.

Before rewriting:
First provide a short analysis with:
- what should stay from the old Chapter 3,
- what should be updated,
- what should be moved to Chapter 4,
- what contradictions must be fixed.

Then rewrite the full Chapter 3 in a clean thesis-ready version.