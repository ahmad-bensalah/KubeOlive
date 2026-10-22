"""
User management endpoints:
  GET    /api/users
  POST   /api/users
  PATCH  /api/users/{username}/quota-group
  DELETE /api/users/{gitlab_id}/{username}
"""
import sqlite3
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.config import DB_PATH, logger
from app.database import get_db, update_profile_status
from app.gitlab_client import gl_api, create_registry_token
from app.kubernetes import (
    create_kubeflow_profile,
    create_registry_pull_secret,
    delete_kubeflow_profile,
    delete_namespace,
    delete_user_minio_account,
    delete_minio_console_virtualservice,
    replicate_minio_secret_and_verify,
)
from app.services.quota_sync import sync_user_quota

router = APIRouter(prefix="/api/users", tags=["users"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class UserCreate(BaseModel):
    name: str
    username: str
    email: str
    password: str
    quota_group_id: Optional[int] = None


class UserQuotaUpdate(BaseModel):
    quota_group_id: Optional[int] = None  # None = Unlimited


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_gitlab_error(gl_res: dict) -> str:
    if not isinstance(gl_res, dict):
        return "Unknown GitLab error"
    if "message" in gl_res:
        msg = gl_res["message"]
        if isinstance(msg, dict):
            parts = []
            for field, messages in msg.items():
                if isinstance(messages, list):
                    parts.append(f"{field}: {', '.join(messages)}")
                else:
                    parts.append(f"{field}: {messages}")
            return "; ".join(parts)
        return str(msg)
    return gl_res.get("error", "Unknown GitLab error")


def _get_quota_group(c, group_id: int) -> Optional[dict]:
    c.execute("SELECT * FROM quota_groups WHERE id = ?", (group_id,))
    row = c.fetchone()
    return dict(row) if row else None


def _provision_registry_secret(gitlab_id: int, username: str) -> None:
    """
    Background helper: create a GitLab registry PAT for *username* then
    inject it as a ``gitlab-registry`` imagePullSecret into their namespace.

    Runs after namespace creation — waits up to 60 s for the namespace to
    exist before giving up.
    """
    import time

    # Wait for the namespace to be ready (Kubeflow Profile controller may
    # take a few seconds after replicate_minio_secret_and_verify finishes).
    for _ in range(60):
        result = __import__("subprocess").run(
            ["kubectl", "get", "namespace", username],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            break
        time.sleep(1)
    else:
        logger.error(
            f"Registry secret skipped: namespace '{username}' not ready after 60 s"
        )
        return

    token = create_registry_token(gitlab_id, username)
    if not token:
        logger.error(f"Registry secret skipped: could not create token for '{username}'")
        return

    create_registry_pull_secret(username, token)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("")
def list_users():
    """Return portal users that still exist in GitLab, including quota info."""
    gl_users, code = gl_api("GET", "/users?per_page=100")
    if code != 200:
        raise HTTPException(status_code=code, detail=f"GitLab error: {gl_users}")

    gl_usernames = {
        u["username"] for u in gl_users if u["username"] not in ("root", "ghost")
    }

    with get_db() as (conn, c):
        c.execute(
            """
            SELECT u.gitlab_id, u.name, u.username, u.email,
                   u.password, u.profile_status, u.created_at,
                   u.quota_group_id,
                   qg.name AS quota_group_name
            FROM   users u
            LEFT JOIN quota_groups qg ON qg.id = u.quota_group_id
            """
        )
        db_users = c.fetchall()

    return [
        {
            "gitlab_id":        row["gitlab_id"],
            "name":             row["name"],
            "username":         row["username"],
            "email":            row["email"],
            "password":         row["password"],
            "profile_status":   row["profile_status"],
            "created_at":       row["created_at"],
            "quota_group_id":   row["quota_group_id"],
            "quota_group_name": row["quota_group_name"],
        }
        for row in db_users
        if row["username"] in gl_usernames
    ]


@router.post("")
def create_user(user: UserCreate, background_tasks: BackgroundTasks):
    """Provision a GitLab account, a Kubeflow Profile, and queue MinIO + quota setup."""
    # 1. Sanitise username
    sanitized = "".join(c for c in user.username.lower() if c.isalnum() or c == "-")
    if not sanitized:
        raise HTTPException(
            status_code=400,
            detail="Username must contain alphanumeric characters or dashes.",
        )

    # 2. Validate quota_group_id if provided
    quota_group = None
    if user.quota_group_id is not None:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM quota_groups WHERE id = ?", (user.quota_group_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Quota group {user.quota_group_id} not found.",
            )
        quota_group = dict(row)

    # 3. Duplicate check
    with get_db() as (conn, c):
        c.execute(
            "SELECT id FROM users WHERE username = ? OR email = ?",
            (sanitized, user.email),
        )
        if c.fetchone():
            raise HTTPException(
                status_code=400,
                detail="User with this username or email already exists in local portal.",
            )

    # 4. Create GitLab user
    gl_res, code = gl_api(
        "POST",
        "/users",
        {
            "name": user.name,
            "username": sanitized,
            "email": user.email,
            "password": user.password,
            "skip_confirmation": True,
        },
    )
    if code >= 400:
        raise HTTPException(
            status_code=code,
            detail=f"GitLab error: {_format_gitlab_error(gl_res)}",
        )
    gitlab_id = gl_res.get("id")

    # 5. Persist to local DB
    try:
        with get_db() as (conn, c):
            c.execute(
                """
                INSERT INTO users
                    (gitlab_id, name, username, email, password, profile_status, quota_group_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    gitlab_id,
                    user.name,
                    sanitized,
                    user.email,
                    user.password,
                    "PROVISIONING",
                    user.quota_group_id,
                ),
            )
    except Exception as exc:
        logger.error(f"Failed to save user in SQLite: {exc}")
        gl_api("DELETE", f"/users/{gitlab_id}")
        raise HTTPException(status_code=500, detail="Failed to save user record locally.")

    # 6. Create Kubeflow Profile
    try:
        create_kubeflow_profile(sanitized, user.email)
    except RuntimeError as exc:
        logger.error(f"Failed to create Kubeflow Profile for {sanitized}: {exc}")
        update_profile_status(sanitized, "PROFILE_CREATION_FAILED")
        raise HTTPException(
            status_code=500, detail=f"Failed to create Kubeflow Profile: {exc}"
        )

    # 7. Queue background tasks: MinIO replication, quota sync, registry secret
    background_tasks.add_task(replicate_minio_secret_and_verify, sanitized, user.password)
    if quota_group:
        background_tasks.add_task(sync_user_quota, sanitized, quota_group)
    # Create a GitLab registry token for the user and inject it as an
    # imagePullSecret into their namespace so Kubeflow pods can pull
    # from their personal registry path automatically.
    background_tasks.add_task(_provision_registry_secret, gitlab_id, sanitized)

    return {
        "status": "success",
        "username": sanitized,
        "gitlab_id": gitlab_id,
        "quota_group_id": user.quota_group_id,
    }


@router.patch("/{username}/quota-group")
def update_user_quota_group(
    username: str,
    body: UserQuotaUpdate,
    background_tasks: BackgroundTasks,
):
    """
    Assign or remove a quota group for an existing user.
    Pass quota_group_id=null (or omit) to set Unlimited.
    Triggers K8s ResourceQuota sync automatically.
    """
    with get_db() as (conn, c):
        c.execute("SELECT id, profile_status FROM users WHERE username = ?", (username,))
        user_row = c.fetchone()
        if not user_row:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found.")

        quota_group = None
        if body.quota_group_id is not None:
            quota_group = _get_quota_group(c, body.quota_group_id)
            if not quota_group:
                raise HTTPException(
                    status_code=404,
                    detail=f"Quota group {body.quota_group_id} not found.",
                )

        c.execute(
            "UPDATE users SET quota_group_id = ? WHERE username = ?",
            (body.quota_group_id, username),
        )

    # Sync K8s quota in background
    background_tasks.add_task(sync_user_quota, username, quota_group)

    return {
        "status": "success",
        "username": username,
        "quota_group_id": body.quota_group_id,
    }


@router.post("/{username}/registry-secret")
def backfill_registry_secret(username: str, background_tasks: BackgroundTasks):
    """
    Provision (or re-provision) the GitLab registry imagePullSecret for an
    existing user. Useful for users created before the registry feature was added.
    """
    with get_db() as (conn, c):
        c.execute(
            "SELECT gitlab_id FROM users WHERE username = ?", (username,)
        )
        row = c.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"User '{username}' not found.")
        gitlab_id = row["gitlab_id"]

    background_tasks.add_task(_provision_registry_secret, gitlab_id, username)
    return {"status": "queued", "username": username}


@router.delete("/{gitlab_id}/{username}")
def delete_user(gitlab_id: int, username: str):
    """Permanently delete a user from GitLab, Kubernetes, and the local DB."""
    logger.info(f"Deleting user: {username} (GitLab ID: {gitlab_id})")

    # 1. Delete from GitLab
    gl_res, code = gl_api("DELETE", f"/users/{gitlab_id}?hard_delete=true")
    if code not in (200, 202, 204, 404):
        # Retry with standard delete request
        gl_res, code = gl_api("DELETE", f"/users/{gitlab_id}")

    if code not in (200, 202, 204, 404):
        error_msg = (
            gl_res.get("message", gl_res.get("error", "Unknown error"))
            if isinstance(gl_res, dict)
            else "Unknown error"
        )
        logger.error(f"Failed to delete GitLab user {gitlab_id}: {error_msg}")
        raise HTTPException(status_code=code, detail=f"GitLab error: {error_msg}")

    logger.info(f"GitLab user {gitlab_id} permanently deleted (or already deleted)")

    # 2. Delete Kubeflow Profile (cascades namespace deletion)
    delete_kubeflow_profile(username)

    # 3. Ensure namespace, MinIO account, and VirtualService are gone
    delete_user_minio_account(username)
    delete_minio_console_virtualservice(username)
    delete_namespace(username)

    # 4. Remove from local DB
    try:
        with get_db() as (conn, c):
            c.execute("DELETE FROM users WHERE gitlab_id = ?", (gitlab_id,))
        logger.info(f"User {username} removed from local database")
    except Exception as exc:
        logger.error(f"Failed to delete user from SQLite: {exc}")
        raise HTTPException(
            status_code=500,
            detail="User deleted from GitLab but local DB cleanup failed.",
        )

    return {"status": "success", "message": f"User {username} successfully deleted"}
