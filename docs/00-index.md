# MLOps Platform — Technical Documentation Index

This repository contains the full infrastructure, automation, and platform
code for a self-hosted Kubeflow MLOps platform on Proxmox VE. The technical guides
below cover every layer, from bare-metal VM provisioning to distributed storage,
multi-tenant identity, and automated pipeline execution.

---

## Documentation Roadmap

| # | File | Topic | Focus Area |
|---|---|---|---|
| 01 | [01-infrastructure-terraform.md](01-infrastructure-terraform.md) | Proxmox VM Provisioning | Cloud-Init, VirtIO, Terraform specs |
| 02 | [02-cluster-ansible.md](02-cluster-ansible.md) | MicroK8s Cluster Automation | Multi-node cluster, MetalLB, CNI, Calico |
| 03 | [03-storage-longhorn.md](03-storage-longhorn.md) | Longhorn Distributed Storage | ReadWriteMany, volume replication, backup |
| 04 | [04-gitlab-oidc.md](04-gitlab-oidc.md) | GitLab CE as OIDC Provider | OAuth2 applications, Dex integration, SSO |
| 05 | [05-kubeflow-juju.md](05-kubeflow-juju.md) | Charmed Kubeflow Deployment | Juju charms, relations, pipeline engine |
| 06 | [06-elyra-notebook.md](06-elyra-notebook.md) | Elyra Notebook Runtime | PodDefault injection, auto-config, S3 sync |
| 07 | [07-admin-portal.md](07-admin-portal.md) | Kubeflow Admin Portal | FastAPI service, profile creation, onboarding |
| 08 | [08-resource-quotas.md](08-resource-quotas.md) | Resource Quota Groups | Guaranteed vs Best-Effort tier enforcement |
| 09 | [09-mlops-pipeline.md](09-mlops-pipeline.md) | End-to-End ML Application | KFP pipeline, MLflow tracking, KServe model |
| 10 | [10-recovery.md](10-recovery.md) | Cluster Recovery Runbook | Ghost pod cleanup, storage healing after reboot |
| 11 | [11-minio-per-user-isolation.md](11-minio-per-user-isolation.md) | MinIO Per-User Isolation | Dedicated tenant pods, isolated S3 credentials |
| — | [COMPLETE-TECHNICAL-WALKTHROUGH.md](COMPLETE-TECHNICAL-WALKTHROUGH.md) | Complete Platform Manual | Full end-to-end technical reference |

---

## Platform at a Glance

```
Proxmox Hypervisor (10.200.11.114 / 10.110.188.118)
│
├── microk8s-control   10.200.11.199   8 vCPU  10 GB RAM  100 GB
├── microk8s-worker-1  10.200.11.200  10 vCPU  12 GB RAM  150 GB
├── microk8s-worker-2  10.200.11.202  10 vCPU  12 GB RAM  150 GB
├── juju-controller    10.200.11.80    4 vCPU   4 GB RAM   50 GB
└── gitlab-server      10.200.11.203   4 vCPU   8 GB RAM   60 GB

Kubernetes (MicroK8s 1.31)
├── Longhorn           distributed block storage  (NodePort :30080)
├── MetalLB            L2 load balancer           (pool: .81–.85 / .200–.207)
├── Charmed Kubeflow   ML platform                (LoadBalancer ingress)
│   ├── Dex (OIDC)     → GitLab OAuth2
│   ├── Kubeflow Pipelines + MinIO (per-user)
│   ├── Notebooks (JupyterLab + Elyra)
│   └── Katib / KServe / MLflow
└── Admin Portal       FastAPI on GitLab VM       (:7070)
```
