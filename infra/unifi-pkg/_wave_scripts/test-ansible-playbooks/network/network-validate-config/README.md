# network-validate-config

Wave test playbook for the `network.unifi.validate-config` wave.

Validates that the live UniFi controller configuration matches the YAML spec in detail.
Runs on localhost — no SSH to any host. Only the UniFi controller API is required.

## What Is Checked

### 1. VLANs (`unifi-pkg/_stack/unifi/examples/example-lab/network → vlans`)

For each VLAN declared in the config:

| Check | UniFi API field |
|-------|----------------|
| Network exists with matching `vlan_id` | `networkconf.vlan` |
| `name` matches | `networkconf.name` |
| `purpose` matches (`corporate` / `guest`) | `networkconf.purpose` |
| Subnet (network address) matches | `networkconf.ip_subnet` (normalised) |
| `dhcp_enabled` matches | `networkconf.dhcpd_enabled` |
| `dhcp_start` / `dhcp_stop` match (when DHCP enabled) | `networkconf.dhcpd_start/stop` |

### 2. Port Profiles (`unifi-pkg/_stack/unifi/examples/example-lab/port-profile → port_profiles`)

For each port profile declared in the config:

| Check | UniFi API field |
|-------|----------------|
| Profile exists with matching `name` | `portconf.name` |
| `native_vlan` key resolves to the correct UniFi network ID | `portconf.native_networkconf_id` |
| `tagged_vlans` keys resolve to the correct set of UniFi network IDs | `portconf.tagged_networkconf_ids` |

VLAN keys (e.g. `cloud_public`, `management`) are resolved to UniFi `networkconf._id`
values by matching `vlan_id` in the YAML against `vlan` in the UniFi network list.

### 3. Device Port Assignments (`unifi-pkg/_stack/unifi/examples/example-lab/device → devices`)

For each device and each configured port override:

| Check | Notes |
|-------|-------|
| Device exists in UniFi by MAC address | |
| Port alias/name matches | Skipped for gateway devices (UDM) |
| Port profile assignment matches | Resolved via profile name → `portconf._id` |
| Connected MAC matches | Checks active wired clients first, then `port_table.last_connection` |

## Files

```
network-validate-config/
  run                        bash wrapper — activates venv and runs playbook.yaml
  playbook.yaml              Ansible play (localhost, config_base → capture → validate)
  tasks/
    capture-config-fact.yaml extracts vlans, port_profiles, devices + UniFi credentials
  scripts/
    validate-unifi-config.py Python validator — fetches UniFi API, compares against config
```

## Running Directly

```bash
cd ~/git/de3
./scripts/wave-scripts/unifi-pkg/test-ansible-playbooks/network/network-validate-config/run
```

## Config Sources

All expected values come from the YAML config (no hardcoded values):

- `infra/unifi-pkg/_config/unifi-pkg.yaml`
  - `providers.unifi.config_params["unifi-pkg/_stack/unifi/examples/example-lab"]._provider_api_url`
  - `providers.unifi.config_params["unifi-pkg/_stack/unifi/examples/example-lab/network"].vlans`
  - `providers.unifi.config_params["unifi-pkg/_stack/unifi/examples/example-lab/port-profile"].port_profiles`
  - `providers.unifi.config_params["unifi-pkg/_stack/unifi/examples/example-lab/device"].devices`
- `infra/unifi-pkg/_config/unifi-pkg_secrets.sops.yaml`
  - `providers.unifi.username` / `providers.unifi.password`
