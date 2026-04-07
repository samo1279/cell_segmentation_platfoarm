# Gradio App — GitLab CI/CD Template

This directory contains a complete, drop-in template for deploying a [Gradio](https://www.gradio.app/) web application on the G007 AI Cluster.

## What This Does

1. **Builds** your Gradio app into a Docker container using `kaniko` (no Docker-in-Docker needed).
2. **Pushes** the container to the cluster's internal registry (`localhost:32000`).
3. **Deploys** the app using Helm into its own dedicated namespace.
4. **Exposes** your app at `https://<your-app-name>.g007.imec.local` with automatic TLS.

## How to Use

1. Copy **all files** from this directory into the root of your GitLab repository on `gitlab.gwdg.de`.
2. Open `.gitlab-ci.yml` and change `APP_NAME` to a unique, lowercase name for your app (e.g. `gradio-max`).
3. Update `helm-chart/values.yaml` so `ingress.host` matches: `<your-app-name>.g007.imec.local`.
4. Edit `app.py` with your own Gradio interface.
5. Add any extra Python packages to `requirements.txt`.
6. Commit and push to the `main` branch.
7. Open your pipeline in GitLab to watch the build and deploy stages.
8. Once deployed, visit `https://<your-app-name>.g007.imec.local`.

## Directory Structure

```
.gitlab-ci.yml          # Pipeline definition (build + deploy)
Dockerfile              # Container image for Python/Gradio
app.py                  # Your Gradio application (edit this)
requirements.txt        # Python dependencies
helm-chart/
  Chart.yaml            # Helm chart metadata
  values.yaml           # Configurable values (image, ingress host, resources)
  templates/
    deployment.yaml     # Kubernetes Deployment
    services.yaml       # Kubernetes Service
    ingress.yaml        # Ingress with TLS
```

## GPU Inference

If your Gradio app needs a GPU (e.g. running a model), make two changes:

**1. `helm-chart/templates/deployment.yaml`** — replace the `nodeSelector` and add GPU limits:
```yaml
nodeSelector:
  kubernetes.io/hostname: imeca40.imec.local
```
And under `resources`:
```yaml
resources:
  limits:
    nvidia.com/gpu: "1"
    cpu: "4"
    memory: "16Gi"
```

**2. `Dockerfile`** — switch to a CUDA base image:
```dockerfile
FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04

RUN apt-get update && apt-get install -y python3 python3-pip && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 7860
CMD ["python3", "app.py"]
```
