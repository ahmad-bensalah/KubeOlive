# 11 — Per-User MinIO Isolation

## Overview

Each Kubeflow user gets a **dedicated MinIO instance** running inside their own
namespace. This provides full storage isolation — no user can access another
user's data. The Admin Portal provisions the MinIO deployment automatically
when a user is created.

---

## Architecture

```
User namespace (e.g. "abid")
  ├── Deployment: minio          ← dedicated MinIO instance
  ├── Service: minio             ← ports 9000 (API) + 9001 (Console)
  ├── PVC: minio-pvc             ← 10Gi Longhorn volume (persists data)
  ├── Secret: mlpipeline-minio-artifact  ← credentials (accesskey / secretkey)
  └── PodDefault: elyra-minio-access     ← injects MinIO env vars into notebooks

kubeflow namespace
  └── VirtualService: minio-console-<username>  ← exposes Console at /minio/<username>/
```

The per-user MinIO reads its root credentials from `mlpipeline-minio-artifact`
in its own namespace via `secretKeyRef` — it does **not** use the shared MinIO
in the `kubeflow` namespace.

---

## How credentials flow

1. Kubeflow's `kfp-profile-controller` (Metacontroller) writes
   `mlpipeline-minio-artifact` into every new user namespace automatically.
2. The per-user MinIO pod starts and reads `MINIO_ROOT_USER` /
   `MINIO_ROOT_PASSWORD` from that secret.
3. The Elyra PodDefault injects the same secret values as env vars into
   notebook pods at startup time.
4. Notebooks connect to `http://minio:9000` (the in-namespace service) using
   those credentials — no cross-namespace traffic.

---

## The credential rotation problem

By default, `kfp-profile-controller` has `secrets` in its `DecoratorController`
attachment list. On every Kubeflow restart it **regenerates** random credentials
in `mlpipeline-minio-artifact`, which:

- Breaks the per-user MinIO pod (it restarts with new creds from the secret)
- Breaks running notebook pods (they have old creds baked in from PodDefault
  injection at startup)

---

## Fix — prevent credential rotation

Remove `secrets` from the DecoratorController attachment list so
`kfp-profile-controller` never touches `mlpipeline-minio-artifact` again.

### Apply the fix

On the master node:

```bash
microk8s kubectl get decoratorcontroller \
  kubeflow-pipelines-profile-controller -o json | \
python3 -c "
import sys, json, subprocess
dc = json.load(sys.stdin)
dc['spec']['attachments'] = [
  a for a in dc['spec'].get('attachments', [])
  if a.get('resource') != 'secrets'
]
proc = subprocess.run(
  ['microk8s', 'kubectl', 'apply', '-f', '-'],
  input=json.dumps(dc), capture_output=True, text=True
)
print(proc.stdout or proc.stderr)
"
```

### Verify the fix

```bash
microk8s kubectl get decoratorcontroller \
  kubeflow-pipelines-profile-controller \
  -o jsonpath='{.spec.attachments}' | python3 -m json.tool
```

The output must **not** contain an entry with `"resource": "secrets"`.

### Confirm credentials are stable after restart

```bash
# Record current accesskey
microk8s kubectl get secret mlpipeline-minio-artifact \
  -n <username> -o jsonpath='{.data.accesskey}' | base64 -d

# Simulate a controller restart
microk8s kubectl rollout restart deployment kfp-profile-controller -n kubeflow
microk8s kubectl rollout status deployment kfp-profile-controller -n kubeflow

# Check accesskey is unchanged
microk8s kubectl get secret mlpipeline-minio-artifact \
  -n <username> -o jsonpath='{.data.accesskey}' | base64 -d
```

---

## Automation

This fix is applied automatically in two places:

| Where | When |
|---|---|
| `roles/juju/tasks/main.yml` | Fresh cluster — applied right after Kubeflow is deployed |
| `app/main.py` startup hook | Applied every time the Admin Portal restarts |

---

## Recovery — resync after a rotation event

If credentials were already rotated before the fix was applied, resync all
per-user MinIO pods so they restart and read the current secret:

```bash
for ns in $(microk8s kubectl get namespaces \
  -o jsonpath='{.items[*].metadata.name}' | tr ' ' '\n' | \
  grep -v -E '^(kubeflow|kube-system|kube-public|kube-node-lease|istio-system|auth|cert-manager|metallb-system|default)'); do
  if microk8s kubectl get deployment minio -n $ns >/dev/null 2>&1; then
    echo "Restarting MinIO in $ns"
    microk8s kubectl rollout restart deployment/minio -n $ns
  fi
done
```

Then restart the affected notebook pods so PodDefault re-injects the current
credentials:

```bash
# Delete the notebook pod — it will be recreated automatically
microk8s kubectl delete pod -n <username> <notebook-pod-name>
```

---

## Check per-user MinIO status

```bash
# List all user MinIO deployments
microk8s kubectl get deployment -A -l app=minio

# Check a specific user's MinIO
microk8s kubectl get pods -n <username> | grep minio
microk8s kubectl logs -n <username> deployment/minio --tail=20

# Check PVC is bound
microk8s kubectl get pvc -n <username>

# Check current credentials in namespace
microk8s kubectl get secret mlpipeline-minio-artifact \
  -n <username> -o jsonpath='{.data.accesskey}' | base64 -d
```

---

## Access MinIO Console

Each user's MinIO Console is accessible through the Kubeflow dashboard
sidebar under **MinIO Storage**, routed via Istio at:

```
http://<kubeflow-dashboard-ip>/minio/<username>/
```

Or directly via port-forward for debugging:

```bash
microk8s kubectl port-forward -n <username> svc/minio 9001:9001
# then open http://localhost:9001
```

---

## Isolation guarantee

| Isolated | Not isolated |
|---|---|
| Storage (separate PVC per user) | MinIO version (same image) |
| Credentials (separate secret per namespace) | Node resources (shared cluster) |
| Network access (in-namespace service only) | Longhorn storage backend (shared) |
| Console UI (separate VirtualService per user) | |

Users cannot reach each other's MinIO — the in-namespace `minio` service only
routes to the pod in that namespace. There is no cross-namespace MinIO access.
