# Platform Engineering — MicroK8s, MLOps & Orchestration

The `micro_k8s` directory contains the automation, configuration manifests, runtime patches, and operational runbooks that build the Kubernetes foundation and MLOps runtime.

---

## 1. Directory Structure

```
micro_k8s/
├── terraform/               # Proxmox VM provisioning (bpg/proxmox provider)
├── ansible/                 # MicroK8s cluster formation & role configurations
├── elyra/                   # Custom Elyra visual pipeline image & auto-config
├── recovery/                # Automated cluster recovery runbooks (power-loss recovery)
├── DEPLOYMENT.md            # Step-by-step deployment guide for new environments
└── minio.md                 # Per-user MinIO architecture and tenant isolation guide
```

---

## 2. Core Components

### 🏗️ Infrastructure & Cluster Formation
* **[`terraform/`](terraform/)**: Declares 5 virtual machines on Proxmox VE (Control plane, 2 Workers, Juju controller, GitLab server) with cloud-init specifications.
* **[`ansible/`](ansible/)**: Orchestrates MicroK8s (v1.31) installation, Calico CNI, MetalLB L2 load balancer pool, Longhorn distributed block storage, and GitLab CE.

### 🧪 Interactive MLOps & Elyra Visual Pipelines
* **[`elyra/`](elyra/)**: Extends JupyterLab with the Elyra visual pipeline canvas:
  * `Dockerfile`: Container image packaging Elyra, Kubeflow Pipelines SDK, and S3 drivers.
  * `setup-elyra-runtime.sh`: Automates runtime creation without requiring manual UI configuration.
  * `patch_elyra.py`: Automatically synchronizes runtime credentials into user workspaces.

### 🛡️ Resilience & Recovery
* **[`recovery/`](recovery/)**: Automated recovery script (`recover.sh`) that monitors, detects, and heals the cluster after unexpected hypervisor reboots or power loss. Resolves ghost pods, mounts broken Longhorn volumes, and waits for deployment stabilization.

### 📚 Operational References
* **[`DEPLOYMENT.md`](DEPLOYMENT.md)**: Comprehensive guide for deploying this stack onto a brand-new Proxmox cluster with new subnets.
* **[`minio.md`](minio.md)**: Deep dive into the per-user MinIO storage isolation pattern that prevents multi-tenant data leaks.
