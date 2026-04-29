locals {
  switches = {
    for k, v in var.devices : k => v
    if try(v.type, "switch") == "switch"
  }
}

# NOTE: Gateway devices (type="gateway", e.g. UDM) are NOT managed here.
# The paultyng/unifi provider issues a PUT on resource CREATE, which the UDM
# and similar gateways reject with api.err.Invalid (400). The resource would be
# tainted and fail on every apply. Configure gateways via the controller UI.

# Switch devices (type="switch" or type not set).
#
# Managed switches accept REST API writes for port overrides and name changes.
# To add or remove a switch, or change its port assignments, edit the devices
# map in infra/unifi-pkg/_config/unifi-pkg.yaml.
resource "unifi_device" "switches" {
  for_each = local.switches

  mac               = each.value.mac
  name              = each.value.name
  forget_on_destroy = false

  # Port overrides are NOT managed here. The paultyng/unifi provider v0.41 uses
  # an incorrect CSRF token approach for modern UDM firmware (reads X-CSRF-Token
  # header instead of TOKEN cookie), causing all PUT /rest/device calls to return
  # 403 Forbidden. Removing port_override blocks means Create() only sets name/mac
  # with no UpdateDevice PUT call, bypassing the CSRF issue.
  #
  # All port profile assignments are managed exclusively by null_resource.port_override_patch
  # below, which uses the correct TOKEN cookie CSRF approach.
  lifecycle {
    ignore_changes = all
  }
}

# Direct API patch for port overrides.
#
# The paultyng/unifi provider's Read() for unifi_device does not reliably detect
# drift in port_override configurations. When a switch reproes (e.g. after any
# UniFi config change), it may silently reset port assignments to the default
# profile without Terraform detecting the change — subsequent plans show "No
# changes" even though the switch is misconfigured.
#
# This null_resource re-pushes the intended portconf_id for every managed port on
# every apply by calling the UniFi REST API directly (same approach as vlan_patch
# in the port-profile module). It triggers when port config or profile IDs change.
resource "null_resource" "port_override_patch" {
  count = length(local.switches) > 0 && var.unifi_api_url != "" ? 1 : 0

  triggers = {
    # always_run forces this to run on every apply so drift is corrected even
    # when -refresh=false prevents the unifi_device resource from detecting it.
    always_run       = timestamp()
    overrides_hash   = sha256(jsonencode({
      for k, v in local.switches : k => {
        mac = v.mac
        port_overrides = {
          for port_num, po in try(v.port_overrides, {}) : port_num => po.port_profile
        }
      }
    }))
    profile_ids_hash = sha256(jsonencode(var.port_profile_ids))
  }

  provisioner "local-exec" {
    command = "python3 ${path.module}/scripts/patch-port-overrides.py"
    environment = {
      UNIFI_URL      = var.unifi_api_url
      UNIFI_USERNAME = var.unifi_username
      UNIFI_PASSWORD = var.unifi_password
      SWITCHES_JSON  = jsonencode([
        for k, v in local.switches : {
          mac  = v.mac
          name = v.name
          port_overrides = [
            for port_num, po in try(v.port_overrides, {}) : {
              number      = tonumber(port_num)
              name        = try(po.name, "")
              portconf_id = po.port_profile != "" ? try(var.port_profile_ids[po.port_profile], "") : ""
            }
          ]
        }
      ])
    }
  }

  depends_on = [unifi_device.switches]
}
