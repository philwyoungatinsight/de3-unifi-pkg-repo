#!/usr/bin/env python3
"""Verify UniFi switch port configuration matches the YAML config.

Reads expected configuration from a JSON file and compares against the live
UniFi controller state.  No SSH to hosts required — only the UniFi API.

Usage:
    verify-switch-ports.py <config.json>

Required environment variables:
    UNIFI_USERNAME   UniFi admin username
    UNIFI_PASSWORD   UniFi admin password

Config JSON schema (written by the Ansible playbook):
{
  "unifi_url":      "https://192.168.2.1",
  "devices":        { <device_key>: { "name": ..., "mac": ..., "port_overrides": {
                        <port_num>: { "name": ..., "port_profile": ..., "mac": ... }
                    } } },
  "port_profiles":  { <profile_key>: { "name": ..., "native_vlan": ..., "tagged_vlans": [...] } }
}

Port-level "mac" field: expected MAC of the device NIC connected to that port.
  Checked against (in order):
    1. stat/sta active clients  → PASS (currently active)
    2. port_table last_connection → PASS (last seen N seconds ago)
  If neither matches → FAIL.

Exit code: 0 if all checks pass, 1 if any check fails.
"""

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request


# ── UniFi API helpers ──────────────────────────────────────────────────────────

def make_client(unifi_url, username, password):
    """Return an authenticated request() closure for the UniFi controller."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    cookie_jar = {}

    def request(method, path, data=None, extra_headers=None):
        url = f"{unifi_url}{path}"
        headers = {"Content-Type": "application/json"}
        if cookie_jar:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_jar.items())
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx) as resp:
                for hdr in resp.headers.get_all("Set-Cookie") or []:
                    name, _, rest = hdr.partition("=")
                    value = rest.split(";")[0]
                    cookie_jar[name.strip()] = value.strip()
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            print(f"HTTP {e.code} from {method} {path}: {body}", file=sys.stderr)
            raise

    print(f"Authenticating to {unifi_url} ...", flush=True)
    request("POST", "/api/auth/login", {"username": username, "password": password})
    return request


# ── Verification logic ─────────────────────────────────────────────────────────

def verify(config, request):
    expected_devices = config["devices"]
    expected_profiles = config["port_profiles"]

    # Fetch live data from UniFi
    print("Fetching devices from UniFi ...", flush=True)
    devices_resp = request("GET", "/proxy/network/api/s/default/stat/device")
    print("Fetching port profiles from UniFi ...", flush=True)
    profiles_resp = request("GET", "/proxy/network/api/s/default/rest/portconf")
    print("Fetching active wired clients from UniFi ...", flush=True)
    sta_resp = request("GET", "/proxy/network/api/s/default/stat/sta")

    # Build lookups for actual UniFi state
    actual_devices_by_mac = {d["mac"].lower(): d for d in devices_resp["data"]}
    actual_profiles_by_id = {p["_id"]: p for p in profiles_resp["data"]}
    actual_profiles_by_name_lower = {
        p["name"].lower(): p for p in profiles_resp["data"]
    }

    # Active wired clients: {(sw_mac, port_idx): set of client MACs}
    # sw_mac and client mac are both lowercased.
    active_by_port = {}
    for client in sta_resp["data"]:
        if not client.get("is_wired"):
            continue
        sw_mac = client.get("sw_mac", "").lower()
        sw_port = client.get("sw_port")
        cli_mac = client.get("mac", "").lower()
        if sw_mac and sw_port and cli_mac:
            active_by_port.setdefault((sw_mac, int(sw_port)), set()).add(cli_mac)

    # Map expected profile keys → actual UniFi portconf _id
    expected_profile_key_to_id = {}
    for profile_key, profile_cfg in expected_profiles.items():
        unifi_name = profile_cfg.get("name", "")
        match = actual_profiles_by_name_lower.get(unifi_name.lower())
        if match:
            expected_profile_key_to_id[profile_key] = match["_id"]
        else:
            expected_profile_key_to_id[profile_key] = None

    passes = []
    failures = []

    def ok(label, note=""):
        line = f"  PASS  {label}"
        if note:
            line += f"  ({note})"
        passes.append(line)

    def fail(label, expected="", actual=""):
        failures.append(f"  FAIL  {label}")
        if expected or actual:
            failures.append(f"          expected : {expected!r}")
            failures.append(f"          actual   : {actual!r}")

    now = int(time.time())

    for dev_key, dev_cfg in expected_devices.items():
        dev_mac = dev_cfg.get("mac", "").lower()
        dev_label = dev_cfg.get("name", dev_key)
        print(f"\n── Device: {dev_key} ({dev_label})  MAC={dev_mac}", flush=True)

        if not dev_mac:
            fail(f"{dev_key}: no MAC defined in config")
            continue

        actual_dev = actual_devices_by_mac.get(dev_mac)
        if actual_dev is None:
            fail(f"{dev_key}: device not found in UniFi (MAC {dev_mac})")
            continue

        actual_dev_name = actual_dev.get("name", "")
        ok(f"{dev_key}: device found", note=f"UniFi name={actual_dev_name!r}")

        port_overrides_cfg = dev_cfg.get("port_overrides", {})
        if not port_overrides_cfg:
            ok(f"{dev_key}: no port overrides to verify")
            continue

        # Gateway devices (UDM) do not expose port aliases via port_overrides in
        # the UniFi API — only switches do.  Skip name checks for gateways.
        is_gateway = dev_cfg.get("type", "") == "gateway"

        # Build per-port lookups from actual device data.
        # port_overrides[] holds configured name + portconf_id (only customised ports).
        # port_table[] holds runtime state for every port (including last_connection).
        actual_port_overrides = {
            p["port_idx"]: p for p in actual_dev.get("port_overrides", [])
        }
        actual_port_table = {
            p["port_idx"]: p for p in actual_dev.get("port_table", [])
        }

        for port_num_str, override in sorted(port_overrides_cfg.items(), key=lambda x: int(x[0])):
            port_idx = int(port_num_str)
            expected_name = override.get("name", "")
            expected_profile_key = override.get("port_profile", "")
            expected_mac = override.get("mac", "").lower()
            port_label = f"{dev_key} port {port_idx}"

            # ── Port name (alias) ──────────────────────────────────────────
            if expected_name and not is_gateway:
                actual_override = actual_port_overrides.get(port_idx, {})
                actual_name = actual_override.get("name", "")
                if actual_name == expected_name:
                    ok(f"{port_label} name")
                else:
                    fail(f"{port_label} name", expected=expected_name, actual=actual_name)

            # ── Port profile ───────────────────────────────────────────────
            if expected_profile_key:
                expected_portconf_id = expected_profile_key_to_id.get(expected_profile_key)
                expected_profile_name = expected_profiles.get(expected_profile_key, {}).get("name", expected_profile_key)

                if expected_portconf_id is None:
                    fail(
                        f"{port_label} profile: profile key {expected_profile_key!r} "
                        f"not found in UniFi (no matching display name)"
                    )
                    continue

                # Prefer portconf_id from port_overrides (configured); fall back to port_table.
                actual_portconf_id = (
                    actual_port_overrides.get(port_idx, {}).get("portconf_id")
                    or actual_port_table.get(port_idx, {}).get("portconf_id", "")
                )
                actual_profile_name = actual_profiles_by_id.get(
                    actual_portconf_id, {}
                ).get("name", actual_portconf_id or "(default)")

                if actual_portconf_id == expected_portconf_id:
                    ok(f"{port_label} profile")
                else:
                    fail(
                        f"{port_label} profile",
                        expected=expected_profile_name,
                        actual=actual_profile_name,
                    )

            # ── Port MAC (connected NIC) ────────────────────────────────────
            if expected_mac:
                # Check 1: active wired client on this switch port right now.
                active_macs = active_by_port.get((dev_mac, port_idx), set())
                if expected_mac in active_macs:
                    ok(f"{port_label} MAC", note="currently active")
                    continue

                # Check 2: last_connection in port_table (persists when device is off).
                pt = actual_port_table.get(port_idx, {})
                lc = pt.get("last_connection", {})
                lc_mac = lc.get("mac", "").lower()
                lc_ts = lc.get("last_seen")
                if lc_mac == expected_mac:
                    age_str = ""
                    if lc_ts:
                        age_secs = now - lc_ts
                        if age_secs < 120:
                            age_str = f"last seen {age_secs}s ago"
                        elif age_secs < 7200:
                            age_str = f"last seen {age_secs // 60}m ago"
                        else:
                            age_str = f"last seen {age_secs // 3600}h ago"
                    ok(f"{port_label} MAC", note=f"not active, {age_str}" if age_str else "not active")
                    continue

                # Neither source confirmed the expected MAC.
                actual_note = ""
                if active_macs:
                    actual_note = f"active={sorted(active_macs)}"
                elif lc_mac:
                    actual_note = f"last_connection={lc_mac!r}"
                else:
                    actual_note = "no data"
                fail(
                    f"{port_label} MAC",
                    expected=expected_mac,
                    actual=actual_note,
                )

    return passes, failures


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: verify-switch-ports.py <config.json>", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        config = json.load(f)

    unifi_url = config["unifi_url"].rstrip("/")
    username = os.environ.get("UNIFI_USERNAME")
    password = os.environ.get("UNIFI_PASSWORD")

    if not username or not password:
        print("ERROR: UNIFI_USERNAME and UNIFI_PASSWORD must be set", file=sys.stderr)
        sys.exit(1)

    try:
        client = make_client(unifi_url, username, password)
        passes, failures = verify(config, client)
    except Exception as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        sys.exit(1)

    # ── Summary ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    total = len(passes) + len(failures)
    for line in passes:
        print(line)
    for line in failures:
        print(line)

    if failures:
        print(f"\nResult: {len(failures)} FAIL, {len(passes)} PASS  (total {total} checks)")
        sys.exit(1)
    else:
        print(f"\nResult: All {len(passes)} checks PASSED")
        sys.exit(0)


if __name__ == "__main__":
    main()
