variable "devices" {
  description = <<-EOT
    Map of device config key to device config object. Each object supports:
      mac            (string, required) – device MAC address
      name           (string, required)
      type           (string, optional) – "switch" (default) | "gateway"
      port_overrides (map, optional)    – keyed by port number (string), each entry:
        name             (string)
        port_profile     (string) – key in var.port_profile_ids, or "" for default

    Devices with type="gateway" (e.g. UDM) have ignore_changes = all because
    gateway devices reject all REST API writes with 400 Invalid. They are tracked
    in state for inventory purposes only; manage them via the controller UI.
  EOT
  type    = any
  default = {}
}

variable "port_profile_ids" {
  description = "Map of port profile key to UniFi port profile resource ID (from port-profile unit output)"
  type        = map(string)
  default     = {}
}

variable "unifi_api_url" {
  description = "UniFi controller base URL (e.g. https://192.168.2.1). Used by the port_override_patch null_resource for direct API calls."
  type        = string
  default     = ""
}

variable "unifi_username" {
  description = "UniFi admin username for direct API calls by port_override_patch."
  type        = string
  default     = ""
}

variable "unifi_password" {
  description = "UniFi admin password for direct API calls by port_override_patch."
  type        = string
  default     = ""
  sensitive   = true
}
