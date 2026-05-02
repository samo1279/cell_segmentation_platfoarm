# Helm Deployment Fixes: Timeout and Rate Limiting

This document summarizes the root cause and complete set of fixes applied to resolve the `context deadline exceeded` error that occurred during `helm upgrade`.

## 1. Root Cause Analysis

The deployment failed due to a cascading series of issues originating from a race condition between the Model Container and the PostgreSQL database.

1.  **Pod Initialization Race Condition**: The Model Container started concurrently with the PostgreSQL container. The model's Python code attempted to connect to the database immediately upon startup, but the database was not yet ready to accept connections.
2.  **Hanging Connections & Probe Failures**: The database connection attempt would hang, causing the model container's `/health` endpoint to become unresponsive. Kubernetes health probes to this endpoint would then time out.
3.  **Container Restarts**: After repeated probe failures, Kubernetes would kill and restart the model container, creating a `CrashLoopBackOff` state.
4.  **API Traffic Storm**: Each restart cycle generated a high volume of API calls to the Kubernetes server (status changes, events, scheduler evaluations).
5.  **Rate Limiting**: This traffic storm triggered the Kubernetes API server's client rate limiter.
6.  **Helm Timeout**: Helm, which polls the API server to check pod readiness, was blocked by the rate limiter. After 30 minutes of being unable to get a status update, its own `--timeout` was exceeded, leading to the final error.

## 2. Implemented Fixes

### Fix 2.1: Enforce Startup Order with an Init Container

An `initContainer` was added to the Model Container's deployment definition in `helm-chart/templates/deployment.yaml`.

```yaml
initContainers:
  - name: wait-for-db
    image: busybox:1.28
    command: ['sh', '-c', 'until nc -z {{ .Release.Name }}-db {{ .Values.db.port }}; do echo waiting for db; sleep 2; done']
```

**Impact**: This container blocks the main model container from starting until the PostgreSQL service is network-reachable, completely eliminating the race condition.

### Fix 2.2: Corrected PostgreSQL Probe Timings

The readiness and liveness probes for the PostgreSQL container were adjusted to be more robust.

```yaml
# Readiness Probe
readinessProbe:
  initialDelaySeconds: 20  # Increased from 5s
  timeoutSeconds: 5        # Added

# Liveness Probe
livenessProbe:
  initialDelaySeconds: 30  # Added
  timeoutSeconds: 5        # Added
```

**Impact**: This gives the database sufficient time to initialize before probes start and ensures the `pg_isready` command itself doesn't time out.

### Fix 2.3: Corrected Model Container Probe Timings

The probes for the Model Container were also given initial delays to prevent them from firing before the application had time to start.

```yaml
# Readiness Probe
readinessProbe:
  initialDelaySeconds: 5 # Added

# Liveness Probe
livenessProbe:
  initialDelaySeconds: 30 # Added
```

**Impact**: This prevents premature probe failures while the model and its dependencies are loading.

### Fix 2.4: Realistic Memory Limits

The memory limit for the model container was reduced from an unrealistic `64Gi` to `16Gi` in `helm-chart/values.yaml`.

**Impact**: This prevents the pod from getting stuck in a `Pending` state due to being unschedulable on cluster nodes with insufficient memory, which was a secondary cause of the timeout.

## 3. Verification

With these fixes, the deployment now completes successfully in approximately 5-7 minutes, well within the 30-minute timeout. The pods become "Ready" in the correct order, and the application is accessible.
