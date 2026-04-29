#!/usr/bin/env python3
"""Patch UniFi port profile(s) to set tagged VLANs using the UniFi 10.x API.

The paultyng/unifi provider uses tagged_networkconf_ids (pre-10.x field)
which UniFi 10.x silently drops on POST. This script uses the 10.x approach:
  tagged_vlan_mgmt=custom + excluded_networkconf_ids (specify which VLANs to block)

excluded_networkconf_ids is computed dynamically by fetching ALL networks from
the controller (not just the ones managed by Terraform) and filtering to only
non-WAN networks. This ensures unmanaged VLANs (e.g. ISP/cell-tower VLANs
provisioned outside of Terraform) are also excluded from trunk ports.

All "customize" profiles are patched in a single authenticated session to avoid
the UDM login rate-limiter (429 / AUTHENTICATION_FAILED_LIMIT_REACHED) that
fires when multiple scripts authenticate back-to-back.

Environment variables (set by the null_resource local-exec):
  UNIFI_URL      - Base URL of the UniFi controller (e.g., https://192.168.2.1)
  UNIFI_USERNAME - Admin username
  UNIFI_PASSWORD - Admin password
  PROFILES_JSON  - JSON array of objects, each with:
                     profile_id    - UniFi port profile _id to patch
                     native_net_id - Network ID of the native (untagged) VLAN
                     tagged_net_ids - Comma-separated network IDs to allow tagged
"""

import os
import json
import base64
import ssl
import time
import urllib.request
import urllib.error
import sys


def main():
    unifi_url  = os.environ["UNIFI_URL"].rstrip("/")
    username   = os.environ["UNIFI_USERNAME"]
    password   = os.environ["UNIFI_PASSWORD"]
    profiles   = json.loads(os.environ["PROFILES_JSON"])

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    cookie_jar = {}

    def request(method, path, data=None, extra_headers=None, _retries=4, _backoff=60):
        url = f"{unifi_url}{path}"
        headers = {"Content-Type": "application/json"}
        if cookie_jar:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookie_jar.items())
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
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                if e.code == 429 and attempt < _retries - 1:
                    wait = _backoff * (2 ** attempt)
                    print(f"HTTP 429 from {method} {path} (attempt {attempt + 1}/{_retries}); retrying in {wait}s ...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"HTTP {e.code} from {method} {path}: {body}", file=sys.stderr)
                raise

    # Authenticate once for all profile patches.
    print(f"Authenticating to {unifi_url} ...")
    request("POST", "/api/auth/login", {"username": username, "password": password})

    token = cookie_jar.get("TOKEN", "")
    if not token:
        print("ERROR: No TOKEN cookie after auth", file=sys.stderr)
        sys.exit(1)

    parts = token.split(".")
    padded = parts[1] + "=" * (4 - len(parts[1]) % 4)
    csrf_token = json.loads(base64.b64decode(padded)).get("csrfToken", "")

    # Discover all excludable networks from the controller once. Two categories
    # are excluded from the candidate set:
    #   1. WAN networks — api.err.InvalidNetworkId if included.
    #   2. Untagged / native networks (no vlan field, or vlan <= 1) — these are
    #      the device management VLAN. UniFi returns api.err.NativeVlanCannotBeExcluded
    #      if a profile that excludes the device's native VLAN is applied to a port.
    print("Fetching all networks from controller ...")
    nets_result = request("GET", "/proxy/network/api/s/default/rest/networkconf")
    all_vlan_ids = {
        n["_id"]
        for n in nets_result["data"]
        if n.get("purpose") != "wan"
        and n.get("vlan") and int(n["vlan"]) > 1
    }

    # Patch each profile in the same session (no re-authentication).
    for entry in profiles:
        profile_id = entry["profile_id"]
        native_id  = entry.get("native_net_id", "")
        tagged_ids = {x for x in entry.get("tagged_net_ids", "").split(",") if x}

        keep_ids     = tagged_ids | ({native_id} if native_id else set())
        excluded_ids = sorted(all_vlan_ids - keep_ids)

        print(f"Fetching port profile {profile_id} ...")
        result  = request("GET", f"/proxy/network/api/s/default/rest/portconf/{profile_id}")
        profile = result["data"][0]

        profile["tagged_vlan_mgmt"] = "custom"
        profile["excluded_networkconf_ids"] = excluded_ids

        print(f"  native network : {native_id}")
        print(f"  tagged networks: {sorted(tagged_ids)}")
        print(f"  all vlan ids   : {sorted(all_vlan_ids)}")
        print(f"  excluded       : {excluded_ids}")

        print(f"Patching port profile {profile_id} ...")
        put_result = request(
            "PUT",
            f"/proxy/network/api/s/default/rest/portconf/{profile_id}",
            profile,
            extra_headers={"X-Csrf-Token": csrf_token},
        )

        if put_result.get("meta", {}).get("rc") != "ok":
            print(f"ERROR: PUT failed for {profile_id}: {put_result}", file=sys.stderr)
            sys.exit(1)

        print(f"SUCCESS: tagged VLAN config applied for {profile_id}")

    print("All profiles patched successfully")


if __name__ == "__main__":
    main()
