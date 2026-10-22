"""
Application entry point.

Assembles the FastAPI app, mounts static files, registers routers,
and handles startup lifecycle events.
"""
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.config import GITLAB_TOKEN, INDEX_HTML, STATIC_DIR, logger
from app.database import init_db
from app.routes import health, quota_groups, users

app = FastAPI(title="Charmed Kubeflow Multi-User Admin Portal")

# Static assets (favicon, logo, …)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# API routers
app.include_router(users.router)
app.include_router(health.router)
app.include_router(quota_groups.router)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@app.on_event("startup")
def startup() -> None:
    if not GITLAB_TOKEN:
        logger.error(
            "GITLAB_TOKEN environment variable is not set! API calls will fail."
        )
    init_db()
    try:
        from app.kubernetes import ensure_admin_portal_ingress_proxy, patch_decorator_controller_if_needed
        ensure_admin_portal_ingress_proxy()
        # Stop kfp-profile-controller (Metacontroller) from rotating
        # mlpipeline-minio-artifact in user namespaces on every restart.
        # Without this patch, Metacontroller regenerates random credentials
        # after each Kubeflow restart, breaking per-user MinIO pods and
        # notebook PodDefault credential injection.
        patch_decorator_controller_if_needed()
    except Exception as exc:
        logger.error(f"Failed to initialize Admin Portal ingress proxy: {exc}")


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def get_index():
    """Serve the single-page admin portal frontend."""
    try:
        with open(INDEX_HTML, "r") as fh:
            return fh.read()
    except Exception as exc:
        return f"<h3>Admin Portal Frontend missing: {exc}</h3>"
