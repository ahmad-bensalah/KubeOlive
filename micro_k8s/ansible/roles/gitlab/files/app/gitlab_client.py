"""
Thin wrapper around the GitLab REST API.
"""
from typing import Any, Tuple

import requests

from app.config import GITLAB_TOKEN, GITLAB_URL, logger

# Type alias for the (data, http_status_code) pair returned by every call.
GitLabResponse = Tuple[Any, int]


def gl_api(method: str, path: str, data: Any = None) -> GitLabResponse:
    """
    Make an authenticated request to the GitLab API.

    Returns a ``(body, status_code)`` tuple.  The body is parsed JSON when
    available, otherwise a plain ``{"error": ...}`` dict.
    """
    headers = {"Private-Token": GITLAB_TOKEN}
    url = f"{GITLAB_URL}/api/v4{path}"

    try:
        resp = requests.request(method, url, headers=headers, json=data, timeout=15)

        # 204 No Content — common for DELETE
        if resp.status_code == 204:
            return {}, 204

        # Empty body
        if not resp.text or not resp.text.strip():
            return {}, resp.status_code

        if resp.status_code >= 400:
            try:
                return resp.json(), resp.status_code
            except Exception:
                return {"error": resp.text or "Unknown error"}, resp.status_code

        return resp.json(), resp.status_code

    except Exception as exc:
        logger.error(f"GitLab API request failed: {exc}")
        return {"error": str(exc)}, 500


def create_registry_token(gitlab_id: int, username: str) -> str | None:
    """
    Retrieve the user's plaintext password from the local portal DB.
    GitLab registry JWT auth works with username:password but NOT with PATs
    or impersonation tokens in this GitLab version.
    Returns the password string, or None if not found.
    """
    from app.database import get_db
    with get_db() as (conn, c):
        c.execute("SELECT password FROM users WHERE gitlab_id = ?", (gitlab_id,))
        row = c.fetchone()
        if row:
            logger.info(f"Retrieved password for registry secret for '{username}'")
            return row["password"]
    logger.error(f"No password found in DB for '{username}' (gitlab_id={gitlab_id})")
    return None
