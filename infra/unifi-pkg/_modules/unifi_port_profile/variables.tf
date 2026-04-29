variable "port_profiles" {
  description = <<-EOT
    Map of port profile config key to port profile config object. Each object supports:
      name         (string, required)
      forward      (string, required) – "all" | "native" | "customize" | "disabled"
      native_vlan  (string, optional) – key in var.network_ids for the untagged VLAN
      tagged_vlans (list(string), optional) – list of keys in var.network_ids to tag

    Profiles with forward="customize" and tagged_vlans set will trigger the VLAN
    patch script via null_resource.vlan_patch after creation, because UniFi 10.x
    silently drops tagged_networkconf_ids on POST/PUT.
  EOT
  type    = any
  default = {}
}

variable "network_ids" {
  description = "Map of VLAN key to UniFi network resource ID (from network unit output)"
  type        = map(string)
  default     = {}
}

variable "unifi_api_url" {
  description = "Base URL of the UniFi controller, used by the VLAN patch script (e.g., https://192.168.2.1)"
  type        = string
}

variable "unifi_username" {
  description = "Admin username for the UniFi controller, used by the VLAN patch script"
  type        = string
  sensitive   = true
}

variable "unifi_password" {
  description = "Admin password for the UniFi controller, used by the VLAN patch script"
  type        = string
  sensitive   = true
}
