include "root" {
  path   = find_in_parent_folders("root.hcl")
  expose = true
}

terraform {
  source = "${include.root.locals.modules_dir}/unifi_network"

  # Limit parallelism to avoid 429 rate-limiting on the UDM login endpoint.
  # The paultyng/unifi provider re-authenticates per resource.
  extra_arguments "rate_limit" {
    commands  = ["apply", "plan", "destroy"]
    arguments = ["-parallelism=1"]
  }

  # Skip per-resource API refresh on apply to avoid triggering the UDM login
  # rate limiter (429). Plan and destroy still refresh (drift detection / safe teardown).
  extra_arguments "no_refresh_apply" {
    commands  = ["apply"]
    arguments = ["-refresh=false"]
  }
}

# ---------------------------------------------------------------------------
# Per-unit overrides via <your-pkg>.yaml config_params.
# Add entries under "<your-pkg>/_stack/unifi/<region>/network" to set:
#
#   domain_name:  homelab.local   # DHCP domain suffix for all VLANs
#   vlans:                        # Map of VLAN config key -> VLAN config object
#     cloud_public:
#       name: Cloud-Public
#       purpose: corporate        # corporate | guest | vlan-only
#       vlan_id: 10
#       subnet: 10.0.10.0/24
#       dhcp_enabled: true
#       dhcp_start: 10.0.10.100
#       dhcp_stop: 10.0.10.254
#       dns_servers: [8.8.8.8, 8.8.4.4]
#
# NOTE: VLAN 1 (default/home) is managed by UniFi internally.
#       Do not include it here — manage it via the controller UI.
# ---------------------------------------------------------------------------

locals {
  domain_name    = try(include.root.locals.unit_params.domain_name, "homelab.local")
  vlans          = try(include.root.locals.unit_params.vlans, {})
  fixed_clients  = try(include.root.locals.unit_params.fixed_clients, {})

  # Credentials for the pre-destroy cleanup script.
  unifi_api_url  = try(include.root.locals.unit_params._provider_unifi_api_url, "")
  unifi_username = try(include.root.locals.unit_secret_params["_provider_unifi_username"], "")
  unifi_password = try(include.root.locals.unit_secret_params["_provider_unifi_password"], "")
}

inputs = {
  domain_name    = local.domain_name
  vlans          = local.vlans
  fixed_clients  = local.fixed_clients
  unifi_api_url  = local.unifi_api_url
  unifi_username = local.unifi_username
  unifi_password = local.unifi_password
}
