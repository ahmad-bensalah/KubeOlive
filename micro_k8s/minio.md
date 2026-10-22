# MinIO in Our Kubeflow Platform — A Beginner's Guide

## What is MinIO?

MinIO is a **file storage system** — think of it like a private Google Drive, but for machines instead of humans.

Instead of saving files to a hard drive, your ML code saves datasets, trained models, and pipeline outputs to MinIO using the **S3 API** (the same standard Amazon uses for AWS S3).

---

## The Problem We Had to Solve

Kubeflow ships with **one shared MinIO** for the entire cluster. Every user's pipeline artifacts (model files, datasets, logs) go into the same bucket. User A can technically access User B's files. That's not acceptable.

We needed **each user to have their own private storage** — completely isolated from everyone else.

---

## Our Solution: One MinIO Per User

Instead of one shared MinIO, we deploy a **dedicated MinIO instance inside every user's namespace**.

```
Before (default Kubeflow):
┌─────────────────────────────────────────────┐
│            ONE shared MinIO                 │
│                                             │
│  bucket: mlpipeline                         │
│  ├── user1/model.pkl    ← user1             │
│  ├── user2/model.pkl    ← user2             │
│  └── user3/dataset.csv  ← user3             │
│                                             │
│  ⚠️  Any user can see everyone else's files │
└─────────────────────────────────────────────┘

After (our setup):
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
│  user1 namespace │  │  user2 namespace │  │  user3 namespace │
│                  │  │                  │  │                  │
│  MinIO pod       │  │  MinIO pod       │  │  MinIO pod       │
│  └── own disk    │  │  └── own disk    │  │  └── own disk    │
│      (PVC)       │  │      (PVC)       │  │      (PVC)       │
│                  │  │                  │  │                  │
│  ✅ Private      │  │  ✅ Private      │  │  ✅ Private      │
└──────────────────┘  └──────────────────┘  └──────────────────┘
```

Each user's MinIO:
- Runs as its own **Kubernetes pod** inside their namespace
- Stores files on its own **Longhorn PVC** (Persistent Volume Claim) — a private 10 GB disk
- Is **completely invisible** to other users

---

## How a User Accesses Their MinIO

### From the Kubeflow Dashboard (browser)

```
User clicks "MinIO Storage" in the sidebar
        │
        ▼
Browser sends request to:
http://10.110.188.200/_api/minio/login/username
        │
        ▼
Istio Gateway routes it to the Admin Portal
(running on the GitLab VM at 10.110.188.148:7070)
        │
        ▼
Admin Portal returns a small HTML page with JavaScript
        │
        ▼
JavaScript runs IN THE BROWSER and sends:
POST /minio/username/api/v1/login
(with shared MinIO credentials)
        │
        ▼
Istio Gateway routes /minio/username/ to:
minio.username.svc.cluster.local:9001
(the user's OWN MinIO Console pod)
        │
        ▼
MinIO Console sets a session cookie in the browser
        │
        ▼
Browser redirects to /minio/username/
        │
        ▼
✅ User sees their private MinIO Console — their files only
```

### Why JavaScript in the browser instead of a backend redirect?

The Kubeflow Gateway has **OIDC authentication** (login via Dex/GitLab). Any backend-to-backend request from our Admin Portal VM to the gateway would be rejected (403 Forbidden) because the server doesn't have the user's login cookie.

By running the login in the **browser**, the user's existing Kubeflow session cookie is automatically included — so the authentication passes.

---

## How a User Accesses Their MinIO from a Notebook (Python)

When a user launches a Jupyter Notebook with the `elyra-minio-access` PodDefault checked, these environment variables are **automatically injected** — no setup required:

```
ELYRA_COS_ENDPOINT    = http://minio:9000
ELYRA_COS_BUCKET      = mlpipeline
ELYRA_COS_USERNAME    = minio
ELYRA_COS_PASSWORD    = <from secret>
```

`http://minio:9000` resolves to the user's **own** MinIO pod because inside Kubernetes, `minio` is a local service name that points to the pod in **the same namespace**. User1's `minio` is different from User2's `minio`.

```python
import boto3, os

s3 = boto3.client(
    "s3",
    endpoint_url="http://minio:9000",          # → user's OWN MinIO
    aws_access_key_id=os.environ["ELYRA_COS_USERNAME"],
    aws_secret_access_key=os.environ["ELYRA_COS_PASSWORD"],
)

# This writes ONLY to user1's private disk — user2 cannot see it
s3.put_object(Bucket="mlpipeline", Key="my-model/v1.pkl", Body=model_bytes)
```

---

## Why the Credentials Are the Same for All Users

You might wonder: if all users use the same password (`minio` / `9X8UV05...`), isn't that a security issue?

**No — and here's why:**

```
User1 logs in with:  username=minio  password=9X8UV05...
        │
        ▼
Routes to: minio.user1.svc.cluster.local  ← user1's pod, user1's disk

User2 logs in with:  username=minio  password=9X8UV05...
        │
        ▼
Routes to: minio.user2.svc.cluster.local  ← user2's pod, user2's disk
```

The **same key opens a different door** for each user. The isolation is enforced by Kubernetes namespace networking — not by separate passwords. `minio.user1` and `minio.user2` are physically different pods on different disks. There is no way for user1 to reach user2's pod, even with the same credentials.

---

## What Gets Created When a User Is Provisioned

When an admin creates a user via the Admin Portal (`http://10.110.188.148:7070`), the following is automatically created in Kubernetes:

```
Namespace: user1
├── Deployment: minio              ← MinIO server pod
├── Service: minio (port 9000/9001)← Internal DNS: minio.user1.svc.cluster.local
├── PVC: minio-pvc (10 Gi)         ← Private disk (Longhorn)
├── Secret: mlpipeline-minio-artifact ← Credentials for notebooks
├── PodDefault: elyra-minio-access ← Auto-injects env vars into notebooks
├── AuthorizationPolicy: minio-console-allow ← Allows browser traffic to port 9001
└── (in kubeflow namespace)
    └── VirtualService: minio-console-user1 ← Routes /minio/user1/ → user1's pod

Kubeflow Gateway (10.110.188.200)
└── VirtualService: admin-portal-proxy ← Routes /_api/minio/ → Admin Portal login helper
```

---

## What Gets Deleted When a User Is Removed

When a user is deleted from the Admin Portal, everything is cleaned up automatically:

```
✅ GitLab user account deleted
✅ Kubeflow Profile deleted (cascades to namespace deletion)
✅ All pods, PVCs, secrets deleted with the namespace
✅ VirtualService minio-console-user1 deleted from kubeflow namespace
✅ MinIO mc user account deleted from central MinIO
✅ User removed from portal database
```

---

## The Two MinIO Instances

There are actually **two types** of MinIO in the cluster. Don't confuse them:

| | Central MinIO (Juju) | Per-User MinIO |
|---|---|---|
| **Where** | `kubeflow` namespace | Each user namespace |
| **What for** | KFP pipeline system metadata | User's pipeline artifacts and data |
| **Who manages it** | Juju charm | Our Admin Portal |
| **Exposed at** | `http://10.110.188.144:30900` | `http://10.110.188.200/minio/{user}/` |
| **Users can access** | Admin only (via NodePort) | Each user via Dashboard |

The **per-user MinIO** is what users interact with. The **central MinIO** is a system component.

---

## Summary

```
Problem:   One shared bucket → all users can see each other's files

Solution:  One private MinIO pod per user, each on its own disk

Access:    Browser → Kubeflow Dashboard → auto-login → private console
           Notebook → http://minio:9000 → routes to OWN pod automatically

Isolation: Enforced by Kubernetes namespace networking
           Same credentials, different pods, different disks

Lifecycle: Created automatically when user is provisioned
           Deleted automatically when user is removed
```
