# Network Plan

## VLANs

| ID | Name | Subnet | Purpose |
|----|------|--------|---------|
| 10 | Cloud-Public | 10.0.10.0/24 | Default VM/host network; DHCP from UniFi |
| 11 | Management | 10.0.11.0/24 | AMT out-of-band (static IPs only, no DHCP) |
| 12 | Provisioning | 10.0.12.0/24 | PXE boot; DHCP managed by MaaS rack controller (10.0.12.2) |
| 13 | Guest | 10.0.13.0/24 | Internet-only guest isolation |
| 14 | Storage | 10.0.14.0/24 | NFS/Ceph/iSCSI storage traffic; DHCP configured |

## Node Types and Network Connectivity

### Proxmox Hypervisor (pve-1)
- Pre-installed HP Z4 workstation at 10.0.10.115; not PXE-booted or managed by MaaS.
- Connects on VLAN10 (cloud-public) via a **`hypervisor_trunk`** port; specific switch port is not tracked in the UniFi YAML config.

### Physical Servers — minisforum-ms01 (×3)
- PXE-booted and managed by MaaS.
- Each has: 2× 2.5Gbps NICs (port3: AMT/management, port4: PXE/data), 2× 10Gbps NICs.
- **Port 3** (AMT NIC) → USW-Flex-2.5G-8 ports 1/2/3: **`amt_mgmt`** — native=VLAN11, no tagged. Static AMT IPs only; no DHCP on this VLAN.
- **Port 4** (PXE/data NIC) → USW-Flex-2.5G-8 ports 4/5/6: **`pxe_provisioning`** — native=VLAN12, no tagged. MaaS DHCP serves this port for PXE; post-deploy the OS adds tagged sub-interfaces for VLAN10 etc.
- 10Gbps NICs reserved for storage (future).

### Proxmox VMs
- Standard VMs: VLAN10 (cloud-public DHCP).
- MaaS servers: `maas-region-1` (VLAN10, 10.0.10.11), `maas-db-1` (VLAN10, 10.0.10.12), `maas-rack-1` (VLAN10 10.0.10.13 + VLAN12 10.0.12.2).
- PXE test VM: VLAN12 only (deployed by MaaS).

## Port Profiles

| Profile | Native VLAN | Tagged VLANs | Used By |
|---------|-------------|--------------|---------|
| `hypervisor_trunk` | VLAN10 (Cloud-Public) | VLAN11, VLAN12, VLAN14 | Proxmox hosts, developer machines |
| `amt_mgmt` | VLAN11 (Management) | — | ms01 AMT/management NICs (port3) |
| `pxe_provisioning` | VLAN12 (Provisioning) | — | ms01 PXE/data NICs (port4); MaaS DHCP for PXE boot |
| `pxe_provisioning_public` | VLAN12 (Provisioning) | VLAN10 | Single-NIC machines that need PXE boot + cloud-public sub-interface |
| `public_only` | VLAN10 (Cloud-Public) | — | Single-VLAN access |
| `storage_only` | VLAN14 (Storage) | — | Dedicated storage NICs |
| `pxe_mgmt_public` | VLAN11 (Management) | VLAN10 | Legacy profile; kept for reference |

**Design note:** `pxe_provisioning` carries no tagged VLANs because ms01s have separate dedicated NICs for AMT (port3) and PXE/data (port4). Post-deploy, the OS on port4 adds VLAN sub-interfaces directly. Single-NIC machines (if any) use `pxe_provisioning_public` to get cloud-public access via a tagged sub-interface after PXE boot.

## Switch Port Assignments (USW-Flex-2.5G-8)

All ms01 NICs connect here. Beast-PC and spare ports also on this switch.

| Port | Name | Profile | Notes |
|------|------|---------|-------|
| 1 | ms01-01-port3 | amt_mgmt | ms01-01 AMT NIC; MAC 38:05:25:31:2f:a3 |
| 2 | ms01-02-port3 | amt_mgmt | ms01-02 AMT NIC; MAC 38:05:25:31:81:11 |
| 3 | ms01-03-port3 | amt_mgmt | ms01-03 AMT NIC; MAC 38:05:25:31:7f:15 |
| 4 | ms01-01-port4 | pxe_provisioning | ms01-01 PXE NIC; MAC 38:05:25:31:2f:a2 |
| 5 | ms01-02-port4 | pxe_provisioning | ms01-02 PXE NIC; MAC 38:05:25:31:81:10 |
| 6 | ms01-03-port4 | pxe_provisioning | ms01-03 PXE NIC; MAC 38:05:25:31:7f:14 |
| 7 | Beast-PC | hypervisor_trunk | |
| 8–9 | Port-8/9 | hypervisor_trunk | Spare |
| 10 | uplink-2.5g | — | Uplink to USW-Pro-Max-16 port 17 |

## Switch Port Assignments (USW-Pro-Max-16)

| Port | Name | Profile | Notes |
|------|------|---------|-------|
| 1 | uplink-gateway-UDM | — | Uplink to UDM |
| 11 | System76-laptop | hypervisor_trunk | |
| 17 | link-2.5G-to-promax | — | Downlink to USW-Flex-2.5G-8 |
| 18 | link-10G-to-mikrotik-crs-317 | hypervisor_trunk | 10Gbps uplink to MikroTik CRS317 |
