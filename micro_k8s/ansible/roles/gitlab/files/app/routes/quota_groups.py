"""
Resource Quota Group endpoints.

  GET    /api/quota-groups                  — list all groups
  POST   /api/quota-groups                  — create a group
  GET    /api/quota-groups/{id}             — get one group
  PUT    /api/quota-groups/{id}             — update a group (triggers K8s sync)
  DELETE /api/quota-groups/{id}             — delete a group
  POST   /api/quota-groups/{id}/duplicate   — clone a group
  POST   /api/quota-groups/sync             — manual full reconciliation
"""
import re
import sqlite3
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, field_validator

from app.config import DB_PATH, logger
from app.database import get_db
from app.services.quota_sync import reconcile_all, sync_user_quota

router = APIRouter(prefix="/api/quota-groups", tags=["quota-groups"])

_GI_RE = re.compile(r"^\d+(\.\d+)?[Gg][Ii]$")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class QuotaGroupCreate(BaseModel):
    name: str
    description: Optional[str] = ""
    cpu_limit: float
    memory_limit: str
    storage_limit: str
    gpu_limit: int = 0

    @field_validator("cpu_limit")
    @classmethod
    def cpu_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("cpu_limit must be > 0")
        return v

    @field_validator("memory_limit", "storage_limit")
    @classmethod
    def gi_format(cls, v: str) -> str:
        # Normalise to canonical "Gi" regardless of input case (GI, gi, Gi)
        normalised = re.sub(r"[Gg][Ii]$", "Gi", v)
        if not _GI_RE.match(normalised):
            raise ValueError(f"'{v}' must be in Gi format, e.g. '16Gi'")
        return normalised

    @field_validator("gpu_limit")
    @classmethod
    def gpu_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("gpu_limit must be >= 0")
        return v


class QuotaGroupUpdate(QuotaGroupCreate):
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict:
    return {
        "id":            row["id"],
        "name":          row["name"],
        "description":   row["description"],
        "cpu_limit":     row["cpu_limit"],
        "memory_limit":  row["memory_limit"],
        "storage_limit": row["storage_limit"],
        "gpu_limit":     row["gpu_limit"],
        "user_count":    row["user_count"],
        "created_at":    row["created_at"],
        "updated_at":    row["updated_at"],
    }


def _get_group_or_404(c, group_id: int) -> sqlite3.Row:
    c.execute(
        """
        SELECT qg.*,
               COUNT(u.id) AS user_count
        FROM   quota_groups qg
        LEFT JOIN users u ON u.quota_group_id = qg.id
        WHERE  qg.id = ?
        GROUP BY qg.id
        """,
        (group_id,),
    )
    row = c.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Quota group {group_id} not found")
    return row


def _get_users_for_group(c, group_id: int) -> list:
    c.execute(
        "SELECT username FROM users WHERE quota_group_id = ? AND profile_status = 'READY'",
        (group_id,),
    )
    return [r["username"] for r in c.fetchall()]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("")
def list_groups():
    """Return all quota groups with a user count."""
    with get_db() as (conn, c):
        c.execute(
            """
            SELECT qg.*,
                   COUNT(u.id) AS user_count
            FROM   quota_groups qg
            LEFT JOIN users u ON u.quota_group_id = qg.id
            GROUP BY qg.id
            ORDER BY qg.name
            """
        )
        return [_row_to_dict(r) for r in c.fetchall()]


@router.get("/{group_id}")
def get_group(group_id: int):
    """Return a single quota group."""
    with get_db() as (conn, c):
        return _row_to_dict(_get_group_or_404(c, group_id))


@router.post("", status_code=201)
def create_group(group: QuotaGroupCreate):
    """Create a new quota group."""
    try:
        with get_db() as (conn, c):
            c.execute(
                """
                INSERT INTO quota_groups
                    (name, description, cpu_limit, memory_limit, storage_limit, gpu_limit)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    group.name,
                    group.description,
                    group.cpu_limit,
                    group.memory_limit,
                    group.storage_limit,
                    group.gpu_limit,
                ),
            )
            new_id = c.lastrowid
            return _row_to_dict(_get_group_or_404(c, new_id))
    except Exception as exc:
        if "UNIQUE constraint failed" in str(exc):
            raise HTTPException(
                status_code=409, detail=f"A quota group named '{group.name}' already exists."
            )
        raise HTTPException(status_code=500, detail=str(exc))


@router.put("/{group_id}")
def update_group(
    group_id: int,
    group: QuotaGroupUpdate,
    background_tasks: BackgroundTasks,
):
    """Update a quota group and trigger K8s sync for all affected users."""
    with get_db() as (conn, c):
        _get_group_or_404(c, group_id)  # 404 guard
        try:
            c.execute(
                """
                UPDATE quota_groups
                SET name          = ?,
                    description   = ?,
                    cpu_limit     = ?,
                    memory_limit  = ?,
                    storage_limit = ?,
                    gpu_limit     = ?,
                    updated_at    = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    group.name,
                    group.description,
                    group.cpu_limit,
                    group.memory_limit,
                    group.storage_limit,
                    group.gpu_limit,
                    group_id,
                ),
            )
        except Exception as exc:
            if "UNIQUE constraint failed" in str(exc):
                raise HTTPException(
                    status_code=409,
                    detail=f"A quota group named '{group.name}' already exists.",
                )
            raise HTTPException(status_code=500, detail=str(exc))

        affected_users = _get_users_for_group(c, group_id)
        updated_row = _get_group_or_404(c, group_id)

    # Sync K8s quotas in the background for every user in this group
    group_dict = dict(updated_row)
    for username in affected_users:
        background_tasks.add_task(sync_user_quota, username, group_dict)

    logger.info(
        f"Quota group {group_id} updated — queuing K8s sync for {len(affected_users)} user(s)"
    )
    return _row_to_dict(updated_row)


@router.delete("/{group_id}")
def delete_group(group_id: int, background_tasks: BackgroundTasks):
    """
    Delete a quota group.  Users assigned to it revert to NULL (unlimited).
    Their ResourceQuotas are removed from Kubernetes.
    """
    with get_db() as (conn, c):
        _get_group_or_404(c, group_id)
        affected_users = _get_users_for_group(c, group_id)

        # Unassign users first (FK would be NULL via ON DELETE SET NULL,
        # but we need the usernames before the row is gone).
        c.execute("DELETE FROM quota_groups WHERE id = ?", (group_id,))

    for username in affected_users:
        background_tasks.add_task(sync_user_quota, username, None)

    logger.info(
        f"Quota group {group_id} deleted — queuing K8s quota removal for {len(affected_users)} user(s)"
    )
    return {
        "status": "success",
        "message": f"Quota group {group_id} deleted. {len(affected_users)} user(s) reverted to Unlimited.",
    }


@router.post("/{group_id}/duplicate", status_code=201)
def duplicate_group(group_id: int):
    """Clone a quota group with a new unique name."""
    with get_db() as (conn, c):
        src = _get_group_or_404(c, group_id)

        # Find a non-colliding name
        base_name = f"{src['name']} (copy)"
        new_name = base_name
        counter = 2
        while True:
            c.execute("SELECT id FROM quota_groups WHERE name = ?", (new_name,))
            if not c.fetchone():
                break
            new_name = f"{base_name} {counter}"
            counter += 1

        c.execute(
            """
            INSERT INTO quota_groups
                (name, description, cpu_limit, memory_limit, storage_limit, gpu_limit)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_name,
                src["description"],
                src["cpu_limit"],
                src["memory_limit"],
                src["storage_limit"],
                src["gpu_limit"],
            ),
        )
        new_id = c.lastrowid
        return _row_to_dict(_get_group_or_404(c, new_id))


@router.post("/sync")
def manual_sync():
    """
    Manually reconcile all users' K8s ResourceQuotas against the database.
    Blocks until complete and returns a full report.
    """
    report = reconcile_all()
    logger.info(f"Manual sync completed: {report}")
    return {"status": "success", "report": report}


# ---------------------------------------------------------------------------
# Dashboard stats endpoint
# ---------------------------------------------------------------------------

@router.get("/stats/overview")
def quota_stats():
    """Aggregate stats used by the Resource Management dashboard."""
    with get_db() as (conn, c):
        # Counts
        c.execute("SELECT COUNT(*) FROM quota_groups")
        total_groups = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM users WHERE quota_group_id IS NOT NULL")
        users_with_quota = c.fetchone()[0]

        c.execute("SELECT COUNT(*) FROM users WHERE quota_group_id IS NULL")
        unlimited_users = c.fetchone()[0]

        # Totals (sum of limits across ALL groups, weighted by user count)
        c.execute(
            """
            SELECT
                SUM(qg.cpu_limit     * sub.cnt) AS total_cpu,
                GROUP_CONCAT(qg.memory_limit)    AS memory_vals,
                SUM(qg.gpu_limit     * sub.cnt)  AS total_gpu
            FROM quota_groups qg
            JOIN (
                SELECT quota_group_id, COUNT(*) AS cnt
                FROM   users
                WHERE  quota_group_id IS NOT NULL
                GROUP  BY quota_group_id
            ) sub ON sub.quota_group_id = qg.id
            """
        )
        totals = c.fetchone()

        # Per-group breakdown for charts
        c.execute(
            """
            SELECT qg.id, qg.name,
                   qg.cpu_limit, qg.memory_limit,
                   qg.storage_limit, qg.gpu_limit,
                   COUNT(u.id) AS user_count
            FROM   quota_groups qg
            LEFT JOIN users u ON u.quota_group_id = qg.id
            GROUP BY qg.id
            ORDER BY qg.name
            """
        )
        groups = [dict(r) for r in c.fetchall()]

    return {
        "total_groups":    total_groups,
        "users_with_quota": users_with_quota,
        "unlimited_users": unlimited_users,
        "total_cpu_allocated":    totals[0] or 0,
        "total_gpu_allocated":    totals[2] or 0,
        "groups": groups,
    }
