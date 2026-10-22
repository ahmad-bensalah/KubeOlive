#  07 — Kubeflow Admin Portal

## Overview

The Admin Portal is a FastAPI web application that gives platform administrators
a single UI to manage all MLOps users.  It runs on the GitLab VM (port 7070)
and orchestrates three systems simultaneously: GitLab, Kubernetes, and SQLite.

**Files:**
```
micro_k8s/ansible/roles/gitlab/files/
├── app/
│   ├── main.py              # FastAPI entry point + startup
│   ├── config.py            # Constants and env vars
│   ├── database.py          # SQLite schema, migrations, helpers
│   ├── gitlab_client.py     # GitLab REST API wrapper
│   ├── kubernetes.py        # kubectl interactions
│   ├── routes/
│   │   ├── users.py         # /api/users endpoints
│   │   ├── quota_groups.py  # /api/quota-groups endpoints
│   │   └── health.py        # /api/health endpoint
│   └── services/
│       └── quota_sync.py    # ResourceQuota CRUD + reconciliation
└── index.html               # Single-page frontend
```

---

## User Provisioning Flow

When an admin clicks "Provision Identity & Namespace":

```
POST /api/users
{ name, username, email, password, quota_group_id? }
         │
         ▼
1. Sanitise username  →  keep only [a-z0-9-]  (K8s namespace-safe)
         │
         ▼
2. Validate quota_group_id exists in DB (if provided)
         │
         ▼
3. Duplicate check  →  SELECT WHERE username=? OR email=?
         │
         ▼
4. Create GitLab user
   gl_api("POST", "/users", { skip_confirmation: true })
   → returns gitlab_id
         │
         ▼
5. Insert into SQLite
   INSERT INTO users (gitlab_id, name, username, email,
                      password, profile_status="PROVISIONING", quota_group_id)
   → on failure: DELETE GitLab user (rollback)
         │
         ▼
6. Create Kubeflow Profile
   kubectl apply -f - (Profile YAML)
   → on failure: status = "PROFILE_CREATION_FAILED"
         │
         ▼
7. Queue background tasks (returns 200 immediately)
   ├─ replicate_minio_secret_and_verify(username)
   └─ sync_user_quota(username, group)   ← if quota group assigned
```

### Background Task: MinIO + Elyra Setup

```
replicate_minio_secret_and_verify(username)
  │
  ├─ Poll kubectl get namespace <username>  (30s timeout, 1s intervals)
  │
  ├─ kubectl get secret mlpipeline-minio-artifact -n kubeflow -o json
  │  Patch: set namespace = username, remove resourceVersion/uid
  │
  ├─ kubectl apply -f -  (apply secret in user namespace)
  │
  ├─ UPDATE users SET profile_status = "READY"
  │
  └─ apply_elyra_poddefault(username)
       kubectl apply -f -  (PodDefault YAML)
```

### Profile Status Values

| Status | Meaning |
|---|---|
| `PROVISIONING` | Created in GitLab + DB, K8s work in progress |
| `READY` | Namespace ready, MinIO secret replicated, fully operational |
| `PROFILE_CREATION_FAILED` | `kubectl apply Profile` failed |
| `NAMESPACE_TIMEOUT` | Namespace didn't appear within 30s |
| `MINIO_SECRET_FETCH_FAILED` | Couldn't read secret from `kubeflow` namespace |
| `MINIO_SECRET_APPLY_FAILED` | Couldn't write secret to user namespace |
| `REPLICATION_ERROR` | Unexpected exception |

---

## User Deletion Flow

```
DELETE /api/users/{gitlab_id}/{username}
  │
  ├─ 1. gl_api DELETE /users/{id}?hard_delete=true
  │      → 204 or 404 = success
  │      → hard_delete permanently removes all GitLab data
  │
  ├─ 2. kubectl delete profile <username>
  │      Kubeflow Profile controller cascades:
  │      deletes namespace + RBAC + PVCs
  │
  ├─ 3. kubectl delete namespace <username> --wait=false
  │      Belt-and-suspenders in case Profile is stuck
  │      ResourceQuota is inside the namespace → deleted with it
  │
  └─ 4. DELETE FROM users WHERE gitlab_id = ?
```

---

## Health Check

```
GET /api/health  (polled every 15s by frontend)
  │
  ├─ kubectl version --client
  │    returncode 0 → kubernetes_reachable: true
  │
  ├─ gl_api GET /users?per_page=1
  │    status 200 → gitlab_reachable: true
  │
  └─ TCP connect to 10.110.188.144:30080 (2s timeout)
       success → longhorn_reachable: true
```


---

## How kubectl Is Used

All Kubernetes operations call the `kubectl` binary via subprocess.

**Pattern 1 — Apply a manifest:**
```python
proc = subprocess.Popen(
    ["kubectl", "apply", "-f", "-"],
    stdin=subprocess.PIPE,    # write YAML into kubectl's stdin
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
)
stdout, stderr = proc.communicate(input=yaml_manifest)
if proc.returncode != 0:
    # handle error
```

Used for: Kubeflow Profiles, MinIO secrets, Elyra PodDefaults, ResourceQuotas.

**Pattern 2 — Query or delete:**
```python
result = subprocess.run(
    ["kubectl", "get", "namespace", username],
    capture_output=True,
    text=True,
)
if result.returncode == 0:
    # namespace exists
```

Used for: namespace polling, secret reads, profile deletion, namespace deletion.

---

## Database (`database.py`)

```sql
-- Quota group definitions
CREATE TABLE quota_groups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT,
    cpu_limit     REAL NOT NULL,      -- user-facing cores (Istio overhead added transparently)
    memory_limit  TEXT NOT NULL,      -- e.g. "16Gi"
    storage_limit TEXT NOT NULL,      -- e.g. "50Gi"
    gpu_limit     INTEGER NOT NULL DEFAULT 0,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Provisioned users
CREATE TABLE users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    gitlab_id      INTEGER UNIQUE,
    name           TEXT,
    username       TEXT UNIQUE,    -- also the K8s namespace name
    email          TEXT UNIQUE,    -- OIDC identifier in Kubeflow
    password       TEXT,           -- stored plaintext (admin-only portal)
    profile_status TEXT,
    quota_group_id INTEGER         -- NULL = Unlimited
        REFERENCES quota_groups(id) ON DELETE SET NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

The `quota_group_id` column was added via `ALTER TABLE` migration:
```python
# In init_db() — runs on every startup, idempotent
existing_cols = {row[1] for row in c.execute("PRAGMA table_info(users)")}
if "quota_group_id" not in existing_cols:
    c.execute("ALTER TABLE users ADD COLUMN quota_group_id INTEGER ...")
```

Existing users default to `NULL` (Unlimited) automatically.

---

## Deployment

The portal runs as a systemd service:

```ini
[Service]
User=root
WorkingDirectory=/home/ubuntu/admin-portal
ExecStart=/home/ubuntu/admin-portal/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 7070
Environment=GITLAB_TOKEN=<token>
Environment=KUBECONFIG=/root/.kube/config
```

---

## API Reference

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serve frontend HTML |
| GET | `/api/health` | Connectivity check |
| GET | `/api/users` | List users (cross-referenced with GitLab) |
| POST | `/api/users` | Provision new user |
| PATCH | `/api/users/{username}/quota-group` | Reassign quota group |
| DELETE | `/api/users/{gitlab_id}/{username}` | Delete user |
| GET | `/api/quota-groups` | List quota groups |
| POST | `/api/quota-groups` | Create quota group |
| GET | `/api/quota-groups/{id}` | Get quota group |
| PUT | `/api/quota-groups/{id}` | Update quota group |
| DELETE | `/api/quota-groups/{id}` | Delete quota group |
| POST | `/api/quota-groups/{id}/duplicate` | Clone quota group |
| POST | `/api/quota-groups/sync` | Manual K8s reconciliation |

---

## Troubleshooting

```bash
# Live logs
sudo journalctl -u kubeflow-admin-portal -f

# Last 50 lines
sudo journalctl -u kubeflow-admin-portal -n 50

# Filter quota-related events
sudo journalctl -u kubeflow-admin-portal -n 200 | grep -i quota

```
