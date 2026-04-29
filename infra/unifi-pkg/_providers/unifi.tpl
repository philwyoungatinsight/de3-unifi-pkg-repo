# UniFi OS controller: network/device management via paultyng/unifi provider.
# api_url: base URL of the UniFi controller (e.g., https://192.168.2.1)
# Auth: username + password — set in the secrets file under providers.unifi.
# insecure: true skips TLS verification — typical for homelab self-signed certs.
#
# NOTE: paultyng/unifi v0.41 re-authenticates per resource.
# Use -parallelism=1 on all apply/plan/destroy calls (set in each unit's
# extra_arguments block) to avoid 429 rate-limiting on the login endpoint.
terraform {
  required_version = ">= 1.3.0"
  required_providers {
    unifi = { source = "paultyng/unifi", version = "~> 0.41" }
    null  = { source = "hashicorp/null" }
  }
}
provider "unifi" {
  api_url        = "${API_URL}"
  username       = "${USERNAME}"
  password       = "${PASSWORD}"
  allow_insecure = ${INSECURE}
}
