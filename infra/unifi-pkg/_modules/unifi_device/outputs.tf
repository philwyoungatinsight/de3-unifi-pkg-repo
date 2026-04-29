output "switch_ids" {
  description = "Map of switch device key to UniFi device resource ID"
  value       = { for k, v in unifi_device.switches : k => v.id }
}
