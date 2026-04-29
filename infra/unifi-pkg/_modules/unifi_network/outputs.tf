# Map of VLAN config key -> UniFi network resource ID.
# Consumed by the port-profile unit via a Terragrunt dependency block.
output "network_ids" {
  description = "Map of VLAN key to UniFi network resource ID"
  value       = { for k, v in unifi_network.this : k => v.id }
}
