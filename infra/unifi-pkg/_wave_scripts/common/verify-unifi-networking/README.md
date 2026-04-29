# verify-unifi-networking

Verifies that the live UniFi switch configuration matches what is declared in
`infra/unifi-pkg/_config/unifi-pkg.yaml`.  Uses only the config YAML and the UniFi controller
API — no SSH to any host is required.

## Usage

```bash
./run --test    # run verification
./run --build   # set up venv only
```

## What It Checks

For each device in `providers.unifi.config_params["unifi-pkg/_stack/unifi/examples/example-lab/device"].devices`:

- **Device exists** in UniFi (matched by MAC address)
- **Port names** match for each configured `port_overrides` entry (switches only)
- **Port profiles** match for each `port_overrides` entry with a non-empty `port_profile`

Exit code: `0` if all checks pass, `1` if any check fails.

## How It Works

1. `run --test` activates a shared venv reused from
   `scripts/tg-scripts/maas-pkg/maas/configure-server` (same Ansible version
   and requirements already installed there).
2. Ansible playbook runs on `localhost`:
   - Loads public YAML config + SOPS secrets via `config_base` role
   - Writes a JSON config snapshot to `/tmp/verify-unifi-networking-config.json`
   - Runs `scripts/verify-switch-ports.py` with UniFi credentials from secrets
3. Python script authenticates to the UniFi controller and compares:
   - Live device MACs vs config MACs
   - Live port aliases vs config port names
   - Live portconf IDs vs expected IDs (resolved from port profile display names)

## Adding New Checks

To add host-level checks (e.g. verify NIC MACs on the OS side), add a second
play to `playbook.verify-unifi-networking.yaml` that targets `maas_server` or
individual hosts and cross-references the machine config from the YAML.
