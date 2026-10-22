# Kubeflow Multi-User Admin Portal — Full Walkthrough

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Deployment](#3-deployment)
4. [Codebase Structure](#4-codebase-structure)
5. [Startup Sequence](#5-startup-sequence)
6. [Feature: User Provisioning](#6-feature-user-provisioning)
7. [Feature: Resource Quota Groups](#7-feature-resource-quota-groups)
8. [Feature: Quota Reassignment](#8-feature-quota-reassignment)
9. [Feature: User Deletion](#9-feature-user-deletion)
10. [Feature: Health Check](#10-feature-health-check)
11. [Database Schema](#11-database-schema)
12. [API Reference](#12-api-reference)
13. [Kubernetes Objects Managed](#13-kubernetes-objects-managed)
14. [How kubectl Is Used](#14-how-kubectl-is-used)
15. [Frontend Architecture](#15-frontend-architecture)
16. [Verifying State in Kubernetes](#16-verifying-state-in-kubernetes)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Overview

The Admin Portal is a **FastAPI web application** running on the GitLab VM (`10.110.188.148:7070`).  
It gives platform administrators a single UI to:

- Provision new MLOps users (GitLab account + Kubeflow namespace)
- Assign resource limits (CPU / Memory / Storage / GPU) to users via quota groups
- Monitor GitLab, Kubernetes, and Longhorn connectivity
- Decommission users and clean up all their Kubernetes resources

The portal sits at the intersection of three systems:

```
Admin Browser
     │
     ▼
Admin Portal (FastAPI — port 7070)
     │
     ├──► GitLab API          (create / delete users)
     ├──► Kubernetes API      (create Profile, ResourceQuota, PodDefault, Secret)
     └──► SQLite DB           (local state, passwords, quota assignments)
```

---

## 2. Architecture

### Why on the GitLab VM?

The portal needs simultaneous access to:
- The **GitLab Rails API** (HTTP to `localhost`) — for user account management
- The **Kubernetes API** (`10.110.188.144:6443`) — via `kubectl` + kubeconfig

The GitLab VM already has network connectivity to both, so it was the natural host.

### Technology Stack

| Layer      | Technology                        |
|------------|-----------------------------------|
| Backend    | Python 3, FastAPI, Uvicorn        |
| Database   | SQLite (file at `/var/lib/portal-data/portal.db`) |
| K8s access | `kubectl` binary via subprocess   |
| GitLab     | REST API via `requests`           |
| Frontend   | Vanilla HTML/CSS/JS, Lucide icons |
| Process    | systemd service (`kubeflow-admin-portal`) |

---

## 3. Deployment

### Systemd Service

```ini
[Service]
User=root
WorkingDirectory=/home/ubuntu/admin-portal
ExecStart=/home/ubuntu/admin-portal/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 7070
Environment=GITLAB_TOKEN=<token>
Environment=KUBECONFIG=/root/.kube/config
```

The service runs as `root` so `kubectl` has unrestricted access to all namespaces.

### Key Paths on the VM

```
/home/ubuntu/admin-portal/
├── app/                  ← Python package (backend)
│   ├── main.py           ← FastAPI app entry point
│   ├── config.py         ← env vars, constants
│   ├── database.py       ← SQLite init, migrations, helpers
│   ├── gitlab_client.py  ← GitLab REST wrapper
│   ├── kubernetes.py     ← kubectl interactions (Profile, MinIO, Elyra)
│   ├── routes/
│   │   ├── users.py      ← /api/users endpoints
│   │   ├── quota_groups.py ← /api/quota-groups endpoints
│   │   └── health.py     ← /api/health endpoint
│   └── services/
│       └── quota_sync.py ← ResourceQuota CRUD + reconciliation
├── index.html            ← Single-page frontend
├── static/               ← favicon, logo
├── venv/                 ← Python virtual environment
└── requirements.txt

/var/lib/portal-data/
└── portal.db             ← SQLite database

/root/.kube/
└── config                ← Kubeconfig pointing to 10.110.188.144:6443
```

---

## 4. Codebase Structure

### `app/config.py`
Central configuration. Reads environment variables at import time.

```python
DB_PATH      = "/var/lib/portal-data/portal.db"
GITLAB_URL   = "http://localhost"
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "")
```

### `app/database.py`
Owns the SQLite schema and runs idempotent migrations on every startup.

- `init_db()` — called at startup; creates tables and adds missing columns
- `get_db()` — context manager yielding `(conn, cursor)`, auto-commits/rolls back
- `update_profile_status(username, status)` — called from background tasks

### `app/gitlab_client.py`
A single function `gl_api(method, path, data)` wraps all GitLab REST calls.  
Returns `(body_dict, http_status_code)` always — never raises on HTTP errors.

```python
gl_api("POST", "/users", {...})   # create
gl_api("DELETE", "/users/42?hard_delete=true")  # delete
gl_api("GET", "/users?per_page=100")  # list
```

### `app/kubernetes.py`
All `kubectl` calls for Kubeflow-specific resources:

| Function | kubectl command | Purpose |
|---|---|---|
| `create_kubeflow_profile` | `kubectl apply -f -` | Creates a `Profile` CRD |
| `delete_kubeflow_profile` | `kubectl delete profile` | Removes the Profile |
| `delete_namespace` | `kubectl delete namespace` | Force-removes the namespace |
| `replicate_minio_secret_and_verify` | `kubectl get/apply` | Copies MinIO secret to user namespace |
| `apply_elyra_poddefault` | `kubectl apply -f -` | Creates Elyra PodDefault |
| `_wait_for_namespace` | `kubectl get namespace` (polls) | Waits up to 30s for namespace |

### `app/services/quota_sync.py`
Reusable ResourceQuota service.

| Function | Description |
|---|---|
| `_build_manifest(namespace, group)` | Renders a ResourceQuota YAML string |
| `apply_resource_quota(namespace, group)` | `kubectl apply -f -` with the manifest |
| `delete_resource_quota(namespace)` | `kubectl delete resourcequota portal-quota -n <ns>` |
| `get_resource_quota(namespace)` | `kubectl get resourcequota -o json` |
| `quota_matches_group(namespace, group)` | Compares live K8s state to DB — avoids redundant applies |
| `sync_user_quota(username, group)` | Apply if group set, delete if None |
| `reconcile_all()` | Full DB↔cluster reconciliation, returns a report |

---

## 5. Startup Sequence

```
systemd starts uvicorn
        │
        ▼
app/main.py loaded
        │
        ├── FastAPI app created
        ├── /static mounted
        ├── Routers registered (users, quota_groups, health)
        │
        ▼
@app.on_event("startup")
        │
        ├── Check GITLAB_TOKEN is set (log warning if not)
        └── init_db()
                ├── Create quota_groups table (if not exists)
                ├── Create users table (if not exists)
                └── ALTER TABLE users ADD COLUMN quota_group_id (if not exists)
                    ← migration is idempotent, existing data is preserved
```

---

## 6. Feature: User Provisioning

### What happens when you click "Provision Identity & Namespace"

```
Browser  POST /api/users
  │  { name, username, email, password, quota_group_id? }
  │
  ▼ routes/users.py → create_user()
  │
  ├─ 1. Sanitise username
  │      keep only [a-z0-9-]  →  K8s namespace-safe
  │
  ├─ 2. Validate quota_group_id (if provided)
  │      SELECT from quota_groups — 404 if not found
  │
  ├─ 3. Duplicate check
  │      SELECT from users WHERE username=? OR email=?
  │      → 400 if already exists
  │
  ├─ 4. Create GitLab user
  │      gl_api("POST", "/users", {..., skip_confirmation: true})
  │      → returns gitlab_id
  │      → on failure: 400/409 with GitLab's error message
  │
  ├─ 5. Insert into SQLite
  │      INSERT INTO users (gitlab_id, name, username, email,
  │                         password, profile_status="PROVISIONING",
  │                         quota_group_id)
  │      → on failure: DELETE GitLab user (rollback), raise 500
  │
  ├─ 6. Create Kubeflow Profile
  │      kubectl apply -f - (Profile YAML)
  │      → on failure: update profile_status="PROFILE_CREATION_FAILED"
  │
  └─ 7. Queue background tasks (returns 200 immediately)
         ├─ replicate_minio_secret_and_verify(username)
         └─ sync_user_quota(username, quota_group)  ← if group assigned
```

### Background task: MinIO + Elyra setup

```
replicate_minio_secret_and_verify(username)
  │
  ├─ Poll: kubectl get namespace <username>  (up to 30s, every 1s)
  │        → NAMESPACE_TIMEOUT if not ready
  │
  ├─ kubectl get secret mlpipeline-minio-artifact -n kubeflow -o json
  │        → MINIO_SECRET_FETCH_FAILED if error
  │
  ├─ Patch secret metadata (new namespace, remove resourceVersion/uid)
  │
  ├─ kubectl apply -f -  (secret in user's namespace)
  │        → MINIO_SECRET_APPLY_FAILED if error
  │
  ├─ UPDATE users SET profile_status="READY"
  │
  └─ apply_elyra_poddefault(username)
         kubectl apply -f - (PodDefault YAML)
         → injects ELYRA_COS_* env vars into notebook pods
```

### Profile Status Values

| Status | Meaning |
|---|---|
| `PROVISIONING` | User created in GitLab + DB, K8s work in progress |
| `READY` | Namespace up, MinIO secret replicated, fully operational |
| `PROFILE_CREATION_FAILED` | `kubectl apply Profile` failed |
| `NAMESPACE_TIMEOUT` | Namespace didn't appear within 30s |
| `MINIO_SECRET_FETCH_FAILED` | Couldn't read secret from `kubeflow` namespace |
| `MINIO_SECRET_APPLY_FAILED` | Couldn't write secret to user namespace |
| `REPLICATION_ERROR` | Unexpected exception during replication |

---

## 7. Feature: Resource Quota Groups

### Concept

A **Quota Group** defines a set of Kubernetes resource limits:

```
cpu_limit     → usable notebook cores (e.g. 10 cores)
memory_limit  → limits.memory         (e.g. 16Gi)
storage_limit → requests.storage      (e.g. 50Gi)
gpu_limit     → requests/limits.nvidia.com/gpu  (e.g. 2)
```

### Istio Sidecar Overhead

Kubeflow automatically injects an **Istio proxy sidecar** (`istio-proxy`) into every pod in a user namespace. This includes Kubeflow system pods like `ml-pipeline-ui-artifact` and `ml-pipeline-visualizationserver`, each with a `limits.cpu: 2` sidecar.

With 2–3 system pods running at all times, this consumes **~4–6 cores** before any notebook starts — making it impossible for users to use their full notebook budget.

**The fix:** `quota_sync.py` defines `ISTIO_CPU_OVERHEAD = 6.0`. When applying a ResourceQuota to Kubernetes, this value is silently added to `cpu_limit`:

```
K8s limits.cpu = cpu_limit (what admin sets) + ISTIO_CPU_OVERHEAD (6 cores)
```

This means:
- The admin sets `cpu_limit: 10` in the quota group → meaning "10 cores for notebooks"
- Kubernetes receives `limits.cpu: 16` → absorbing the 6-core Istio overhead transparently
- The user effectively has 10 cores for their actual workloads

| Admin sets | K8s enforces | Available for notebooks |
|---|---|---|
| 5 cores | 11 cores | ~5 cores |
| 10 cores | 16 cores | ~10 cores |
| 20 cores | 26 cores | ~20 cores |

When a user is assigned to a group, the portal creates this object in their namespace:

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: portal-quota
  namespace: <username>
  labels:
    managed-by: kubeflow-admin-portal
spec:
  hard:
    limits.cpu: "16"        # cpu_limit(10) + ISTIO_CPU_OVERHEAD(6)
    limits.memory: "16Gi"
    requests.storage: "50Gi"
    requests.nvidia.com/gpu: "2"   # only if gpu_limit > 0
    limits.nvidia.com/gpu: "2"
```

Users with **no quota group** (NULL) are unlimited — no ResourceQuota object exists in their namespace.

### CRUD Flow

#### Create Group
```
POST /api/quota-groups
  │  { name, description, cpu_limit, memory_limit, storage_limit, gpu_limit }
  │
  ├─ Validation (Pydantic)
  │   cpu_limit > 0
  │   memory_limit matches ^\d+(\.\d+)?Gi$  (case-normalised)
  │   storage_limit matches ^\d+(\.\d+)?Gi$
  │   gpu_limit >= 0
  │
  └─ INSERT INTO quota_groups
```

#### Update Group (triggers automatic K8s sync)
```
PUT /api/quota-groups/{id}
  │
  ├─ UPDATE quota_groups SET ...
  ├─ SELECT all READY users assigned to this group
  └─ For each user → background_tasks.add_task(sync_user_quota, username, group)
                          │
                          └─ apply_resource_quota(username, group)
                                 │
                                 └─ kubectl apply -f - (ResourceQuota manifest)
```

#### Delete Group
```
DELETE /api/quota-groups/{id}
  │
  ├─ Collect affected usernames before deletion
  ├─ DELETE FROM quota_groups (FK ON DELETE SET NULL → users.quota_group_id → NULL)
  └─ For each user → sync_user_quota(username, None)
                          │
                          └─ delete_resource_quota(username)
                                 │
                                 └─ kubectl delete resourcequota portal-quota -n <username>
```

#### Duplicate Group
```
POST /api/quota-groups/{id}/duplicate
  │
  └─ INSERT with name "<original> (copy)"
     Auto-increments if name already exists
```

### Manual Sync (Repair Tool)

```
POST /api/quota-groups/sync  →  reconcile_all()
  │
  ├─ SELECT all READY users with their quota_group
  │
  └─ For each user:
       ├─ has group + no live quota   → apply_resource_quota   → report[created]
       ├─ has group + quota outdated  → apply_resource_quota   → report[updated]
       ├─ has group + quota matches   → skip
       ├─ no group + live quota exists → delete_resource_quota → report[deleted]
       └─ no group + no quota         → skip
```

Returns:
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

## 8. Feature: Quota Reassignment

After a user is provisioned, you can change their quota group at any time.

```
Click pencil icon next to user's quota badge
  │
  ▼
Reassign modal opens (pre-selected to current group)
  │
  ▼
Select new group (or Unlimited)  →  click Apply
  │
  ▼
PATCH /api/users/{username}/quota-group
  { quota_group_id: 3 }   ← or null for Unlimited
  │
  ├─ Validate group exists (404 if not)
  ├─ UPDATE users SET quota_group_id = ?
  └─ background_tasks.add_task(sync_user_quota, username, group)
         │
         ├─ group set  → apply_resource_quota  (create or update in K8s)
         └─ group None → delete_resource_quota (remove from K8s)
```

The sync is **idempotent** — running it twice has the same result as running it once.

---

## 9. Feature: User Deletion

```
Click trash icon  →  type username to confirm  →  click Delete User
  │
  ▼
DELETE /api/users/{gitlab_id}/{username}
  │
  ├─ 1. gl_api DELETE /users/{id}?hard_delete=true
  │      (hard_delete permanently removes user data from GitLab)
  │      → 204 or 404 = success, anything else = error
  │
  ├─ 2. kubectl delete profile <username>
  │      (Profile controller cascades: deletes namespace + RBAC)
  │
  ├─ 3. kubectl delete namespace <username> --wait=false
  │      (belt-and-suspenders in case Profile is stuck)
  │      NOTE: ResourceQuota is inside the namespace, so it's gone too
  │
  └─ 4. DELETE FROM users WHERE gitlab_id = ?
```

---

## 10. Feature: Health Check

```
GET /api/health  (polled every 15s by the frontend)
  │
  ├─ kubectl version --client
  │    returncode == 0  →  kubernetes_reachable: true
  │
  ├─ gl_api GET /users?per_page=1
  │    status == 200    →  gitlab_reachable: true
  │
  └─ TCP connect to 10.110.188.144:30080 (timeout 2s)
       success          →  longhorn_reachable: true
```

---

## 11. Database Schema

```sql
-- Quota group definitions
CREATE TABLE quota_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    description     TEXT,
    cpu_limit       REAL    NOT NULL,      -- cores, e.g. 4.0
    memory_limit    TEXT    NOT NULL,      -- e.g. "16Gi"
    storage_limit   TEXT    NOT NULL,      -- e.g. "50Gi"
    gpu_limit       INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Provisioned users
CREATE TABLE users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    gitlab_id       INTEGER UNIQUE,
    name            TEXT,
    username        TEXT UNIQUE,           -- also the K8s namespace name
    email           TEXT UNIQUE,           -- OIDC identifier in Kubeflow
    password        TEXT,                  -- stored in plaintext (admin use only)
    profile_status  TEXT,                  -- see status values above
    quota_group_id  INTEGER                -- NULL = Unlimited
        REFERENCES quota_groups(id) ON DELETE SET NULL,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Migration note:** `quota_group_id` was added via `ALTER TABLE` — existing users default to `NULL` (Unlimited) automatically. The migration runs on every startup and is idempotent.

---

## 12. API Reference

### Users

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/users` | List all portal users (cross-referenced with GitLab) |
| `POST` | `/api/users` | Provision a new user |
| `PATCH` | `/api/users/{username}/quota-group` | Reassign quota group |
| `DELETE` | `/api/users/{gitlab_id}/{username}` | Permanently delete user |

### Quota Groups

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/quota-groups` | List all groups |
| `GET` | `/api/quota-groups/{id}` | Get one group |
| `POST` | `/api/quota-groups` | Create a group |
| `PUT` | `/api/quota-groups/{id}` | Update a group (triggers K8s sync) |
| `DELETE` | `/api/quota-groups/{id}` | Delete a group (triggers K8s cleanup) |
| `POST` | `/api/quota-groups/{id}/duplicate` | Clone a group |
| `POST` | `/api/quota-groups/sync` | Manual full reconciliation |
| `GET` | `/api/quota-groups/stats/overview` | Aggregate stats |

### System

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Connectivity check |
| `GET` | `/` | Serve `index.html` |

---

## 13. Kubernetes Objects Managed

For each provisioned user, the portal manages these objects in the user's namespace `<username>`:

| Object | Kind | Name | When created |
|--------|------|------|---|
| Kubeflow Profile | `kubeflow.org/v1 Profile` | `<username>` | User creation |
| MinIO credentials | `v1 Secret` | `mlpipeline-minio-artifact` | After namespace ready |
| Elyra notebook injection | `kubeflow.org/v1alpha1 PodDefault` | `elyra-minio-access` | After MinIO secret |
| Resource limits | `v1 ResourceQuota` | `portal-quota` | When quota group assigned |

---

## 14. How kubectl Is Used

All Kubernetes calls go through the `kubectl` binary on the host — no Python SDK.

### Pattern 1 — Apply a manifest via stdin

Used for: Profile, Secret, PodDefault, ResourceQuota creation/update.

```python
proc = subprocess.Popen(
    ["kubectl", "apply", "-f", "-"],  # "-" = read from stdin
    stdin=subprocess.PIPE,   # open a write channel into kubectl
    stdout=subprocess.PIPE,  # capture output (not printed to terminal)
    stderr=subprocess.PIPE,  # capture errors
    text=True,               # use strings, not bytes
)
stdout, stderr = proc.communicate(input=yaml_manifest)
# communicate() sends the manifest string and waits for kubectl to finish
if proc.returncode != 0:
    # handle error using stderr
```

### Pattern 2 — Run a query or delete

Used for: get namespace (polling), get secret, delete profile, delete namespace, delete resourcequota.

```python
result = subprocess.run(
    ["kubectl", "get", "namespace", username],
    capture_output=True,  # same as stdout=PIPE, stderr=PIPE
    text=True,
)
if result.returncode == 0:
    # success
```

---

## 15. Frontend Architecture

The frontend is a **single HTML file** (`index.html`) with inline CSS and JavaScript — no build tools, no frameworks.

### Navigation

Three sidebar sections, switched by `showPage(page)`:

| Nav Item | Page ID | Content |
|---|---|---|
| User Provisioner | `page-users` | Health widgets + provision form + user directory |
| Resource Quotas | `page-quota` | Quota group cards (CRUD) |

### Data Flow

```
window.onload → init()
  ├── fetchHealth()     → GET /api/health    → update 3 status dots
  ├── fetchUsers()      → GET /api/users     → render directory table
  └── fetchQuotaGroupsForSelect()  → GET /api/quota-groups  → populate dropdown

Polling:
  fetchHealth() every 15s
  fetchUsers()  every 10s
```

### State

`allQuotaGroups` — module-level array, populated by `fetchQuotaGroups()`.  
Used by the reassign modal to build its `<select>` without a second API call.

### Key JS Functions

| Function | Trigger | Action |
|---|---|---|
| `provisionUser()` | Submit button | `POST /api/users` |
| `openReassignModal(username, name, groupId)` | Pencil icon | Opens quota reassign modal |
| `saveReassign()` | Apply button | `PATCH /api/users/{username}/quota-group` |
| `openDeleteModal(gitlabId, username, name)` | Trash icon | Opens typed-confirmation modal |
| `confirmDelete()` | Delete button in modal | `DELETE /api/users/{id}/{username}` |
| `fetchQuotaGroups()` | Page navigation | `GET /api/quota-groups` → render cards |
| `saveQuotaGroup()` | Save in quota modal | `POST` or `PUT /api/quota-groups` |
| `duplicateGroup(id)` | Duplicate icon | `POST /api/quota-groups/{id}/duplicate` |
| `deleteGroup(id)` | Delete icon | `DELETE /api/quota-groups/{id}` |
| `manualSync()` | Sync Kubernetes button | `POST /api/quota-groups/sync` |

---

## 16. Verifying State in Kubernetes

### Check all ResourceQuotas

```bash
kubectl get resourcequota --all-namespaces
```

### Inspect a specific user's quota

```bash
kubectl describe resourcequota portal-quota -n <username>
```

Example output:
```
Name:              portal-quota
Namespace:         alice
Resource           Used    Hard
--------           ----    ----
limits.cpu         1       5
limits.memory      4Gi     16Gi
requests.storage   10Gi    50Gi
```

### Check the Kubeflow Profile

```bash
kubectl get profile <username> -o yaml
```

### Verify the MinIO secret was replicated

```bash
kubectl get secret mlpipeline-minio-artifact -n <username>
```

### Check Elyra PodDefault

```bash
kubectl get poddefault elyra-minio-access -n <username> -o yaml
```

### Read portal logs

```bash
# All recent logs
sudo journalctl -u kubeflow-admin-portal -n 100

# Filter for quota-related events only
sudo journalctl -u kubeflow-admin-portal -n 200 | grep -i quota

# Follow live
sudo journalctl -u kubeflow-admin-portal -f
```

---

## 17. Troubleshooting

### User stuck in PROVISIONING

The background MinIO replication task may still be running or failed silently.

```bash
# Check logs for that username
sudo journalctl -u kubeflow-admin-portal -n 200 | grep <username>

# Check if namespace exists
kubectl get namespace <username>

# Check if MinIO secret was replicated
kubectl get secret mlpipeline-minio-artifact -n <username>
```

If the namespace exists but secret is missing, fix manually:
```bash
kubectl get secret mlpipeline-minio-artifact -n kubeflow -o json \
  | jq 'del(.metadata.resourceVersion,.metadata.uid,.metadata.ownerReferences) | .metadata.namespace = "<username>"' \
  | kubectl apply -f -
```
Then update the DB status:
```bash
sqlite3 /var/lib/portal-data/portal.db \
  "UPDATE users SET profile_status='READY' WHERE username='<username>';"
```

### ResourceQuota not applied

Use the **Sync Kubernetes** button in the portal, or run:
```bash
# Check what's in DB vs K8s
sqlite3 /var/lib/portal-data/portal.db \
  "SELECT u.username, qg.name, qg.cpu_limit FROM users u LEFT JOIN quota_groups qg ON qg.id=u.quota_group_id;"

kubectl get resourcequota portal-quota -n <username>
```

### Portal won't start

```bash
sudo systemctl status kubeflow-admin-portal
sudo journalctl -u kubeflow-admin-portal -n 50
```

Common causes:
- `GITLAB_TOKEN` not set or expired → regenerate via `gitlab-rails runner`
- `KUBECONFIG` path wrong → check `/root/.kube/config` exists
- Python import error → check that `app/` directory was fully synced

### GitLab shows "Unreachable" in health widget

```bash
# Test from the GitLab VM itself
curl -s -H "Private-Token: $GITLAB_TOKEN" http://localhost/api/v4/users?per_page=1
```

If you get `401` the token expired. Regenerate:
```bash
gitlab-rails runner "
  user = User.find_by_username('root')
  user.personal_access_tokens.where(name: 'kubeflow-admin-portal-token').destroy_all
  token = user.personal_access_tokens.create!(
    name: 'kubeflow-admin-portal-token', scopes: ['api'], expires_at: 365.days.from_now
  )
  puts token.token
"
```

Then update the systemd service with the new token and restart.
