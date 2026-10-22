# 01 Infrastructure Provisioning with Terraform

## Overview

Everything starts here.  Terraform provisions five virtual machines on a
Proxmox hypervisor by cloning a pre-built Ubuntu cloud-init template.  After
`terraform apply`, the machines exist with static IPs, SSH keys injected, and
the Ansible inventory auto-generated.

**Files:**
```
micro_k8s/terraform/
├── providers.tf       # Proxmox + local provider config
├── variables.tf       # Input variable declarations
├── terraform.tfvars   # Concrete values (never commit to public repos)
├── main.tf            # VM resource definitions
├── inventory.tf       # Generates ansible/inventory.ini
└── inventory.tpl      # Template for the inventory file
```

---

## Provider Configuration (`providers.tf`)

```hcl
provider "proxmox" {
  endpoint  = var.proxmox_api_url     # https://10.110.188.118:8006/api2/json
  api_token = var.proxmox_api_token   # root@pam!terraform=<uuid>
  insecure  = true                    # self-signed cert on Proxmox

  ssh {
    agent    = false
    username = "root"
    password = var.proxmox_ssh_password  # needed for cloud-init disk ops
  }
}
```

Two providers are used:
- **`bpg/proxmox`** — creates/manages Proxmox VMs.  Requires both an API token
  (for REST API calls) and SSH credentials (for operations that the API cannot
  do, like writing cloud-init ISOs directly to the storage backend).
- **`hashicorp/local`** — writes the Ansible inventory file to disk.

---

## Variables (`variables.tf` + `terraform.tfvars`)

| Variable | Value | Purpose |
|---|---|---|
| `proxmox_api_url` | `https://10.110.188.118:8006/api2/json` | Proxmox REST API |
| `proxmox_node` | `proxmox-kube` | Target Proxmox node name |
| `template_id` | `9000` | Source VM to clone (Ubuntu cloud-init template) |
| `worker_count` | `2` | How many worker nodes to create |
| `vm_user` | `ubuntu` | Cloud-init default user |
| `network_gateway` | `10.110.188.1` | LAN gateway |
| `dns_server` | `8.8.8.8` | DNS for all VMs |
| `ssh_public_keys` | three keys | Injected into every VM via cloud-init |

---

## VM Definitions (`main.tf`)

All VMs clone from template `9000` (a pre-built Ubuntu 24.04 image with
cloud-init and QEMU guest agent already installed).  Full clones are used
(`full = true`) so each VM is completely independent.

### Control Plane
```hcl
vm_id = 220
name  = "microk8s-control"
cpu { cores = 8 }
memory { dedicated = 10240 }    # 10 GB
disk { size = 100 }             # 100 GB SSD
ip = "10.110.188.144/22"
```

### Worker Nodes (count-driven)
```hcl
count = var.worker_count        # 2 workers
vm_id = 221 + count.index       # 221, 222
cpu { cores = 10 }
memory { dedicated = 12288 }    # 12 GB per worker
disk { size = 150 }             # 150 GB SSD
ip = "10.110.188.${145 + count.index}/22"
```

### Juju Controller VM
```hcl
vm_id = 223
name  = "juju-controller"
cpu { cores = 4 }
memory { dedicated = 4096 }    # 4 GB
disk { size = 50 }
ip = "10.110.188.147/22"
```

### GitLab Server
```hcl
vm_id = 224
name  = "gitlab-server"
cpu { cores = 4 }
memory { dedicated = 8192 }    # 8 GB
disk { size = 60 }
ip = "10.110.188.148/22"
```

All VMs share these settings:
- `cpu.type = "host"` — passes through host CPU flags (SSE4, AVX) for performance
- `network_device.bridge = "vmbr0"` — connects to the main LAN bridge
- `agent.enabled = true` — QEMU guest agent for graceful shutdown + IP reporting
- `on_boot = true` — VM starts automatically when Proxmox boots

---

## Automatic Inventory Generation (`inventory.tf` + `inventory.tpl`)

After creating the VMs, Terraform extracts their IPs and renders the Ansible
inventory file directly:

```hcl
# inventory.tf
resource "local_file" "ansible_inventory" {
  filename = "${path.module}/../ansible/inventory.ini"
  content  = templatefile("inventory.tpl", {
    controlplane_ip    = split("/", k8s_control.initialization[0].ip_config[0].ipv4[0].address)[0]
    worker_ips         = [ for vm in k8s_worker: split("/", vm...address)[0] ]
    juju_controller_ip = split("/", juju_controller...address)[0]
    gitlab_ip          = split("/", gitlab_server...address)[0]
    vm_user            = var.vm_user
    ssh_key            = "~/.ssh/pfa_bouri_terraform"
  })
}
```

The template (`inventory.tpl`) produces a file like:
```ini
[master]
controlplane ansible_host=10.110.188.144

[worker]
node01 ansible_host=10.110.188.145
node02 ansible_host=10.110.188.146

[juju_machine]
juju-controller ansible_host=10.110.188.147

[gitlab_machine]
gitlab ansible_host=10.110.188.148

[all:vars]
ansible_user=ubuntu
ansible_python_interpreter=/usr/bin/python3
ansible_ssh_private_key_file=~/.ssh/pfa_bouri_terraform
```

This means you never manually write the inventory — it is always in sync with
the actual Terraform state.

---

## How to Run

```bash
cd micro_k8s/terraform

# 1. Initialise providers
terraform init

# 2. Preview what will be created
terraform plan

# 3. Apply (creates all 5 VMs + inventory.ini)
terraform apply

# To destroy everything
terraform destroy
```

**Prerequisites:**
- Proxmox API token with VM create/modify permissions
- Cloud-init template VM (ID 9000) already exists on Proxmox
- SSH key `~/.ssh/pfa_bouri_terraform` on the machine running terraform

---

## Key Design Decisions

| Decision | Reason |
|---|---|
| Static IPs via cloud-init | Avoids DHCP race conditions during Ansible provisioning |
| Full clone (not linked) | Each VM is fully independent — no shared disk dependency |
| `cpu.type = "host"` | Required for KVM nested virtualisation (LXD inside Juju VM) |
| Worker count as variable | Cluster can be scaled horizontally by incrementing `worker_count` |
| Inventory auto-generation | Terraform state → Ansible inventory, single source of truth |
