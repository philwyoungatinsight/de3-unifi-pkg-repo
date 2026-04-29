# One resource block manages all port profiles defined in config.
# To add or remove a profile, edit the port_profiles map in infra/unifi-pkg/_config/unifi-pkg.yaml.
#
# NOTE: Two provider/firmware limitations apply to port profiles on UniFi 7.4+:
#   1. PUT (update) returns "not found" — updates always fail.
#   2. The GET response does not include tagged_networkconf_ids — so after every
#      apply Terraform sees drift and tries to PUT (which then fails per #1).
#
# Workaround: ignore_changes = all suppresses both failures. The correct values
# ARE sent on the initial CREATE (POST), so a newly-created or re-created profile
# will have the right VLANs. To pick up config changes, taint the resource and
# re-apply (terragrunt destroy + apply for this unit).
resource "unifi_port_profile" "this" {
  for_each = var.port_profiles

  name    = each.value.name
  forward = each.value.forward

  # native_vlan and tagged_vlans are config keys that map to network resource IDs.
  # try() gracefully handles the empty-map case during destroy when
  # the network dependency uses mock outputs (network_ids = {}).
  native_networkconf_id = try(var.network_ids[each.value.native_vlan], null)
  tagged_networkconf_ids = [
    for vlan_key in try(each.value.tagged_vlans, []) : var.network_ids[vlan_key]
    if can(var.network_ids[vlan_key])
  ]

  lifecycle {
    ignore_changes = all
  }
}

# Patch tagged VLANs on all "customize" port profiles in a single authenticated
# session to avoid the UniFi UDM login rate-limiter (429 / AUTHENTICATION_FAILED_
# LIMIT_REACHED) that fires when multiple scripts authenticate back-to-back.
#
# All profiles that need patching are serialised into PROFILES_JSON and sent to
# a single Python script invocation. The script logs in once, fetches networks
# once, and patches every profile in sequence without re-authenticating.
locals {
  _customize_profiles = {
    for k, v in var.port_profiles : k => v
    if try(v.forward, "") == "customize" && length(try(v.tagged_vlans, [])) > 0
  }
}

resource "null_resource" "vlan_patch" {
  count = length(local._customize_profiles) > 0 ? 1 : 0

  triggers = {
    profiles_hash = sha256(jsonencode({
      for k, v in local._customize_profiles : k => {
        id           = unifi_port_profile.this[k].id
        native_vlan  = try(v.native_vlan, "")
        tagged_vlans = join(",", sort(try(v.tagged_vlans, [])))
      }
    }))
    network_ids = sha256(jsonencode(var.network_ids))
  }

  provisioner "local-exec" {
    command = "python3 ${path.module}/scripts/patch-port-profile-vlans.py"
    environment = {
      UNIFI_URL      = var.unifi_api_url
      UNIFI_USERNAME = var.unifi_username
      UNIFI_PASSWORD = var.unifi_password
      PROFILES_JSON = jsonencode([
        for k, v in local._customize_profiles : {
          profile_id    = unifi_port_profile.this[k].id
          native_net_id = try(var.network_ids[v.native_vlan], "")
          tagged_net_ids = join(",", [
            for vlan in try(v.tagged_vlans, []) : var.network_ids[vlan]
            if can(var.network_ids[vlan])
          ])
        }
      ])
    }
  }

  depends_on = [unifi_port_profile.this]
}

# Pre-destroy cleanup: clear portconf_id references from all device ports.
#
# The paultyng/unifi provider's forget_on_destroy=false on unifi_device resources
# means device destroy removes the resource from state but makes no API call.
# Switch ports therefore still have portconf_id set on the controller, which causes
# the subsequent unifi_port_profile DELETE to fail with api.err.ObjectReferredByDevice.
#
# Destroy order: because this resource depends_on unifi_port_profile.this, Terraform
# destroys it FIRST on a destroy run, running the local-exec BEFORE the API DELETE
# of the port profiles. This clears the references, unblocking the DELETE.
#
# NOTE: credentials are stored in triggers so they are available to self.triggers
# in the destroy provisioner (at destroy time the module inputs are gone).
# This is the standard Terraform pattern for destroy-time provisioners that need
# values from the apply phase.
resource "null_resource" "pre_destroy_clear_overrides" {
  triggers = {
    profile_ids    = jsonencode({ for k, v in unifi_port_profile.this : k => v.id })
    unifi_api_url  = var.unifi_api_url
    unifi_username = var.unifi_username
    unifi_password = var.unifi_password
  }

  provisioner "local-exec" {
    when    = destroy
    command = "python3 ${path.module}/scripts/clear-port-overrides.py"
    environment = {
      UNIFI_URL        = self.triggers.unifi_api_url
      UNIFI_USERNAME   = self.triggers.unifi_username
      UNIFI_PASSWORD   = self.triggers.unifi_password
      PORT_PROFILE_IDS = self.triggers.profile_ids
    }
  }

  depends_on = [unifi_port_profile.this]
}
