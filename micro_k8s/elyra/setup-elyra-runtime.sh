#!/bin/bash

RUNTIME_DIR="/home/jovyan/.local/share/jupyter/metadata/runtimes"
RUNTIME_FILE="$RUNTIME_DIR/default.json"

NAMESPACE=$(cat /var/run/secrets/kubernetes.io/serviceaccount/namespace 2>/dev/null || echo "default")
mkdir -p "$RUNTIME_DIR"

cat > "$RUNTIME_FILE" <<EOF
{
  "schema_name": "kfp",
  "display_name": "default",
  "metadata": {
    "api_endpoint": "http://ml-pipeline.kubeflow.svc.cluster.local:8888",
    "user_namespace": "${NAMESPACE}",
    "engine": "Argo",
    "auth_type": "KUBERNETES_SERVICE_ACCOUNT_TOKEN",
    "cos_endpoint": "${ELYRA_COS_ENDPOINT:-http://minio:9000}",
    "cos_bucket": "${ELYRA_COS_BUCKET:-mlpipeline}",
    "cos_username": "${ELYRA_COS_USERNAME}",
    "cos_password": "${ELYRA_COS_PASSWORD}",
    "cos_auth_type": "USER_CREDENTIALS"
  }
}
EOF

# Hand off to the original s6 entrypoint
exec /init "$@"
