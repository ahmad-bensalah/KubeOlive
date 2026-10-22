"""
Central configuration: environment variables, constants, and logging setup.
"""
import os
import logging

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("kubeflow-admin-portal")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DB_PATH = "/var/lib/portal-data/portal.db"
# Resolve paths dynamically so they work regardless of the VM username
_portal_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(_portal_dir, "static")
INDEX_HTML = os.path.join(_portal_dir, "index.html")

# ---------------------------------------------------------------------------
# GitLab
# ---------------------------------------------------------------------------
GITLAB_URL = "http://localhost"
GITLAB_TOKEN = os.getenv("GITLAB_TOKEN", "")
# Registry URL injected by the systemd service unit (set during Ansible provisioning).
# Format: http://<gitlab_ip>:5050
GITLAB_REGISTRY_URL = os.getenv("GITLAB_REGISTRY_URL", "http://localhost:5050")
MINIO_NODEPORT_URL = os.getenv("MINIO_NODEPORT_URL", "http://10.110.188.144:30900")
