#!/usr/bin/env python3
"""Clear portconf_id from device port overrides before port profiles are deleted.

Called by the null_resource.pre_destroy_clear_overrides destroy-time provisioner
in the unifi-port-profile module. This unblocks `unifi_port_profile` DELETE calls,
which fail with api.err.ObjectReferredByDevice if any device port still has
portconf_id pointing to the profile being deleted.

Why this is needed:
  The paultyng/unifi provider uses forget_on_destroy=false on unifi_device resources,
  which removes the resource from state but makes no API call. Switch ports therefore
  still have portconf_id set on the controller after 'terragrunt destroy' of the
  device unit. This script clears those references so port profiles can be deleted.

Environment variables (set by null_resource.pre_destroy_clear_overrides triggers):
  UNIFI_URL        - Base URL of the UniFi controller
  UNIFI_USERNAME   - Admin username
  UNIFI_PASSWORD   - Admin password
  PORT_PROFILE_IDS - JSON object mapping profile key to profile ID (the ones being deleted)
"""

import os
import json
import ssl
import urllib.request
import urllib.error
import sys


def main():
    unifi_url   = os.environ["UNIFI_URL"].rstrip("/")
    username    = os.environ["UNIFI_USERNAME"]
    password    = os.environ["UNIFI_PASSWORD"]
    profile_ids = set(json.loads(os.environ["PORT_PROFILE_IDS"]).values())

    if not profile_ids:
        print("No managed profile IDs — nothing to clear")
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
        """Like request() but returns (body, err, csrf) — csrf from x-updated-csrf-token."""
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

    print(f"Clearing portconf_id for {len(profile_ids)} profile(s): {profile_ids}")
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

        # Find overrides that reference one of our managed profiles
        refs = [po for po in overrides if po.get("portconf_id", "") in profile_ids]
        if not refs:
            continue

        # Rebuild port_overrides: keep name + number, drop portconf_id for managed profiles.
        # Unmanaged portconf_id entries are preserved unchanged.
        # port_idx (stat field) → number (REST API field).
        updated = []
        for po in overrides:
            if po.get("portconf_id", "") in profile_ids:
                entry = {"number": po["port_idx"]}
                if po.get("name"):
                    entry["name"] = po["name"]
            else:
                entry = {"number": po["port_idx"]}
                if po.get("name"):
                    entry["name"] = po["name"]
                if po.get("portconf_id"):
                    entry["portconf_id"] = po["portconf_id"]
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
            print(f"  {device_name}: cleared portconf_id on {len(refs)} port(s)")
            cleared_total += len(refs)

    print(f"Done — cleared {cleared_total} port profile assignment(s) across all devices")


if __name__ == "__main__":
    main()
