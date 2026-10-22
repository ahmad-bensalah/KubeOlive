# Platform Scripts & Operations Tooling

Automated operational tools for MLOps deployment and cluster lifecycle management.

---

## 1. Scripts Overview

| Script | Purpose | Execution Context |
|:---|:---|:---|
| **[`deploy-mlflow.sh`](deploy-mlflow.sh)** | Deploys Charmed MLflow with MariaDB, connects to MinIO object storage, and integrates into Kubeflow dashboard links | Run on `juju-controller` VM |
| **[`cluster-recovery.sh`](cluster-recovery.sh)** | Self-healing recovery runbook that cleans stuck/ghost pods, restarts failed CSI daemons, and verifies health after reboot | Run locally or from control plane |

---

## 2. Usage

### Deploying Charmed MLflow
```bash
chmod +x scripts/deploy-mlflow.sh
./scripts/deploy-mlflow.sh
```

### Recovering Cluster After Power Loss
```bash
chmod +x scripts/cluster-recovery.sh
./scripts/cluster-recovery.sh
```
