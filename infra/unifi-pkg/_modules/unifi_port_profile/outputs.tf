# Map of port profile config key -> UniFi port profile resource ID.
# Consumed by the device unit via a Terragrunt dependency block.
output "port_profile_ids" {
  description = "Map of port profile key to UniFi port profile resource ID"
  value       = { for k, v in unifi_port_profile.this : k => v.id }
}
