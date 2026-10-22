# ─── Control Plane ───────────────────────────────────────────────────────────
resource "proxmox_virtual_environment_vm" "k8s_control" {
  node_name = var.proxmox_node
  vm_id     = 1220  # offset VM IDs slightly to avoid collisions with the old 200 VM ID
  name      = "microk8s-control"
  tags      = ["k8s", "control-plane", "microk8s"]

  clone {
    vm_id = var.template_id
    full  = true
  }

  cpu {
    cores   = 8
    sockets = 1
    type    = "host"
  }
 
  memory {
    dedicated = 10240
  }

  disk {
    interface    = "scsi0"
    size         = 100
    datastore_id = "TeamgroupR6_8x2Tb'"
    discard      = "on"
    ssd          = true
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  operating_system {
    type = "l26"
  }

  initialization {
    datastore_id = "TeamgroupR6_8x2Tb'"

    user_account {
      username = var.vm_user
      password = var.vm_password
      keys     = [var.ssh_public_keys]
    }

    ip_config {
      ipv4 {
        address = "10.200.11.199/24"
        gateway = var.network_gateway
      }
    }

    dns {
      servers = [var.dns_server]
    }
  }

  agent {
    enabled = true
  }

  serial_device {}

  vga {
    type = "serial0"
  }

  on_boot = true
  started = true
}

# ─── Worker Node 1 ───────────────────────────────────────────────────────────
resource "proxmox_virtual_environment_vm" "k8s_worker_1" {
  node_name = var.proxmox_node
  vm_id     = 1221
  name      = "microk8s-worker-1"
  tags      = ["k8s", "worker", "microk8s"]

  clone {
    vm_id = var.template_id
    full  = true
  }

  cpu {
    cores   = 10
    sockets = 1
    type    = "host"
  }

  memory {
    dedicated = 12288
  }

  disk {
    interface    = "scsi0"
    size         = 150
    datastore_id = "TeamgroupR6_8x2Tb'"
    discard      = "on"
    ssd          = true
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  operating_system {
    type = "l26"
  }

  initialization {
    datastore_id = "TeamgroupR6_8x2Tb'"

    user_account {
      username = var.vm_user
      password = var.vm_password
      keys     = [var.ssh_public_keys]
    }

    ip_config {
      ipv4 {
        address = "10.200.11.200/24"
        gateway = var.network_gateway
      }
    }

    dns {
      servers = [var.dns_server]
    }
  }

  agent {
    enabled = true
  }

  serial_device {}

  vga {
    type = "serial0"
  }

  on_boot = true
  started = true
}

# ─── Worker Node 2 ───────────────────────────────────────────────────────────
resource "proxmox_virtual_environment_vm" "k8s_worker_2" {
  node_name = var.proxmox_node
  vm_id     = 1222
  name      = "microk8s-worker-2"
  tags      = ["k8s", "worker", "microk8s"]

  clone {
    vm_id = var.template_id
    full  = true
  }

  cpu {
    cores   = 10
    sockets = 1
    type    = "host"
  }

  memory {
    dedicated = 12288
  }

  disk {
    interface    = "scsi0"
    size         = 150
    datastore_id = "TeamgroupR6_8x2Tb'"
    discard      = "on"
    ssd          = true
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  operating_system {
    type = "l26"
  }

  initialization {
    datastore_id = "TeamgroupR6_8x2Tb'"

    user_account {
      username = var.vm_user
      password = var.vm_password
      keys     = [var.ssh_public_keys]
    }

    ip_config {
      ipv4 {
        address = "10.200.11.202/24"
        gateway = var.network_gateway
      }
    }

    dns {
      servers = [var.dns_server]
    }
  }

  agent {
    enabled = true
  }

  serial_device {}

  vga {
    type = "serial0"
  }

  on_boot = true
  started = true
}

# ─── Juju Controller Machine ────────────────────────────────────────────────
resource "proxmox_virtual_environment_vm" "juju_controller" {
  node_name = var.proxmox_node
  vm_id     = 1223
  name      = "juju-controller"
  tags      = ["juju", "management"]

  clone {
    vm_id = var.template_id
    full  = true
  }

  cpu {
    cores   = 4
    sockets = 1
    type    = "host"
  }

  memory {
    dedicated = 4096
  }

  disk {
    interface    = "scsi0"
    size         = 50
    datastore_id = "TeamgroupR6_8x2Tb'"
    discard      = "on"
    ssd          = true
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  operating_system {
    type = "l26"
  }

  initialization {
    datastore_id = "TeamgroupR6_8x2Tb'"

    user_account {
      username = var.vm_user
      password = var.vm_password
      keys     = [var.ssh_public_keys]
    }

    ip_config {
      ipv4 {
        address = "10.200.11.80/24"
        gateway = var.network_gateway
      }
    }

    dns {
      servers = [var.dns_server]
    }
  }

  agent {
    enabled = true
  }

  serial_device {}

  vga {
    type = "serial0"
  }

  on_boot = true
  started = true
}

# ─── GitLab Server Machine ──────────────────────────────────────────────────
resource "proxmox_virtual_environment_vm" "gitlab_server" {
  node_name = var.proxmox_node
  vm_id     = 1224
  name      = "gitlab-server"
  tags      = ["gitlab", "auth"]

  clone {
    vm_id = var.template_id
    full  = true
  }

  cpu {
    cores   = 4
    sockets = 1
    type    = "host"
  }

  memory {
    dedicated = 8192
  }

  disk {
    interface    = "scsi0"
    size         = 60
    datastore_id = "TeamgroupR6_8x2Tb'"
    discard      = "on"
    ssd          = true
  }

  network_device {
    bridge = "vmbr0"
    model  = "virtio"
  }

  operating_system {
    type = "l26"
  }

  initialization {
    datastore_id = "TeamgroupR6_8x2Tb'"

    user_account {
      username = var.vm_user
      password = var.vm_password
      keys     = [var.ssh_public_keys]
    }

    ip_config {
      ipv4 {
        address = "10.200.11.203/24"
        gateway = var.network_gateway
      }
    }

    dns {
      servers = [var.dns_server]
    }
  }

  agent {
    enabled = true
  }

  serial_device {}

  vga {
    type = "serial0"
  }

  on_boot = true
  started = true
}

