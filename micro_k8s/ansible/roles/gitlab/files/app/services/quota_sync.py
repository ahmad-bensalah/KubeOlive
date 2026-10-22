"""
Kubernetes ResourceQuota service.

Provides idempotent create / update / delete / read operations for
ResourceQuota objects, plus a full database↔cluster reconciliation loop.

Design decisions:
- All kubectl calls follow the same subprocess pattern used in kubernetes.py.
- The ResourceQuota is always named  "portal-quota"  inside the user namespace.
- Only CPU, Memory, Storage, and GPU limits are managed here.
- No LimitRange, no default notebook resources.

Istio overhead:
- Kubeflow injects Istio sidecars (istio-proxy) into every namespace pod.
- Each sidecar has a limits.cpu of 2 cores.
- With 2-3 system pods (ml-pipeline-ui-artifact, ml-pipeline-visualizationserver)
  that is ~4-6 cores consumed before any notebook starts.
- ISTIO_CPU_OVERHEAD is added transparently to every ResourceQuota so that
  the quota group's cpu_limit represents usable notebook cores, not total namespace cores.
"""
import json
import sqlite3
import subprocess
from typing import Optional

from app.config import DB_PATH, logger

# The fixed name given to every ResourceQuota created by this portal.
QUOTA_NAME = "portal-quota"

# Extra CPU cores reserved for Istio sidecars injected into every namespace.
# This overhead is added transparently so that the cpu_limit in a quota group
# represents usable notebook cores, not raw namespace cores.
ISTIO_CPU_OVERHEAD = 6.0


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _build_manifest(namespace: str, group: dict) -> str:
    """
    Render a Kubernetes ResourceQuota manifest from a quota_group row.

    The effective CPU limit = group["cpu_limit"] + ISTIO_CPU_OVERHEAD.
    This ensures the user-facing cpu_limit represents usable notebook cores
    while Istio sidecars are accounted for transparently.

    *group* must contain: cpu_limit, memory_limit, storage_limit, gpu_limit.
    """
    effective_cpu = group["cpu_limit"] + ISTIO_CPU_OVERHEAD

    hard: dict = {
        "limits.cpu":       str(effective_cpu),
        "limits.memory":    group["memory_limit"],
        "requests.storage": group["storage_limit"],
        "requests.nvidia.com/gpu": str(group["gpu_limit"]),
        "limits.nvidia.com/gpu":   str(group["gpu_limit"]),
    }

    hard_yaml = "\n".join(f"        {k}: \"{v}\"" for k, v in hard.items())

    return f"""apiVersion: v1
kind: ResourceQuota
metadata:
  name: {QUOTA_NAME}
  namespace: {namespace}
  labels:
    managed-by: kubeflow-admin-portal
spec:
  hard:
{hard_yaml}
"""


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------

def apply_resource_quota(namespace: str, group: dict) -> bool:
    """
    Create or update the ResourceQuota in *namespace*.
    Returns True on success, False on failure.
    """
    manifest = _build_manifest(namespace, group)
    proc = subprocess.Popen(
        ["kubectl", "apply", "-f", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = proc.communicate(input=manifest)
    if proc.returncode == 0:
        logger.info(f"ResourceQuota '{QUOTA_NAME}' applied to namespace '{namespace}'")
        return True
    logger.error(f"Failed to apply ResourceQuota in '{namespace}': {stderr}")
    return False


def delete_resource_quota(namespace: str) -> bool:
    """
    Delete the ResourceQuota from *namespace*.
    Returns True on success or if it was already absent.
    """
    result = subprocess.run(
        ["kubectl", "delete", "resourcequota", QUOTA_NAME, "-n", namespace],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        logger.info(f"ResourceQuota '{QUOTA_NAME}' deleted from namespace '{namespace}'")
        return True
    if "not found" in result.stderr.lower():
        logger.info(f"ResourceQuota '{QUOTA_NAME}' not present in '{namespace}', nothing to delete")
        return True
    logger.error(f"Failed to delete ResourceQuota in '{namespace}': {result.stderr}")
    return False


def get_resource_quota(namespace: str) -> Optional[dict]:
    """
    Read the ResourceQuota from Kubernetes.
    Returns the parsed JSON dict, or None if absent.
    """
    result = subprocess.run(
        ["kubectl", "get", "resourcequota", QUOTA_NAME, "-n", namespace, "-o", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except Exception:
        return None


def quota_matches_group(namespace: str, group: dict) -> bool:
    """
    Return True if the live ResourceQuota hard limits already match *group*.
    Accounts for ISTIO_CPU_OVERHEAD when comparing cpu limits.
    Used to decide whether an update is actually needed.
    """
    live = get_resource_quota(namespace)
    if not live:
        return False
    hard = live.get("spec", {}).get("hard", {})
    effective_cpu = group["cpu_limit"] + ISTIO_CPU_OVERHEAD
    expected = {
        "limits.cpu":       str(effective_cpu),
        "limits.memory":    group["memory_limit"],
        "requests.storage": group["storage_limit"],
        "requests.nvidia.com/gpu": str(group["gpu_limit"]),
        "limits.nvidia.com/gpu":   str(group["gpu_limit"]),
    }
    return all(hard.get(k) == v for k, v in expected.items())


# ---------------------------------------------------------------------------
# Automatic synchronisation (triggered on user/group changes)
# ---------------------------------------------------------------------------

def sync_user_quota(username: str, group: Optional[dict]) -> None:
    """
    Idempotent sync for a single user:
    - If group is set  → apply (create/update) the ResourceQuota.
    - If group is None → delete any existing ResourceQuota.
    """
    if group:
        apply_resource_quota(username, group)
    else:
        delete_resource_quota(username)


# ---------------------------------------------------------------------------
# Manual full reconciliation
# ---------------------------------------------------------------------------

def reconcile_all() -> dict:
    """
    Iterate every user in the database and reconcile the Kubernetes state.

    Returns a report dict with lists: created, updated, deleted, errors.
    """
    report = {"created": [], "updated": [], "deleted": [], "errors": []}

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute(
        """
        SELECT u.username, u.quota_group_id,
               qg.cpu_limit, qg.memory_limit, qg.storage_limit, qg.gpu_limit
        FROM   users u
        LEFT JOIN quota_groups qg ON qg.id = u.quota_group_id
        WHERE  u.profile_status = 'READY'
        """
    )
    rows = c.fetchall()
    conn.close()

    for row in rows:
        username = row["username"]
        has_group = row["quota_group_id"] is not None

        live = get_resource_quota(username)

        if not has_group:
            if live:
                ok = delete_resource_quota(username)
                (report["deleted"] if ok else report["errors"]).append(username)
            # else: nothing to do
        else:
            group = {
                "cpu_limit":     row["cpu_limit"],
                "memory_limit":  row["memory_limit"],
                "storage_limit": row["storage_limit"],
                "gpu_limit":     row["gpu_limit"],
            }
            if not live:
                ok = apply_resource_quota(username, group)
                (report["created"] if ok else report["errors"]).append(username)
            elif not quota_matches_group(username, group):
                ok = apply_resource_quota(username, group)
                (report["updated"] if ok else report["errors"]).append(username)
            # else: already in sync

    return report
