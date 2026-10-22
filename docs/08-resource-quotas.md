#  08 — Resource Quota Groups

## Overview

Resource Quota Groups let administrators define reusable CPU/Memory/Storage/GPU
limits that can be assigned to users.  When a user is assigned to a group, a
Kubernetes `ResourceQuota` object is automatically created in their namespace,
limiting the total resources they can consume across all their workloads.

---

## Concept

### What a Quota Group Defines

```
admin sets in portal → what K8s enforces
─────────────────────────────────────────
cpu_limit:    10    → limits.cpu: 16  (10 + 6 Istio overhead)
memory_limit: 16Gi  → limits.memory: 16Gi
storage_limit: 50Gi → requests.storage: 50Gi
gpu_limit:    2    
```

### The Istio Overhead Problem

Kubeflow injects Istio sidecars (`istio-proxy`) into every pod in a user
namespace.  These sidecars have `limits.cpu: 2` each.  With the Kubeflow
system pods (`ml-pipeline-ui-artifact`, `ml-pipeline-visualizationserver`)
that run in every user namespace, this consumes approximately **4–6 cores**
before any notebook starts.

Without accounting for this, a user with a "10 core" quota immediately has only
4 cores available for their actual work.

**The fix (`services/quota_sync.py`):**
```python
ISTIO_CPU_OVERHEAD = 6.0

def _build_manifest(namespace, group):
    effective_cpu = group["cpu_limit"] + ISTIO_CPU_OVERHEAD
    hard = {
        "limits.cpu":       str(effective_cpu),  # transparent to admin
        "limits.memory":    group["memory_limit"],
        "requests.storage": group["storage_limit"],
    }
```

| Admin sets | K8s enforces | Available for notebooks |
|---|---|---|
| 5 cores | 11 cores | ~5 cores |
| 10 cores | 16 cores | ~10 cores |
| 20 cores | 26 cores | ~20 cores |

---

## ResourceQuota Manifest

When applied to a user's namespace, it looks like this:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: portal-quota
  namespace: alice
  labels:
    managed-by: kubeflow-admin-portal
spec:
  hard:
    limits.cpu: "16"          # cpu_limit(10) + ISTIO_CPU_OVERHEAD(6)
    limits.memory: "16Gi"
    requests.storage: "50Gi"
```

---

## Automatic Synchronisation

Kubernetes is kept in sync automatically whenever the database changes:

| Trigger | What happens |
|---|---|
| User created with quota group | `sync_user_quota(username, group)` queued as background task |
| User quota group changed (PATCH) | `sync_user_quota(username, new_group)` queued |
| Quota group updated (PUT) | All READY users in that group re-synced |
| Quota group deleted (DELETE) | All users in group revert to NULL, quotas deleted from K8s |

`sync_user_quota` is idempotent:
```python
def sync_user_quota(username, group):
    if group:
        apply_resource_quota(username, group)   # create or update
    else:
        delete_resource_quota(username)          # remove quota
```

---

## Manual Reconciliation

The **Sync Kubernetes** button in the portal runs `reconcile_all()`:

```
For each READY user in DB:
  ├─ has group + no live quota   → apply   → report[created]
  ├─ has group + quota outdated  → apply   → report[updated]
  ├─ has group + quota matches   → skip
  ├─ no group + live quota       → delete  → report[deleted]
  └─ no group + no quota         → skip
```

**Return value:**
```json
{
  "status": "success",
  "report": {
    "created": ["alice"],
    "updated": ["bob"],
    "deleted": ["charlie"],
    "errors":  []
  }
}
```


---

## Validation Rules

| Field | Rule |
|---|---|
| `cpu_limit` | Must be > 0 |
| `memory_limit` | Must match `^\d+(\.\d+)?Gi$` (case-insensitive — `16GI` is normalised to `16Gi`) |
| `storage_limit` | Same format as memory |
| `gpu_limit` | Must be ≥ 0 |

The frontend normalises input before sending:
```javascript
payload.memory_limit  = payload.memory_limit.replace(/gi$/i, 'Gi');
payload.storage_limit = payload.storage_limit.replace(/gi$/i, 'Gi');
```

---

## Verifying in Kubernetes

```bash
# List all ResourceQuotas
kubectl get resourcequota --all-namespaces

# Inspect a specific user's quota (shows used vs hard)
kubectl describe resourcequota portal-quota -n alice

# Example output:
# Resource           Used    Hard
# --------           ----    ----
# limits.cpu         1       16
# limits.memory      4Gi     16Gi
# requests.storage   10Gi    50Gi
```

---

## What "exceeded quota" means

When a pod is blocked by the quota:
```
pods "notebook-0" is forbidden: exceeded quota: portal-quota,
requested: limits.cpu=10400m, used: limits.cpu=4600m, limited: limits.cpu=10
```

Reading this:
- `used: 4600m` — Istio sidecars are consuming 4.6 cores
- `requested: 10400m` — the new notebook requests 10.4 cores
- `4600 + 10400 = 15000m` exceeds `limited: 10000m`

**Resolution options:**
1. Raise the quota group's `cpu_limit` in the portal
2. Stop other running notebooks to free up the used quota
3. Move user to Unlimited
