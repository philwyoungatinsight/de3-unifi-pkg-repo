variable "domain_name" {
  description = "DHCP domain suffix assigned to all VLANs managed by this module"
  type        = string
  default     = "homelab.local"
}

variable "unifi_api_url" {
  description = "Base URL of the UniFi controller, used by the pre-destroy cleanup script"
  type        = string
}

variable "unifi_username" {
  description = "Admin username for the UniFi controller, used by the pre-destroy cleanup script"
  type        = string
  sensitive   = true
}

variable "unifi_password" {
  description = "Admin password for the UniFi controller, used by the pre-destroy cleanup script"
  type        = string
  sensitive   = true
}

variable "vlans" {
  description = <<-EOT
    Map of VLAN config key to VLAN config object. Each object supports:
      name                         (string, required)
      purpose                      (string, required) – "corporate" | "guest" | "vlan-only"
      vlan_id                      (number, required)
      subnet                       (string, required) – CIDR notation
      dhcp_start                   (string)
      dhcp_stop                    (string)
      dhcp_enabled                 (bool, default false)
      dns_servers                  (list(string), optional)
      internet_access_enabled      (bool, default true)
      intra_network_access_enabled (bool, default true)
      dhcpd_boot_enabled           (bool, default false)
      dhcpd_boot_server            (string, optional)
      dhcpd_boot_filename          (string, optional)
  EOT
  type    = any
  default = {}
}

variable "fixed_clients" {
  description = <<-EOT
    Map of client name to static DHCP reservation. Each object supports:
      name        (string, required) – friendly label shown in UniFi UI
      mac         (string, required) – MAC address (colon-separated, lowercase)
      fixed_ip    (string, required) – IP to assign; must be within the VLAN subnet
      network_key (string, required) – key in var.vlans for the target network
  EOT
  type    = any
  default = {}
}
