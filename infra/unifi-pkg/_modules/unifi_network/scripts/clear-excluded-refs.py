#!/usr/bin/env python3
"""Remove managed network IDs from device port_override excluded_networkconf_ids.

Called by the null_resource.pre_destroy_clear_excluded destroy-time provisioner
in the unifi-network module. This unblocks `unifi_network` DELETE calls, which
fail with api.err.ResourceReferredBy when a device port_override still lists the
network ID in excluded_networkconf_ids (set by the vlan_patch provisioner or
manually via the controller).

Why this is needed:
  The vlan_patch script in unifi-port-profile sets tagged_vlan_mgmt=custom and
  excluded_networkconf_ids on port profiles. When these settings are reflected onto
  device port_overrides (or set directly on gateway ports), the controller refuses
  to delete the referenced networks. Clearing those IDs from device port_overrides
  before network deletion unblocks the destroy.

Environment variables (set by null_resource.pre_destroy_clear_excluded triggers):
  UNIFI_URL    - Base URL of the UniFi controller
  UNIFI_USERNAME - Admin username
  UNIFI_PASSWORD - Admin password
  NETWORK_IDS  - JSON object mapping network key to network ID (the ones being deleted)
"""

import os
import json
import ssl
import ipaddress
import urllib.request
import urllib.error
import sys


def main():
    unifi_url   = os.environ["UNIFI_URL"].rstrip("/")
    username    = os.environ["UNIFI_USERNAME"]
    password    = os.environ["UNIFI_PASSWORD"]
    network_ids = set(json.loads(os.environ["NETWORK_IDS"]).values())

    if not network_ids:
        print("No managed network IDs — nothing to clear")
        return

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    cookies = {}

    def request(method, path, data=None, extra_headers=None):
        url = f"{unifi_url}{path}"
        headers = {"Content-Type": "application/json"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx) as resp:
                for hdr in resp.headers.get_all("Set-Cookie") or []:
                    name, _, rest = hdr.partition("=")
                    cookies[name.strip()] = rest.split(";")[0].strip()
                return json.loads(resp.read()), None
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}: {e.read().decode()[:200]}"

    csrf_token = ""

    def request_with_csrf(method, path, data=None, extra_headers=None):
        """Like request() but also returns the x-updated-csrf-token header value."""
        url = f"{unifi_url}{path}"
        headers = {"Content-Type": "application/json"}
        if cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=ctx) as resp:
                for hdr in resp.headers.get_all("Set-Cookie") or []:
                    name, _, rest = hdr.partition("=")
                    cookies[name.strip()] = rest.split(";")[0].strip()
                new_csrf = resp.headers.get("x-updated-csrf-token", "")
                return json.loads(resp.read()), None, new_csrf
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}: {e.read().decode()[:200]}", ""

    print(f"Authenticating to {unifi_url} ...")
    _, _, csrf_token = request_with_csrf("POST", "/api/auth/login", {"username": username, "password": password})

    print(f"Clearing references to {len(network_ids)} managed network(s): {network_ids}")
    print("Fetching all devices ...")
    result, err = request("GET", "/proxy/network/api/s/default/stat/device")
    if err:
        print(f"ERROR fetching devices: {err}", file=sys.stderr)
        sys.exit(1)
    all_devices = result["data"]

    cleared_total = 0
    for device in all_devices:
        device_id   = device["_id"]
        device_name = device.get("name", device_id)
        overrides   = device.get("port_overrides", [])

        # Check if any override has one of our managed network IDs in excluded_networkconf_ids
        def has_ref(po):
            return bool(network_ids & set(po.get("excluded_networkconf_ids", [])))

        refs = [po for po in overrides if has_ref(po)]
        if not refs:
            continue

        # Rebuild port_overrides: remove managed network IDs from excluded_networkconf_ids.
        # Preserve everything else (name, portconf_id, native_networkconf_id, etc.).
        # port_idx (stat field) → number (REST API field).
        updated = []
        for po in overrides:
            entry = {"number": po["port_idx"]}
            if po.get("name"):
                entry["name"] = po["name"]
            if po.get("portconf_id"):
                entry["portconf_id"] = po["portconf_id"]
            if po.get("native_networkconf_id"):
                entry["native_networkconf_id"] = po["native_networkconf_id"]
            excluded = [x for x in po.get("excluded_networkconf_ids", []) if x not in network_ids]
            if excluded:
                entry["excluded_networkconf_ids"] = excluded
            # Preserve tagged_vlan_mgmt if there are still excluded IDs
            if excluded and po.get("tagged_vlan_mgmt"):
                entry["tagged_vlan_mgmt"] = po["tagged_vlan_mgmt"]
            if po.get("forward") and po["forward"] != "all":
                entry["forward"] = po["forward"]
            updated.append(entry)

        _, err = request(
            "PUT",
            f"/proxy/network/api/s/default/rest/device/{device_id}",
            {"port_overrides": updated},
            extra_headers={"X-Csrf-Token": csrf_token},
        )
        if err:
            # Gateway devices reject REST writes — this is expected and safe to skip.
            print(f"  {device_name}: SKIP (device rejects writes: {err[:80]})")
        else:
            removed = sum(len(network_ids & set(po.get("excluded_networkconf_ids", []))) for po in refs)
            print(f"  {device_name}: removed {removed} network reference(s) from {len(refs)} port(s)")
            cleared_total += removed

    print(f"Done — removed {cleared_total} network reference(s) across all devices")

    # --- Proactive check: clients with fixed IPs overlapping managed subnets ---
    # A client with use_fixedip=true whose fixed IP falls inside a managed network's
    # subnet will cause the network DELETE to fail with api.err.ResourceReferredBy
    # (reference_type: FIXED_IP_OVERLAPS_NETWORK_SUBNET). Detect this now and give
    # an actionable error message before Terraform sees the cryptic API error.
    print("\nChecking for clients with fixed IPs in managed networks ...")

    result, err = request("GET", "/proxy/network/api/s/default/rest/networkconf")
    if err:
        print(f"WARNING: Could not fetch network configs to check fixed IPs: {err}", file=sys.stderr)
        return

    managed_nets = {}
    for net in result.get("data", []):
        if net.get("_id") not in network_ids:
            continue
        # UniFi API uses ip_subnet (CIDR) for the network subnet
        subnet_str = net.get("ip_subnet") or net.get("subnet")
        if not subnet_str:
            continue
        try:
            net_obj = ipaddress.ip_network(subnet_str, strict=False)
        except ValueError:
            continue
        managed_nets[net["_id"]] = {
            "name": net.get("name", net["_id"]),
            "subnet": subnet_str,
            "network": net_obj,
        }

    if not managed_nets:
        print("No managed network subnets found — skipping fixed IP check.")
        return

    result, err = request("GET", "/proxy/network/api/s/default/rest/user")
    if err:
        print(f"WARNING: Could not fetch clients to check fixed IPs: {err}", file=sys.stderr)
        return

    blockers = []
    for client in result.get("data", []):
        if not client.get("use_fixedip"):
            continue
        fixed_ip_str = client.get("fixed_ip") or client.get("ip")
        if not fixed_ip_str:
            continue
        try:
            fixed_ip = ipaddress.ip_address(fixed_ip_str)
        except ValueError:
            continue
        for net_id, net_info in managed_nets.items():
            if fixed_ip in net_info["network"]:
                blockers.append({
                    "client_name": client.get("name") or client.get("hostname") or client.get("mac", "unknown"),
                    "client_id":   client["_id"],
                    "mac":         client.get("mac", ""),
                    "fixed_ip":    fixed_ip_str,
                    "network_name": net_info["name"],
                    "network_id":   net_id,
                    "subnet":       net_info["subnet"],
                })

    if not blockers:
        print("No fixed-IP conflicts found — network deletion should succeed.")
        return

    # Auto-clear fixed IPs rather than blocking. These clients have fixed IPs in
    # managed subnets which would block network deletion with ResourceReferredBy.
    # Clearing use_fixedip=false lets the client keep its current IP via normal
    # DHCP but removes the hard pin that references the network object.
    print(f"\nAuto-clearing fixed IPs for {len(blockers)} client(s) to unblock network operations:")
    failed = []
    for b in blockers:
        print(f"  Clearing fixed IP {b['fixed_ip']} for client '{b['client_name']}' (MAC: {b['mac']}) ...")
        _, err, new_csrf = request_with_csrf(
            "PUT",
            f"/proxy/network/api/s/default/rest/user/{b['client_id']}",
            {"use_fixedip": False},
            extra_headers={"X-Csrf-Token": csrf_token},
        )
        if new_csrf:
            csrf_token = new_csrf
        if err:
            print(f"    FAILED: {err}", file=sys.stderr)
            failed.append(b)
        else:
            print(f"    OK — fixed IP cleared for '{b['client_name']}'")

    if failed:
        print("\nERROR: Could not auto-clear all fixed IPs. Clear these manually:", file=sys.stderr)
        for b in failed:
            print(f"  Client:  {b['client_name']}  (MAC: {b['mac']})", file=sys.stderr)
            print(f"  Fixed IP: {b['fixed_ip']}  in network '{b['network_name']}' ({b['subnet']})", file=sys.stderr)
            print(f"  UniFi UI → Clients → {b['client_name']} → Settings → disable Fixed IP", file=sys.stderr)
        sys.exit(1)

    print("All fixed-IP conflicts cleared — network operations should succeed.")


if __name__ == "__main__":
    main()
