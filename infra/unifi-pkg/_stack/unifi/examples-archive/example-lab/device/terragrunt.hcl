include "root" {
  path   = find_in_parent_folders("root.hcl")
  expose = true
}

# Port profile IDs from the port-profile unit — needed to map profile keys to IDs.
dependency "port_profile" {
  config_path = "../port-profile"
  mock_outputs = {
    port_profile_ids = {}
  }
  mock_outputs_allowed_terraform_commands = ["init", "validate", "plan", "destroy"]
}

terraform {
  source = "${include.root.locals.modules_dir}/unifi_device"

  # Limit parallelism to avoid 429 rate-limiting on the UDM login endpoint.
  # The paultyng/unifi provider re-authenticates per resource.
  extra_arguments "rate_limit" {
    commands  = ["apply", "plan", "destroy"]
    arguments = ["-parallelism=1"]
  }

  # Skip per-resource API refresh on apply to avoid triggering the UDM login
  # rate limiter (429). Plan and destroy still refresh.
  extra_arguments "no_refresh_apply" {
    commands  = ["apply"]
    arguments = ["-refresh=false"]
  }
}

# ---------------------------------------------------------------------------
# Per-unit overrides via <your-pkg>.yaml config_params.
# Add entries under "<your-pkg>/_stack/unifi/<region>/device" to set:
#
#   devices:                        # Map of device key -> device config object
#     udm:
#       mac: aa:bb:cc:dd:ee:ff      # replace with real MAC from UniFi controller
#       name: UniFi Dream Machine
#       type: gateway               # "gateway" (ignore_changes=all) or "switch"
#     switch_flex:
#       mac: aa:bb:cc:dd:ee:00      # replace with real MAC
#       name: USW-Flex-2.5G-8
#       type: switch
#       port_overrides:
#         "1":
#           name: host-1-port3
#           port_profile: amt_mgmt       # key in port_profiles map, or "" for default
#         "2":
#           name: host-1-port4
#           port_profile: pxe_provisioning
# ---------------------------------------------------------------------------

locals {
  devices = try(include.root.locals.unit_params.devices, {})

  # Credentials for the port_override_patch null_resource.
  unifi_api_url  = try(include.root.locals.unit_params._provider_unifi_api_url, "")
  unifi_username = try(include.root.locals.unit_secret_params["_provider_unifi_username"], "")
  unifi_password = try(include.root.locals.unit_secret_params["_provider_unifi_password"], "")
}

inputs = {
  devices          = local.devices
  port_profile_ids = dependency.port_profile.outputs.port_profile_ids
  unifi_api_url    = local.unifi_api_url
  unifi_username   = local.unifi_username
  unifi_password   = local.unifi_password
}
