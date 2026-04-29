# One resource block manages all VLANs defined in config.
# To add or remove a VLAN, edit the vlans map in infra/unifi-pkg/_config/unifi-pkg.yaml — no
# Terraform code changes needed.
#
# NOTE: VLAN 1 (default/home) is managed by UniFi internally and cannot be
# imported or managed via this provider. Configure it via the controller UI.
resource "unifi_network" "this" {
  for_each = var.vlans

  name    = each.value.name
  purpose = each.value.purpose

  subnet       = each.value.subnet
  vlan_id      = each.value.vlan_id
  dhcp_start   = try(each.value.dhcp_start, null)
  dhcp_stop    = try(each.value.dhcp_stop, null)
  dhcp_enabled = try(each.value.dhcp_enabled, false)

  dhcp_dns    = try(each.value.dns_servers, null)
  domain_name = var.domain_name

  # internet_access_enabled controls whether the network can reach the internet.
  # intra_network_access_enabled controls whether devices on the same VLAN can
  # talk to each other (set false to isolate clients within the network).
  internet_access_enabled      = try(each.value.internet_access_enabled, true)
  intra_network_access_enabled = try(each.value.intra_network_access_enabled, true)

  # DHCP network boot (PXE). All three must be set together.
  dhcpd_boot_enabled  = try(each.value.dhcpd_boot_enabled, false)
  dhcpd_boot_server   = try(each.value.dhcpd_boot_server, null)
  dhcpd_boot_filename = try(each.value.dhcpd_boot_filename, null)

  # These attributes are set on the live controller before Terraform management
  # and are not returned by the provider, so ignoring them prevents spurious drift.
  lifecycle {
    ignore_changes = [
      dhcp_v6_start,
      dhcp_v6_stop,
      ipv6_pd_start,
      ipv6_pd_stop,
      ipv6_ra_priority,
    ]
  }
}

# Pre-destroy cleanup: remove managed network IDs from device port_override
# excluded_networkconf_ids before the networks themselves are deleted.
#
# When the vlan_patch provisioner in unifi-port-profile sets tagged_vlan_mgmt=custom
# on a port profile, the controller reflects those excluded_networkconf_ids onto
# device port_overrides. Even after port profiles and device resources are destroyed,
# those network ID references can remain on physical devices, causing network DELETE
# to fail with api.err.ResourceReferredBy.
#
# Destroy order: because this resource depends_on unifi_network.this, Terraform
# destroys it FIRST, running the local-exec cleanup BEFORE the network API DELETEs.
#
# NOTE: credentials stored in triggers — standard pattern for destroy-time
# provisioners that need apply-phase values (see port-profile module for details).
resource "null_resource" "pre_destroy_clear_excluded" {
  triggers = {
    network_ids    = jsonencode({ for k, v in unifi_network.this : k => v.id })
    unifi_api_url  = var.unifi_api_url
    unifi_username = var.unifi_username
    unifi_password = var.unifi_password
  }

  provisioner "local-exec" {
    when    = destroy
    command = "python3 ${path.module}/scripts/clear-excluded-refs.py"
    environment = {
      UNIFI_URL    = self.triggers.unifi_api_url
      UNIFI_USERNAME = self.triggers.unifi_username
      UNIFI_PASSWORD = self.triggers.unifi_password
      NETWORK_IDS  = self.triggers.network_ids
    }
  }

  depends_on = [unifi_network.this]
}

# Static DHCP reservations (fixed IP per MAC address).
# network_key must match a key in var.vlans so the network_id can be resolved.
resource "unifi_user" "fixed" {
  for_each = var.fixed_clients

  name       = each.value.name
  mac        = each.value.mac
  fixed_ip   = each.value.fixed_ip
  network_id = unifi_network.this[each.value.network_key].id

  depends_on = [unifi_network.this]
}
