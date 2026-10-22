# Deployment Walkthrough — Terraform + Ansible on New Proxmox

This guide covers everything you need to do when deploying the stack on a new
Proxmox host with new IPs. The architecture is:

```
Terraform  →  creates 4 VMs on Proxmox  →  writes ansible/inventory.ini
Ansible    →  reads inventory.ini + group_vars/all.yml  →  configures everything
```

---

## Architecture

| VM | Hostname | Role | Default IP |
|---|---|---|---|
| Control plane | `controlplane` | MicroK8s master | `10.110.188.144` |
| Worker 1 | `node01` | MicroK8s worker | `10.110.188.145` |
| Worker 2 | `node02` | MicroK8s worker | `10.110.188.146` |
| Juju VM | `juju-controller` | Juju + Charmed Kubeflow | `10.110.188.147` |
| GitLab VM | `gitlab` | GitLab CE + Admin Portal | `10.110.188.148` |
| MetalLB pool | — | LoadBalancer IPs (Kubeflow ingress) | `10.110.188.200–207` |

---

## Prerequisites

- New Proxmox host with API access
- A Proxmox API token with VM creation permissions (`root@pam!terraform=<secret>`)
- A cloud-init Ubuntu 24.04 template on the new Proxmox (default VMID `9000`)
  - Must have `qemu-guest-agent` installed
  - Must have cloud-init enabled
  - Must use `scsi0` as the disk interface
- Terraform >= 1.0 installed on your machine
- Ansible >= 2.14 installed on your machine
- Your SSH private key at `~/.ssh/pfa_bouri_terraform`

---

## Step 1 — Update Terraform for the new Proxmox

Edit **`terraform/terraform.tfvars`**. This is the only Terraform file you need to change.

```hcl
# New Proxmox connection
proxmox_api_url      = "https://<NEW_PROXMOX_IP>:8006/api2/json"
proxmox_api_token    = "root@pam!terraform=<NEW_TOKEN>"
proxmox_ssh_password = "<NEW_ROOT_PASSWORD>"
proxmox_node         = "<NEW_NODE_NAME>"      # e.g. "proxmox-prod"

# New network
network_gateway      = "<NEW_GATEWAY>"         # e.g. "192.168.1.1"
dns_server           = "8.8.8.8"

# Your SSH public key — injected into all VMs via cloud-init
ssh_public_keys = "ssh-ed25519 AAAA... your-key"
```

---

## Step 2 — Update VM IPs in `main.tf`

The IPs are hardcoded in `terraform/main.tf` inside each VM's `ip_config` block.
Update all five to your new subnet. Also update the subnet mask if your new
network is not `/22`.

| Resource | Block to find | Change |
|---|---|---|
| `k8s_control` | `ip_config → ipv4 → address` | `<NEW_CP_IP>/mask` |
| `k8s_worker[0]` | `ip_config → ipv4 → address` | `<NEW_W1_IP>/mask` |
| `k8s_worker[1]` | `ip_config → ipv4 → address` | `<NEW_W2_IP>/mask` |
| `juju_controller` | `ip_config → ipv4 → address` | `<NEW_JUJU_IP>/mask` |
| `gitlab_server` | `ip_config → ipv4 → address` | `<NEW_GITLAB_IP>/mask` |

> Terraform will automatically write the new IPs into `ansible/inventory.ini`
> after `apply` via `inventory.tf` + `inventory.tpl`. You never edit
> `inventory.ini` by hand.

---

## Step 3 — Update the Ansible source of truth

Edit **`ansible/group_vars/all.yml`**. This is the **only Ansible file** you touch.
All roles and the playbook read from these variables — nothing else needs changing.

```yaml
# --- VM IPs ---
controlplane:
  name: controlplane
  ip:   "<NEW_CONTROLPLANE_IP>"

workers:
  - name: node01
    ip:   "<NEW_WORKER1_IP>"
  - name: node02
    ip:   "<NEW_WORKER2_IP>"

gitlab_ip:             "<NEW_GITLAB_IP>"

# --- Network ---
vm_subnet:             "<NEW_SUBNET>/24"         # e.g. "192.168.1.0/24"
                                                 # all VMs must be on this subnet

# --- MetalLB ---
# Pick a free range on the same subnet, outside your VM IPs,
# and not served by your DHCP server.
metallb_ip_range:      "<LB_START>-<LB_END>"    # e.g. "192.168.1.200-192.168.1.207"
kubeflow_dashboard_ip: "<LB_START>"              # first IP of the pool above
```

> `gitlab_base_url` and `kubeflow_dashboard_url` are derived automatically.
> Do not edit them.

---

## Step 4 — Verify the Proxmox template

Terraform clones from the VMID set in `template_id` (default `9000`).
On the new Proxmox you need a matching cloud-init template.

To create one if it does not exist:

```bash
# On the Proxmox host — download Ubuntu 24.04 cloud image and create template
wget https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

qm create 9000 --name ubuntu-2404-template --memory 2048 --cores 2 --net0 virtio,bridge=vmbr0
qm importdisk 9000 noble-server-cloudimg-amd64.img local-lvm
qm set 9000 --scsihw virtio-scsi-pci --scsi0 local-lvm:vm-9000-disk-0
qm set 9000 --ide2 local-lvm:cloudinit
qm set 9000 --boot c --bootdisk scsi0
qm set 9000 --serial0 socket --vga serial0
qm set 9000 --agent enabled=1

# Install qemu-guest-agent inside the VM before converting to template
# Then convert:
qm template 9000
```

---

## Step 5 — Run Terraform

```bash
cd micro_k8s/terraform

# Download the bpg/proxmox provider (required on first run or new machine)
terraform init

# Preview what will be created — verify IPs and VM specs look correct
terraform plan

# Create the 4 VMs on Proxmox
terraform apply
```

After `apply` completes, `ansible/inventory.ini` is automatically written with
the correct IPs. Verify it looks right:

```bash
cat ../ansible/inventory.ini
```

---

## Step 6 — Verify SSH access

Before running Ansible, confirm you can reach all VMs:

```bash
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@<CONTROLPLANE_IP>
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@<WORKER1_IP>
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@<WORKER2_IP>
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@<JUJU_IP>
ssh -i ~/.ssh/pfa_bouri_terraform ubuntu@<GITLAB_IP>
```

If your SSH key lives at a different path, update `ssh_key` in
`terraform/inventory.tpl` and re-run `terraform apply` to regenerate
`inventory.ini`.

---

## Step 7 — Run Ansible

```bash
cd micro_k8s/ansible

# Full stack — all 7 plays in order
ansible-playbook playbook.yml
```

To run a single play after a failure, use tags:

```bash
ansible-playbook playbook.yml --tags common
ansible-playbook playbook.yml --tags master
ansible-playbook playbook.yml --tags worker
ansible-playbook playbook.yml --tags metallb
ansible-playbook playbook.yml --tags longhorn
ansible-playbook playbook.yml --tags gitlab
ansible-playbook playbook.yml --tags portal      # sub-task of gitlab
ansible-playbook playbook.yml --tags juju
```

---

## What Ansible does — play by play

| # | Tag | Hosts | What happens |
|---|---|---|---|
| 1 | `common` | all k8s nodes | Installs MicroK8s 1.31, configures kernel modules, disables swap, sets up DNS and `/etc/hosts` |
| 2 | `master` | controlplane | Initializes cluster, enables DNS and MetalLB addons, installs Helm, extracts kubeconfig to `configs/config` |
| 3 | `worker` | node01, node02 | Joins workers to the cluster one by one (serial: 1) |
| 4 | `metallb` | controlplane | Renders and applies `metallb-pool.yaml` — creates `IPAddressPool` and `L2Advertisement` CRs |
| 5 | `longhorn` | all k8s nodes | Installs open-iscsi on all nodes, deploys Longhorn via Helm on master, sets it as default StorageClass |
| 6 | `gitlab` | gitlab VM | Installs GitLab CE, creates Kubeflow Dex OAuth app, fetches credentials to `configs/gitlab_oauth_creds.json`, deploys Admin Portal |
| 7 | `juju` | juju VM | Bootstraps Juju controller in LXD, fixes cross-network routing to K8s nodes, deploys Charmed Kubeflow 1.10, configures Dex with GitLab OIDC and static admin credentials |

---

## Auto-generated files — do not edit manually

These files are written automatically during the deployment. Editing them
by hand will have no effect because they get overwritten.

| File | Written by | Content |
|---|---|---|
| `ansible/inventory.ini` | Terraform (`inventory.tf`) | All VM IPs and SSH config |
| `ansible/configs/config` | Ansible master role | MicroK8s kubeconfig |
| `ansible/configs/gitlab_oauth_creds.json` | Ansible gitlab role | GitLab OAuth `client_id` and `client_secret` |

---

## Timing expectations

| Phase | Expected duration |
|---|---|
| Terraform apply (VM creation + cloud-init) | 5–10 min |
| Play 1–3 (MicroK8s cluster) | 10–15 min |
| Play 5 (Longhorn) | 5–10 min |
| Play 6 (GitLab install + reconfigure) | 10–15 min |
| Play 7 (Charmed Kubeflow deploy) | 30–60 min |
| **Total** | **~60–90 min** |

---

## Accessing services after deployment

| Service | URL |
|---|---|
| Kubeflow dashboard | `http://<kubeflow_dashboard_ip>` |
| GitLab | `http://<gitlab_ip>` |
| Admin Portal | `http://<gitlab_ip>:7070` |
| Longhorn UI | `http://<any_worker_ip>:30080` |

Default Kubeflow login: `admin` / `kubeflow-admin-2024`

---

## Rotating the Admin Portal GitLab token

The Admin Portal uses a GitLab Personal Access Token (PAT) called
`kubeflow-admin-portal-token` to create GitLab users. It is created with a
**365-day lifetime**. When it expires, the portal will return `401 Unauthorized`
errors when trying to create users.

### How to know it has expired

```bash
# On the GitLab VM — check token expiry
sudo gitlab-rails runner "
  token = User.find_by_username('root')
             .personal_access_tokens
             .find_by(name: 'kubeflow-admin-portal-token')
  puts token ? \"Expires: #{token.expires_at} | Revoked: #{token.revoked?}\" : 'Token not found'
"
```

Or simply — if the Admin Portal UI returns errors when creating users, the
token has expired.

### How to rotate

Run this single command from your Ansible controller:

```bash
cd micro_k8s/ansible
ansible-playbook playbook.yml --tags rotate_token
```

This will:
1. Delete the old token from GitLab's database
2. Create a new one valid for another 365 days
3. Rewrite `/lib/systemd/system/kubeflow-admin-portal.service` with the new token
4. Reload systemd and restart the portal service
5. Confirm the service is back to `active`

The whole operation takes about 1 minute. No downtime to the rest of the
platform — only the Admin Portal restarts briefly.

### Set a calendar reminder

Token lifetime is hardcoded to 365 days in
`roles/gitlab/tasks/portal.yml` and `roles/gitlab/tasks/rotate_token.yml`.
Set a yearly reminder to run `--tags rotate_token` before it expires.

---

## Troubleshooting

**Terraform fails with "connection refused" on Proxmox API**
→ Check that `proxmox_api_url` is reachable and the token has the right permissions.

**SSH connection refused during Ansible**
→ VMs may still be booting. Wait 2–3 minutes and retry. Check that `qemu-guest-agent` is running inside the template.

**MetalLB IPs not being assigned**
→ Ensure the IP range in `metallb_ip_range` is on the same L2 subnet as your VMs, is not used by any DHCP server, and is not already assigned to another host.

**GitLab play hangs at "Reconfigure GitLab"**
→ Normal — `gitlab-ctl reconfigure` takes up to 10 minutes. The task has a 600s timeout. Wait it out.

**Juju charm stuck in "waiting" forever**
→ The routing fix (Play 7) may not have applied. Check that the `juju-route.service` is active on all K8s nodes:
```bash
systemctl status juju-route
ip route show | grep lxdbr0
```

**Dex "CSRF check failed" on GitLab login**
→ Verify `kubeflow_dashboard_ip` in `all.yml` matches the actual IP assigned to the Istio ingress `LoadBalancer` service:
```bash
kubectl get svc -n kubeflow istio-ingressgateway
```
