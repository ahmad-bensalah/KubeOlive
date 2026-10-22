#  05 — Charmed Kubeflow Deployment via Juju

## Overview

Kubeflow is deployed using **Juju** — a model-driven operator framework that
packages complex Kubernetes applications as "charms" (operators).  Juju handles
the entire lifecycle: deploy, configure, relate, upgrade.

**Role:** `roles/juju/tasks/main.yml`  
**Target:** `juju_machine` (10.110.188.147)

---

## Architecture

```
Juju VM (10.110.188.147)
├── LXD container (lxdbr0, e.g. 10.177.205.126)
│   └── Juju Controller daemon (port 17070)
│       └── manages Juju model "kubeflow"
│
└── Routes: K8s nodes → lxdbr0 subnet (via 10.110.188.147)

Kubernetes cluster
├── namespace: kubeflow   ← Juju model
│   ├── istio-pilot, istio-ingressgateway
│   ├── dex-auth          ← OIDC server
│   ├── kubeflow-profiles
│   ├── kfp-api, kfp-ui, kfp-persistence-agent
│   ├── minio
│   ├── jupyter-ui        ← notebook server manager
│   └── ... 50+ charms
```

---

## Step 1 — Install LXD and Juju

```bash
snap install lxd    --channel=latest/stable
snap install juju   --channel=3.6/stable --classic
```

**Why `--classic` for Juju?**  Juju needs to access the LXD unix socket
(`/var/snap/lxd/common/lxd/unix.socket`), the kubectl binary, SSH keys, and
various system paths.  Classic confinement removes the snap sandbox, allowing
this unrestricted access.

```bash
lxd init --auto          # creates default storage pool + lxdbr0 bridge
usermod -aG lxd ubuntu   # ubuntu user can access LXD socket
```

---

## Step 2 — Bootstrap Juju Controller

```bash
juju bootstrap localhost lxd-controller
```

This creates an LXD container on the Juju VM and installs the Juju agent daemon
inside it.  The container gets a private IP from the `lxdbr0` bridge (e.g.
`10.177.205.126`).

**Problem this creates:** Kubernetes pods need to connect to the Juju agent at
`10.177.205.126:17070` for operator lifecycle management, but Kubernetes nodes
have no route to the `10.177.205.0/24` LXD bridge subnet.

---

## Step 3 — Cross-Network Routing Fix

This is the most critical and subtle part of the deployment.

```
BEFORE:
K8s Node (10.110.188.144)  ──X──►  10.177.205.126:17070
                                    "no route to host"

AFTER:
K8s Node (10.110.188.144)  ──►  10.110.188.147 (Juju VM)  ──►  10.177.205.126
```

This route persists across reboots because it is managed by systemd.

---

## Step 4 — Register MicroK8s as a Juju Cloud

```bash
juju add-k8s kubeflow-cloud \
  --controller lxd-controller \
  --storage=longhorn      # sets Longhorn as default StorageClass for Juju
```

Juju reads `~/.kube/config` to discover the MicroK8s API server address and
certificates.  The `--storage=longhorn` flag tells Juju to use the Longhorn
StorageClass for all PVCs it creates.

---

## Step 5 — Create the Kubeflow Model

```bash
juju add-model kubeflow kubeflow-cloud
```

In Juju terminology, a "model" maps directly to a Kubernetes namespace.  This
creates the `kubeflow` namespace in the cluster.

---

## Step 6 — Deploy Charmed Kubeflow

```bash
juju deploy kubeflow --trust --channel=1.10/stable -m kubeflow
```

`--trust` is **critical**.  It grants Juju charms the RBAC permissions to
create cluster-scoped resources:
- `CustomResourceDefinitions`
- `ClusterRoles` and `ClusterRoleBindings`
- Admission webhooks

Without `--trust`, charms like `istio-pilot` and `kfp-api` fail with
`forbidden: CRD create not allowed`.

This command deploys 50+ charms including:
- Istio (service mesh + ingress)
- Dex (OIDC server)
- Kubeflow Pipelines (kfp-api, kfp-ui, kfp-persistence, kfp-schedwf)
- Kubeflow Profiles (namespace management)
- Notebooks (jupyter-ui, jupyter-controller)
- MinIO (artifact storage)
- Katib (hyperparameter tuning)
- KServe (model serving)

Deployment takes 20–40 minutes.

---

## Step 7 — Configure Dex OIDC

**Wait for Dex to become active:**
```bash
juju status -m kubeflow --format=json \
  | jq -r '.applications["dex-auth"]["application-status"].current'
# polls until: "active"
```

**Set static admin credentials (fallback login):**
```bash
juju config -m kubeflow dex-auth \
  static-username=admin \
  static-password=kubeflow-admin-2024
```

**Set the public issuer URL:**
```bash
juju config -m kubeflow dex-auth \
  issuer-url="http://10.110.188.200/dex"
```

Why this matters: Dex embeds the issuer URL in every JWT token it issues.
If Dex defaults to its internal service URL (`http://dex-auth.kubeflow.svc:5556`),
the browser-side validation fails because the token's `iss` claim doesn't match
the URL the browser is talking to.

**Apply GitLab OIDC connector:**

```bash
# Written to /home/ubuntu/ not /tmp/ — snap confinement blocks /tmp access
juju config -m kubeflow dex-auth connectors=@/home/ubuntu/dex_connectors.yaml
```

---

## Step 8 — Configure Jupyter UI

**Add Elyra image + pre-select PodDefaults:**

```bash
# Custom image list (replaces entire default list)
juju config -m kubeflow jupyter-ui \
  jupyter-images='["ahmed8603/elyra:v2",
  "charmedkubeflow/jupyter-scipy:1.10.0-0be57a5",
  ...]'

# Pre-select both PodDefaults for every new notebook
juju config -m kubeflow jupyter-ui \
  default-poddefaults='["access-ml-pipeline", "elyra-minio-access"]'
```

Why `juju config` and not `kubectl patch`:  The `jupyter-ui` charm writes
`spawner_ui_config.yaml` into the pod via an EmptyDir volume on every
reconciliation.  Any direct ConfigMap or file edit gets overwritten.  The only
persistent way to change charm-managed configuration is through Juju.

---

## Verifying the Deployment

```bash
# Check all charms are active
juju status -m kubeflow

# Kubeflow dashboard
curl -I http://10.110.188.200

# Check Dex is serving OIDC discovery
curl http://10.110.188.200/dex/.well-known/openid-configuration

# Check all pods
kubectl get pods -n kubeflow
```

---

## Key Design Decisions

| Decision | Reason |
|---|---|
| Juju 3.6/stable | Compatible with Kubeflow 1.10/stable bundle |
| LXD for controller | Lightweight container isolation, no extra VM needed |
| Routing via iptables | No BGP or overlay network required for LXD→K8s communication |
| Plain IP for dashboard URL | nip.io cookies blocked by Chrome/Safari public suffix list |
| `--trust` flag | Required for cluster-scoped CRD and RBAC creation |
| `juju config` for spawner | Charm overwrites manual edits; config is the only durable change point |
