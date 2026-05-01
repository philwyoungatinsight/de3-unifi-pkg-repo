include "root" {
  path   = find_in_parent_folders("root.hcl")
  expose = true
}

# Network IDs from the network unit — needed to map VLAN keys to resource IDs.
dependency "network" {
  config_path = "../network"
  mock_outputs = {
    network_ids = {}
  }
  mock_outputs_allowed_terraform_commands = ["init", "validate", "plan", "destroy"]
}

terraform {
  source = "${include.root.locals.modules_dir}/unifi_port_profile"

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
# Add entries under "<your-pkg>/_stack/unifi/<region>/port-profile" to set:
#
#   port_profiles:                  # Map of profile key -> profile config object
#     hypervisor_trunk:
#       name: Hypervisor Trunk
#       forward: customize
#       native_vlan: cloud_public   # key in vlans map (native/untagged VLAN)
#       tagged_vlans:               # list of VLAN keys from the network unit
#         - management
#         - provisioning
#         - storage
#     amt_mgmt:
#       name: AMT Management
#       forward: customize
#       native_vlan: management
#       tagged_vlans: []
# ---------------------------------------------------------------------------

locals {
  port_profiles = try(include.root.locals.unit_params.port_profiles, {})

  # Credentials for the VLAN patch script (null_resource.vlan_patch local-exec).
  unifi_api_url  = try(include.root.locals.unit_params._provider_unifi_api_url, "")
  unifi_username = try(include.root.locals.unit_secret_params["_provider_unifi_username"], "")
  unifi_password = try(include.root.locals.unit_secret_params["_provider_unifi_password"], "")
}

inputs = {
  port_profiles  = local.port_profiles
  network_ids    = dependency.network.outputs.network_ids
  unifi_api_url  = local.unifi_api_url
  unifi_username = local.unifi_username
  unifi_password = local.unifi_password
}
