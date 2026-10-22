# 09— Cluster Recovery After Power Loss

## Overview

When the Proxmox host loses power, all VMs restart but Kubernetes pods may
come up in broken states (CrashLoopBackOff, Unknown, OOMKilled).  The recovery
script automates the cleanup and wait sequence.

**File:** `micro_k8s/recovery/recover.sh`  
**Run from:** Your local machine (not the cluster)

---

## When to Use This Script

- After a Proxmox server reboot or power cut
- After a planned maintenance window
- When `kubectl get pods -n kubeflow` shows multiple CrashLoopBackOff or Unknown pods

---

## How It Works

The script connects to the control plane via SSH and issues kubectl commands
remotely.  All operations are non-destructive — it only deletes pods in broken
states (which Kubernetes will automatically recreate).

```bash
CONTROL_PLANE="ubuntu@10.110.188.144"
SSH_KEY="$HOME/.ssh/pfa_bouri_terraform"
KUBECTL="ssh -i $SSH_KEY $CONTROL_PLANE kubectl"
```

### Phase 0a — Wait for API Server

```bash
until kubectl cluster-info; do
    warn "API server not ready — retrying in 10s..."
    sleep 10
done
```

The API server may take 1–3 minutes after a reboot.  The script waits
indefinitely rather than giving up.

### Phase 0b — Wait for All Nodes Ready

```bash
while true; do
    not_ready=$(kubectl get nodes | grep -vc ' Ready')
    if [ "$not_ready" -eq 0 ]; then
        break
    fi
    sleep 10
done
```

Nodes must all be Ready before trying to recover workloads.

### Phase 1 — Longhorn (recovered first)

```bash
recover_namespace "longhorn-system" "Phase 1 — Longhorn (storage)"
```

**Why first?**  Kubeflow pods that need PersistentVolumes will stay in
`ContainerCreating` until the Longhorn CSI driver is healthy.  Recovering
storage before workloads prevents unnecessary timeout waits.

### Phase 2 — Kubeflow

```bash
recover_namespace "kubeflow" "Phase 2 — Kubeflow"
```

### What `recover_namespace` Does

```
1. print_status(ns)          — log current pod/sts/deploy/ds state

2. cleanup_namespace(ns)
   ├─ Force-delete Unknown pods (grace-period=0)
   └─ Force-delete CrashLoopBackOff pods

3. wait_healthy(ns)
   └─ poll count_broken(ns) every 15s until == 0
      prints details every 4th tick (60s)

4. Second pass if still broken
   (new CrashLoopBackOff may appear as volumes reattach)
```

### `count_broken()` — What Counts as Broken

```bash
# Pods
grep -cE 'Unknown|CrashLoopBackOff|OOMKilled|Error|Init:[0-9]|PodInitializing|Terminating'

# StatefulSets: ready != desired
awk '{split($2,a,"/"); if(a[1]!=a[2]) c++}'

# Deployments: available != desired
awk '{split($2,a,"/"); if(a[1]!=a[2]) c++}'

# DaemonSets: ready != desired
awk '{if($4!=$2) c++}'
```

The total of all four counts is returned.  `wait_healthy` loops until this is 0.

---

## Running the Script

```bash
chmod +x micro_k8s/recovery/recover.sh
./micro_k8s/recovery/recover.sh
```

**Prerequisites:**
- SSH key at `~/.ssh/pfa_bouri_terraform` on the local machine
- Control plane VM has finished booting (can SSH in)
- `kubectl` available on the control plane (microk8s wrapper)

---

## Expected Output

```
[10:15:32] ════ Phase 0a: Waiting for Kubernetes API server ════
[10:15:42] ✓ API server is responding.

[10:15:42] ════ Phase 0b: Waiting for all nodes to be Ready ════
[10:16:05] ✓ All 3 nodes are Ready.

[10:16:05] ════ Phase 1 — Longhorn (storage) ════
[10:16:05] State BEFORE recovery: ...
[10:16:15] ⚠  longhorn-system: 3 resource(s) not ready — waiting 15s...
[10:17:30] ✓ Phase 1 — Longhorn (storage): all resources healthy.

[10:17:30] ════ Phase 2 — Kubeflow ════
[10:17:30] State BEFORE recovery: ...
[10:17:45] ⚠  Unknown pods in kubeflow — force deleting:
[10:17:45]    → kfp-api-0
[10:19:15] ✓ Phase 2 — Kubeflow: all resources healthy.

[10:19:15] ════ Recovery complete ════
```

---

## Manual Recovery Commands

If the script doesn't cover your specific situation:

```bash
# Force-delete a specific stuck pod
kubectl delete pod <name> -n <namespace> --force --grace-period=0

# Check what's broken in a namespace
kubectl get pods -n kubeflow | grep -E 'CrashLoopBackOff|Unknown|Error'

# Restart a specific deployment
kubectl rollout restart deployment/<name> -n kubeflow

# Check persistent volume claims
kubectl get pvc -n kubeflow

# Check Longhorn volumes
kubectl get volumes -n longhorn-system
```

---

## Why Pods Break After Reboot

| Pod state | Cause | Script action |
|---|---|---|
| `Unknown` | Pod was running when node shut down; node fenced | Force delete (K8s will reschedule) |
| `CrashLoopBackOff` | Started too early, dependency not ready | Delete (K8s restarts cleanly) |
| `OOMKilled` | Node came up with memory pressure | Counted as broken; K8s restarts |
| `Init:N/M` | Init container waiting on dependency | Counted until resolved naturally |
| `Terminating` (stuck) | Volume detach failed | Force delete |

Longhorn volumes may take 30–60 seconds to fully reattach after node restart.
The two-pass cleanup handles cases where a pod restarts during volume reattach
and enters a new CrashLoopBackOff cycle.
