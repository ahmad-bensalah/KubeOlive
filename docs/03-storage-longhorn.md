#  03 — Longhorn Distributed Block Storage

## Overview

Longhorn provides persistent storage for the Kubernetes cluster.  Every
Kubeflow component that needs a PersistentVolume (MinIO, MySQL, Kubeflow
Pipelines metadata) relies on Longhorn to provision and manage the actual disk.

**Role:** `roles/longhorn/tasks/main.yml`  
**Target:** All K8s nodes (installs iSCSI), master only (deploys Longhorn)

---

## Why Longhorn?

| Need | How Longhorn addresses it |
|---|---|
| Dynamic PV provisioning | StorageClass `longhorn` — no manual disk management |
| Data replication | Configurable replicas per volume (set to 1 for dev) |
| Volume expansion | `allowVolumeExpansion: true` on the StorageClass |
| GUI | Dashboard at `http://<worker-ip>:30080` |
| RWX volumes | Supported via iSCSI target daemon (`tgt`) |

---

## Step 1 — iSCSI on Every Node

Longhorn uses iSCSI to attach block devices across nodes.  Every node must have
the iSCSI initiator (`open-iscsi`) and target daemon (`tgt`).

```yaml
- name: Install open-iscsi, tgt, nfs-common, util-linux
  apt:
    name: [open-iscsi, tgt, nfs-common, util-linux]

- name: Enable and start iscsid
  systemd: { name: iscsid, enabled: true, state: started }

- name: Enable and start tgt
  systemd: { name: tgt, enabled: true, state: started }
```

**Why `tgt` specifically?**  Longhorn's RWX (ReadWriteMany) volume feature
uses `tgt` as the local iSCSI target daemon.  Without it, the `longhorn-manager`
pods crash-loop with "tgt not running" errors during RWX volume creation.

---

## Step 2 — Helm Install (master only)

```bash
# Add repo
helm repo add longhorn https://charts.longhorn.io
helm repo update

# Create namespace
kubectl create namespace longhorn-system

# Install
helm upgrade --install longhorn longhorn/longhorn \
  --namespace longhorn-system \
  --set defaultSettings.defaultReplicaCount=1 \
  --set defaultSettings.storageMinimalAvailablePercentage=10 \
  --set defaultSettings.defaultDataPath=/var/lib/longhorn \
  --set persistence.defaultClassReplicaCount=1 \
  --set service.ui.type=NodePort \
  --set service.ui.nodePort=30080 \
  --set csi.kubeletRootDir="/var/snap/microk8s/common/var/lib/kubelet" \
  --timeout 10m \
  --wait
```

**Key settings explained:**

| Setting | Value | Reason |
|---|---|---|
| `defaultReplicaCount` | 1 | Development cluster — 1 replica saves disk space |
| `storageMinimalAvailablePercentage` | 10% | Longhorn won't create volumes if <10% free |
| `defaultDataPath` | `/var/lib/longhorn` | Where Longhorn stores volume data on each node |
| `service.ui.nodePort` | 30080 | Fixed port for the dashboard |
| `csi.kubeletRootDir` | MicroK8s snap path | Overrides the standard `/var/lib/kubelet` — critical for MicroK8s |

The `csi.kubeletRootDir` setting is the most important MicroK8s-specific change.
MicroK8s stores kubelet state under `/var/snap/microk8s/common/var/lib/kubelet`
instead of the standard path.  Without this override, the Longhorn CSI driver
cannot find the kubelet socket and volumes never attach.

---

## Step 3 — Make Longhorn the Default StorageClass

```bash
# Remove default annotation from any existing StorageClass
kubectl get sc -o name | xargs -I{} kubectl annotate {} \
  storageclass.kubernetes.io/is-default-class=false --overwrite

# Set Longhorn as default with volume expansion enabled
kubectl patch storageclass longhorn -p '{
  "metadata": {
    "annotations": {"storageclass.kubernetes.io/is-default-class":"true"}
  },
  "allowVolumeExpansion": true
}'
```

Any PVC that does not specify a StorageClass will automatically use Longhorn.
Kubeflow's components (MinIO, MySQL) use the default class.

---

## Step 4 — Verify

```bash
kubectl rollout status daemonset/longhorn-manager -n longhorn-system --timeout=5m
```

Longhorn manager is a DaemonSet — one pod per node.  The rollout status command
blocks until all pods are ready.

---

## Accessing the Dashboard

```
http://10.110.188.145:30080   (any worker node IP)
```

The dashboard shows all volumes, their replication status, and node disk usage.

---

## Common Issues

| Symptom | Cause | Fix |
|---|---|---|
| `longhorn-manager` CrashLoopBackOff | `tgt` not running | `systemctl start tgt` on affected node |
| Volume stuck in Pending | No StorageClass default | Check `kubectl get sc` |
| PVC stuck in Pending | Wrong kubelet path | Reinstall with correct `csi.kubeletRootDir` |
| Insufficient storage warning | <10% free on a node | Free disk space or adjust `storageMinimalAvailablePercentage` |
