variable "proxmox_api_url" {
  description = "Proxmox API endpoint"
  type        = string
}

variable "proxmox_api_token" {
  description = "Proxmox API token in format user@realm!tokenid=secret"
  type        = string
  sensitive   = true
}

variable "proxmox_ssh_password" {
  description = "Proxmox root SSH password for bpg provider"
  type        = string
  sensitive   = true
}

variable "worker_count" {
  description = "Number of Kubernetes worker nodes"
  type        = number
  default     = 2
}

variable "proxmox_node" {
  description = "Proxmox node name"
  type        = string
  default     = "r7525g"
}

variable "template_id" {
  description = "VMID of the cloud-init template"
  type        = number
  default     = 9000
}

variable "ssh_public_keys" {
  description = "SSH public keys to inject into VMs"
  type        = string
}

variable "vm_user" {
  description = "Default cloud-init user"
  type        = string
  default     = "ubuntu"
}

variable "vm_password" {
  description = "Default cloud-init password"
  type        = string
  sensitive   = true
  default     = "ubuntu"
}

variable "network_gateway" {
  description = "Default gateway for VMs"
  type        = string
  default     = "10.200.11.1"
}

variable "dns_server" {
  description = "DNS server for VMs"
  type        = string
  default     = "8.8.8.8"
}
