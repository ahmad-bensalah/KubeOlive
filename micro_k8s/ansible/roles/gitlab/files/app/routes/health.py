"""
Health-check endpoint: GET /api/health
"""
import socket
import subprocess

from fastapi import APIRouter

from app.gitlab_client import gl_api

router = APIRouter(prefix="/api", tags=["health"])

# Longhorn UI address — adjust if the service IP / NodePort changes.
LONGHORN_HOST = "10.110.188.144"
LONGHORN_PORT = 30080


@router.get("/health")
def health():
    """
    Returns connectivity status for:
    - Kubernetes (kubectl client check)
    - GitLab API
    - Longhorn storage UI
    """
    # Kubernetes
    res = subprocess.run(["kubectl", "version", "--client"], capture_output=True)
    k8s_ok = res.returncode == 0

    # GitLab
    _, code = gl_api("GET", "/users?per_page=1")
    gitlab_ok = code == 200

    # Longhorn (TCP reachability check)
    longhorn_ok = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        longhorn_ok = sock.connect_ex((LONGHORN_HOST, LONGHORN_PORT)) == 0
        sock.close()
    except Exception:
        pass

    return {
        "status": "ok",
        "kubernetes_reachable": k8s_ok,
        "gitlab_reachable": gitlab_ok,
        "longhorn_reachable": longhorn_ok,
    }
