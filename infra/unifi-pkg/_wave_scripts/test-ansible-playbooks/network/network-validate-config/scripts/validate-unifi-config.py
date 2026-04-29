#!/usr/bin/env python3
"""Validate UniFi controller configuration matches the YAML spec.

Checks three categories:
  1. VLANs       — name, purpose, subnet prefix, DHCP enabled/range
  2. Port profiles — native VLAN, tagged VLANs (resolved via VLAN ID)
  3. Device ports  — port name, assigned port profile, connected MAC

Reads expected configuration from a JSON file written by the Ansible playbook
and compares against the live UniFi controller state via the UniFi API.
No SSH to hosts required.

Usage:
    validate-unifi-config.py <config.json>

Required environment variables:
    UNIFI_USERNAME   UniFi admin username
    UNIFI_PASSWORD   UniFi admin password

Config JSON schema (written by the Ansible playbook):
{
  "unifi_url":      "https://192.168.2.1",
  "vlans": {
    "<vlan_key>": {
      "vlan_id": <int>,
      "name": "<str>",
      "purpose": "<str>",          # "corporate" | "guest"
      "subnet": "<cidr>",          # e.g. "10.0.10.0/24"
      "dhcp_enabled": <bool>,
      "dhcp_start": "<ip>",
      "dhcp_stop": "<ip>"
    }
  },
  "port_profiles": {
    "<profile_key>": {
      "name": "<str>",
      "native_vlan": "<vlan_key>",
      "tagged_vlans": ["<vlan_key>", ...]
    }
  },
  "devices": {
    "<dev_key>": {
      "name": "<str>",
      "mac": "<mac>",
      "type": "switch" | "gateway",
      "port_overrides": {
        "<port_num>": {
          "name": "<str>",
          "port_profile": "<profile_key>",
          "mac": "<mac>"            # optional: expected connected NIC MAC
        }
      }
    }
  }
}

Exit code: 0 if all checks pass, 1 if any check fails.
"""

import ipaddress
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


# ── Subnet comparison helper ───────────────────────────────────────────────────

def normalize_subnet(value):
    """Return the network address string for a CIDR (e.g. '10.0.10.1/24' → '10.0.10.0/24')."""
    try:
        return str(ipaddress.ip_interface(value).network)
    except ValueError:
        return value


# ── Verification logic ─────────────────────────────────────────────────────────

def verify(config, request):
    expected_vlans = config["vlans"]
    expected_profiles = config["port_profiles"]
    expected_devices = config["devices"]

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

    # ── Fetch all live data up front ───────────────────────────────────────────
    print("Fetching networks from UniFi ...", flush=True)
    networks_resp = request("GET", "/proxy/network/api/s/default/rest/networkconf")
    print("Fetching port profiles from UniFi ...", flush=True)
    profiles_resp = request("GET", "/proxy/network/api/s/default/rest/portconf")
    print("Fetching devices from UniFi ...", flush=True)
    devices_resp = request("GET", "/proxy/network/api/s/default/stat/device")
    print("Fetching active wired clients from UniFi ...", flush=True)
    sta_resp = request("GET", "/proxy/network/api/s/default/stat/sta")

    # ── Build lookup tables ────────────────────────────────────────────────────

    # UniFi networks: keyed by _id, by vlan_id (int), and by name (lower)
    actual_networks = networks_resp["data"]
    actual_net_by_id = {n["_id"]: n for n in actual_networks}
    actual_net_by_vlan = {}
    for n in actual_networks:
        vid = n.get("vlan") or n.get("vlan_id")
        if vid:
            actual_net_by_vlan[int(vid)] = n

    # UniFi 10.x stores tagged VLANs as an exclusion list rather than an inclusion
    # list.  Build the set of all "excludable" network IDs (non-WAN, vlan > 1) so
    # we can reverse-engineer the tagged set from excluded_networkconf_ids.
    # This mirrors the filter used in patch-port-profile-vlans.py.
    all_excludable_net_ids = {
        n["_id"]
        for n in actual_networks
        if n.get("purpose") != "wan"
        and n.get("vlan") and int(n["vlan"]) > 1
    }

    # UniFi port profiles: keyed by _id, by name (lower)
    actual_profiles = profiles_resp["data"]
    actual_profile_by_id = {p["_id"]: p for p in actual_profiles}
    actual_profile_by_name_lower = {p["name"].lower(): p for p in actual_profiles}

    # Map expected VLAN key → UniFi network _id (resolved via vlan_id)
    vlan_key_to_net_id = {}
    for vlan_key, vlan_cfg in expected_vlans.items():
        vid = vlan_cfg.get("vlan_id")
        if vid is not None:
            net = actual_net_by_vlan.get(int(vid))
            if net:
                vlan_key_to_net_id[vlan_key] = net["_id"]

    # Map expected profile key → UniFi portconf _id (resolved via display name)
    expected_profile_key_to_id = {}
    for profile_key, profile_cfg in expected_profiles.items():
        unifi_name = profile_cfg.get("name", "")
        match = actual_profile_by_name_lower.get(unifi_name.lower())
        if match:
            expected_profile_key_to_id[profile_key] = match["_id"]
        else:
            expected_profile_key_to_id[profile_key] = None

    # Active wired clients: {(sw_mac, port_idx): set of client MACs}
    actual_devices_by_mac = {d["mac"].lower(): d for d in devices_resp["data"]}
    active_by_port = {}
    for client in sta_resp["data"]:
        if not client.get("is_wired"):
            continue
        sw_mac = client.get("sw_mac", "").lower()
        sw_port = client.get("sw_port")
        cli_mac = client.get("mac", "").lower()
        if sw_mac and sw_port and cli_mac:
            active_by_port.setdefault((sw_mac, int(sw_port)), set()).add(cli_mac)

    # ── 1. VLAN checks ─────────────────────────────────────────────────────────
    print("\n── VLAN checks ──────────────────────────────────────────────", flush=True)
    for vlan_key, vlan_cfg in sorted(expected_vlans.items()):
        vid = vlan_cfg.get("vlan_id")
        vlan_label = f"VLAN {vid} ({vlan_key})"
        print(f"\n  Checking {vlan_label}", flush=True)

        actual_net = actual_net_by_vlan.get(int(vid)) if vid is not None else None
        if actual_net is None:
            fail(f"{vlan_label}: not found in UniFi (no network with vlan_id={vid})")
            continue

        # Name
        expected_name = vlan_cfg.get("name", "")
        actual_name = actual_net.get("name", "")
        if expected_name:
            if actual_name == expected_name:
                ok(f"{vlan_label} name")
            else:
                fail(f"{vlan_label} name", expected=expected_name, actual=actual_name)

        # Purpose
        expected_purpose = vlan_cfg.get("purpose", "")
        actual_purpose = actual_net.get("purpose", "")
        if expected_purpose and actual_purpose:
            if actual_purpose == expected_purpose:
                ok(f"{vlan_label} purpose")
            else:
                fail(f"{vlan_label} purpose", expected=expected_purpose, actual=actual_purpose)

        # Subnet — UniFi stores the gateway IP/prefix; config has the network address/prefix.
        # We normalise both to network/prefix for comparison.
        expected_subnet = vlan_cfg.get("subnet", "")
        actual_ip_subnet = actual_net.get("ip_subnet", "")
        if expected_subnet and actual_ip_subnet:
            expected_net_addr = normalize_subnet(expected_subnet)
            actual_net_addr = normalize_subnet(actual_ip_subnet)
            if expected_net_addr == actual_net_addr:
                ok(f"{vlan_label} subnet")
            else:
                fail(
                    f"{vlan_label} subnet",
                    expected=expected_subnet,
                    actual=actual_ip_subnet,
                )

        # DHCP enabled
        expected_dhcp = vlan_cfg.get("dhcp_enabled")
        # UniFi uses dhcpd_enabled for corporate networks; guest networks handle
        # DHCP implicitly. Only check when the field is present in the API response.
        actual_dhcp = actual_net.get("dhcpd_enabled")
        if expected_dhcp is not None and actual_dhcp is not None:
            if bool(actual_dhcp) == bool(expected_dhcp):
                ok(f"{vlan_label} dhcp_enabled")
            else:
                fail(
                    f"{vlan_label} dhcp_enabled",
                    expected=str(expected_dhcp),
                    actual=str(actual_dhcp),
                )

        # DHCP range (only when DHCP is expected to be enabled)
        if expected_dhcp:
            for field, cfg_key, api_key in [
                ("dhcp_start", "dhcp_start", "dhcpd_start"),
                ("dhcp_stop", "dhcp_stop", "dhcpd_stop"),
            ]:
                expected_val = vlan_cfg.get(cfg_key, "")
                actual_val = actual_net.get(api_key, "")
                if expected_val and actual_val:
                    if actual_val == expected_val:
                        ok(f"{vlan_label} {field}")
                    else:
                        fail(f"{vlan_label} {field}", expected=expected_val, actual=actual_val)

    # ── 2. Port profile checks ─────────────────────────────────────────────────
    print("\n── Port profile checks ──────────────────────────────────────", flush=True)
    for profile_key, profile_cfg in sorted(expected_profiles.items()):
        profile_label = f"profile {profile_key!r}"
        print(f"\n  Checking {profile_label}", flush=True)

        actual_profile_id = expected_profile_key_to_id.get(profile_key)
        if actual_profile_id is None:
            fail(
                f"{profile_label}: not found in UniFi "
                f"(no portconf named {profile_cfg.get('name', '')!r})"
            )
            continue

        actual_profile = actual_profile_by_id[actual_profile_id]
        ok(f"{profile_label}: found", note=f"name={actual_profile.get('name')!r}")

        # Native VLAN
        native_vlan_key = profile_cfg.get("native_vlan", "")
        if native_vlan_key:
            expected_native_id = vlan_key_to_net_id.get(native_vlan_key)
            actual_native_id = actual_profile.get("native_networkconf_id", "")
            if expected_native_id is None:
                fail(
                    f"{profile_label} native_vlan: VLAN key {native_vlan_key!r} "
                    f"could not be resolved to a UniFi network ID"
                )
            else:
                exp_vlan_name = actual_net_by_id.get(expected_native_id, {}).get("name", native_vlan_key)
                act_vlan_name = actual_net_by_id.get(actual_native_id, {}).get("name", actual_native_id or "(default)")
                if actual_native_id == expected_native_id:
                    ok(f"{profile_label} native_vlan", note=exp_vlan_name)
                else:
                    fail(
                        f"{profile_label} native_vlan",
                        expected=exp_vlan_name,
                        actual=act_vlan_name,
                    )

        # Tagged VLANs
        tagged_vlan_keys = profile_cfg.get("tagged_vlans", []) or []
        expected_tagged_ids = set()
        unresolved_tagged = []
        for tvk in tagged_vlan_keys:
            tid = vlan_key_to_net_id.get(tvk)
            if tid:
                expected_tagged_ids.add(tid)
            else:
                unresolved_tagged.append(tvk)

        if unresolved_tagged:
            fail(
                f"{profile_label} tagged_vlans: could not resolve VLAN keys "
                f"{unresolved_tagged!r} to UniFi network IDs"
            )
        else:
            # UniFi 10.x stores tagged VLANs as an exclusion list.
            # If tagged_vlan_mgmt == "custom", compute tagged as:
            #   all_excludable - excluded - native
            # otherwise fall back to reading tagged_networkconf_ids directly (pre-10.x).
            native_net_id = actual_profile.get("native_networkconf_id", "")
            if actual_profile.get("tagged_vlan_mgmt") == "custom":
                excluded_ids = set(actual_profile.get("excluded_networkconf_ids") or [])
                actual_tagged_ids = all_excludable_net_ids - excluded_ids - {native_net_id}
            else:
                actual_tagged_ids = set(actual_profile.get("tagged_networkconf_ids") or [])
            if actual_tagged_ids == expected_tagged_ids:
                exp_names = sorted(
                    actual_net_by_id.get(i, {}).get("name", i)
                    for i in expected_tagged_ids
                )
                ok(f"{profile_label} tagged_vlans", note=f"[{', '.join(exp_names)}]")
            else:
                def ids_to_names(ids):
                    return sorted(
                        actual_net_by_id.get(i, {}).get("name", i) for i in ids
                    )
                fail(
                    f"{profile_label} tagged_vlans",
                    expected=str(ids_to_names(expected_tagged_ids)),
                    actual=str(ids_to_names(actual_tagged_ids)),
                )

    # ── 3. Device / port checks ────────────────────────────────────────────────
    print("\n── Device port checks ───────────────────────────────────────", flush=True)
    now = int(time.time())

    for dev_key, dev_cfg in sorted(expected_devices.items()):
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

        actual_port_overrides = {
            p["port_idx"]: p for p in actual_dev.get("port_overrides", [])
        }
        actual_port_table = {
            p["port_idx"]: p for p in actual_dev.get("port_table", [])
        }

        for port_num_str, override in sorted(
            port_overrides_cfg.items(), key=lambda x: int(x[0])
        ):
            port_idx = int(port_num_str)
            expected_name = override.get("name", "")
            expected_profile_key = override.get("port_profile", "")
            expected_mac = override.get("mac", "").lower()
            port_label = f"{dev_key} port {port_idx}"

            # Port name (alias) — skip for gateway devices and empty names
            if expected_name and not is_gateway:
                actual_override = actual_port_overrides.get(port_idx, {})
                actual_name = actual_override.get("name", "")
                if actual_name == expected_name:
                    ok(f"{port_label} name")
                else:
                    fail(f"{port_label} name", expected=expected_name, actual=actual_name)

            # Port profile
            if expected_profile_key:
                expected_portconf_id = expected_profile_key_to_id.get(expected_profile_key)
                expected_profile_name = (
                    expected_profiles.get(expected_profile_key, {}).get("name", expected_profile_key)
                )

                if expected_portconf_id is None:
                    fail(
                        f"{port_label} profile: profile key {expected_profile_key!r} "
                        f"not found in UniFi (no matching display name)"
                    )
                else:
                    actual_portconf_id = (
                        actual_port_overrides.get(port_idx, {}).get("portconf_id")
                        or actual_port_table.get(port_idx, {}).get("portconf_id", "")
                    )
                    actual_profile_name = actual_profile_by_id.get(
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

            # Connected MAC
            if expected_mac:
                active_macs = active_by_port.get((dev_mac, port_idx), set())
                if expected_mac in active_macs:
                    ok(f"{port_label} MAC", note="currently active")
                    continue

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
                    ok(
                        f"{port_label} MAC",
                        note=f"not active, {age_str}" if age_str else "not active",
                    )
                    continue

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
        print("Usage: validate-unifi-config.py <config.json>", file=sys.stderr)
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
