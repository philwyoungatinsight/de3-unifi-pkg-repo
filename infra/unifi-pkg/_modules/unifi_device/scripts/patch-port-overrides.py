#!/usr/bin/env python3
"""Patch UniFi switch port overrides to enforce the configured portconf_id values.

The paultyng/unifi provider's Read() for unifi_device does not reliably detect
drift in port_override configurations. When a switch reproes (e.g. after a port
profile change), it may reset port assignments to the default profile without
Terraform detecting the change — so subsequent plans show "No changes" even
though the switch is misconfigured.

This script patches port overrides directly via the UniFi API after every apply,
ensuring the switch always has the intended portconf_id on each managed port.

After setting the desired config via the REST API, the script issues a
force-provision command to push the controller's desired state to the switch
hardware immediately (rather than waiting for the switch's next inform cycle).

Environment variables (set by the null_resource local-exec):
  UNIFI_URL      - Base URL of the UniFi controller (e.g., https://192.168.2.1)
  UNIFI_USERNAME - Admin username
  UNIFI_PASSWORD - Admin password
  SWITCHES_JSON  - JSON array of switch objects, each with:
                     mac           - switch MAC address
                     name          - switch name (for logging)
                     port_overrides - list of {number, name, portconf_id} dicts
                                      (input uses "number" key; script maps to "port_idx" for API)
                                      portconf_id="" means default (omitted from API call)
"""

import os
import json
import ssl
import time
import urllib.request
import urllib.error
import sys


def main():
    unifi_url = os.environ["UNIFI_URL"].rstrip("/")
    username  = os.environ["UNIFI_USERNAME"]
    password  = os.environ["UNIFI_PASSWORD"]
    switches  = json.loads(os.environ["SWITCHES_JSON"])

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    cookie_jar = {}
    # csrf_token is stored as a mutable container so the closure can update it.
    csrf_state = {"token": ""}

    def request(method, path, data=None, extra_headers=None, _retries=4, _backoff=30):
        url = f"{unifi_url}{path}"
        headers = {"Content-Type": "application/json"}
        if cookie_jar:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_jar.items())
        if csrf_state["token"] and method.upper() in ("POST", "PUT", "DELETE", "PATCH"):
            headers["X-Csrf-Token"] = csrf_state["token"]
        if extra_headers:
            headers.update(extra_headers)
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        for attempt in range(_retries):
            try:
                with urllib.request.urlopen(req, context=ctx) as resp:
                    for hdr in resp.headers.get_all("Set-Cookie") or []:
                        name, _, rest = hdr.partition("=")
                        value = rest.split(";")[0]
                        cookie_jar[name.strip()] = value.strip()
                    # Capture CSRF token: prefer X-Csrf-Token / x-updated-csrf-token UUID
                    # (returned by UniFi on login and after mutating calls).
                    # Do NOT use the TOKEN cookie — that is a JWT, not the CSRF token.
                    csrf_hdr = resp.headers.get("X-Csrf-Token") or resp.headers.get("x-updated-csrf-token")
                    if csrf_hdr:
                        csrf_state["token"] = csrf_hdr
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                body_text = e.read().decode()
                if e.code == 429 and attempt < _retries - 1:
                    wait = _backoff * (2 ** attempt)
                    print(f"HTTP 429 on {method} {path} (attempt {attempt+1}/{_retries}); retrying in {wait}s", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"HTTP {e.code} on {method} {path}: {body_text}", file=sys.stderr)
                raise

    print(f"Authenticating to {unifi_url} ...")
    auth_resp = request("POST", "/api/auth/login", {"username": username, "password": password})

    # CSRF token is already captured from the X-Csrf-Token response header during login.
    # Do NOT override it with the TOKEN cookie — TOKEN is a JWT, not the CSRF UUID.

    print("Fetching all devices from controller ...")
    result = request("GET", "/proxy/network/api/s/default/stat/device")
    all_devices = {d["mac"].lower(): d for d in result.get("data", [])}

    patched_macs = []

    for sw in switches:
        sw_mac  = sw["mac"].lower()
        sw_name = sw.get("name", sw_mac)
        desired_overrides = sw.get("port_overrides", [])

        if not desired_overrides:
            print(f"{sw_name}: no port overrides configured — skipping")
            continue

        device = all_devices.get(sw_mac)
        if not device:
            print(f"WARNING: switch {sw_name} ({sw_mac}) not found in UniFi — skipping", file=sys.stderr)
            continue

        device_id = device["_id"]
        current_overrides = device.get("port_overrides", [])

        # Build a map of current port overrides by port number for comparison.
        current_by_port = {po.get("port_idx", po.get("number")): po for po in current_overrides}

        # Build the new port_overrides list: merge desired config with current,
        # preserving any non-managed ports (ports not in our desired list).
        # The UniFi REST API and switch firmware require "port_idx" as the port key.
        # SWITCHES_JSON input uses "number" (from main.tf); we map that to port_idx here.
        desired_by_port = {po["number"]: po for po in desired_overrides}
        all_port_nums   = sorted(set(list(current_by_port.keys()) + list(desired_by_port.keys())))

        new_overrides = []
        changes       = []
        for port_num in all_port_nums:
            desired = desired_by_port.get(port_num)
            current = current_by_port.get(port_num, {})

            if desired is not None:
                entry = {"port_idx": port_num}
                if desired.get("name"):
                    entry["name"] = desired["name"]
                    current_name = current.get("name", "")
                    if current_name != desired["name"]:
                        changes.append(f"port {port_num} name: {current_name!r} → {desired['name']!r}")
                if desired.get("portconf_id"):
                    entry["portconf_id"] = desired["portconf_id"]
                    current_id = current.get("portconf_id", "")
                    if current_id != desired["portconf_id"]:
                        changes.append(f"port {port_num} profile: {current_id or 'default'} → {desired['portconf_id']}")
                new_overrides.append(entry)
            else:
                # Non-managed port: preserve existing config using port_idx key.
                entry = {"port_idx": port_num}
                if current.get("name"):
                    entry["name"] = current["name"]
                if current.get("portconf_id"):
                    entry["portconf_id"] = current["portconf_id"]
                new_overrides.append(entry)

        if not changes:
            print(f"{sw_name}: all {len(desired_overrides)} managed port(s) already correct — no API call needed")
            continue

        print(f"{sw_name}: pushing {len(changes)} port override change(s):")
        for c in changes:
            print(f"  {c}")

        put_result = request(
            "PUT",
            f"/proxy/network/api/s/default/rest/device/{device_id}",
            {"port_overrides": new_overrides},
        )

        rc = put_result.get("meta", {}).get("rc", "?")
        if rc != "ok":
            print(f"ERROR: PUT failed for {sw_name} (rc={rc}): {put_result}", file=sys.stderr)
            sys.exit(1)

        print(f"SUCCESS: port overrides stored for {sw_name}")
        patched_macs.append(sw_mac)

    # Force-provision all patched switches so the controller pushes the new
    # desired config to the switch hardware immediately.
    if patched_macs:
        print(f"\nForce-provisioning {len(patched_macs)} switch(es) ...")
        for mac in patched_macs:
            prov_result = request(
                "POST",
                "/proxy/network/api/s/default/cmd/devmgr",
                {"cmd": "force-provision", "mac": mac},
            )
            rc = prov_result.get("meta", {}).get("rc", "?")
            print(f"  {mac}: force-provision rc={rc}")

    print("All switches patched successfully")


if __name__ == "__main__":
    main()
