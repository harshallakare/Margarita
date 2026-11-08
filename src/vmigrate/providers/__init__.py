"""Purpose: provider registry + platform support matrix. Providers are imported lazily so the core
never pulls a platform SDK; selecting a provider in config is the only thing that imports it.

Usage:
    src = build_source("ahv1", cfg.sources["ahv1"], ctx)
    tgt = build_target("vhi1", cfg.targets["vhi1"], ctx)
    for row in support_matrix(): print(row)

Adding a platform: implement the adapter under providers/<platform>/, then flip its row here
from (None, "planned") to ("vmigrate.providers.<platform>.<module>:<Class>", "implemented").
"""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from ..core.context import RunContext
from ..core.errors import ConfigError, ProviderNotImplemented
from ..core.interfaces import SourceProvider, TargetProvider

# type -> (import path or None, status, notes)
SOURCES: dict[str, tuple[str | None, str, str]] = {
    "fake": ("vmigrate.providers.fake.provider:FakeSource", "implemented", "in-memory, for tests/demos"),
    "nutanix": ("vmigrate.providers.nutanix.source:NutanixSource", "implemented",
                "Prism v3+v2 cross-check; iSCSI via PC v4 recovery points + volume groups, or NFS"),
    "vmware": ("vmigrate.providers.vmware.source:VmwareSource", "implemented",
               "pyVmomi inventory + snapshots; VDDK via nbdkit (or HTTPS flat-file); CBT for replicas"),
    "hyperv": ("vmigrate.providers.hyperv.source:HypervSource", "implemented",
               "WinRM/PowerShell + WMI recovery snapshots; VHDX/VHD read over SMB/CSV mounts; RCT opt-in"),
    "proxmox": ("vmigrate.providers.proxmox.source:ProxmoxSource", "implemented",
                "PVE REST + SSH/qemu-nbd snapshot reads (lvmthin, zfs, rbd, qcow2); rbd diff incremental"),
    "openstack": ("vmigrate.providers.openstack.source:OpenstackSource", "implemented",
                  "Nova createImage snapshots; Ceph RBD or attach-to-worker reads; rbd diff incremental"),
    "aws": ("vmigrate.providers.aws.source:AwsSource", "implemented",
            "CreateSnapshots (crash-consistent set) + EBS direct APIs; ListChangedBlocks incremental"),
    "physical": ("vmigrate.providers.physical.source:PhysicalSource", "implemented",
                 "P2V Linux over SSH: cold (rescue ISO) or live LVM-snapshot reads; unverified on real hardware"),
    "azure": (None, "planned", "managed-disk snapshot SAS export, page-range diff"),
}

TARGETS: dict[str, tuple[str | None, str, str]] = {
    "fake": ("vmigrate.providers.fake.provider:FakeTarget", "implemented", "file-staged, for tests/demos"),
    "vhi": ("vmigrate.providers.vhi.target:VhiTarget", "implemented",
            "Cinder VMDS volume, direct qcow2 backing write, hw_disk_bus=scsi"),
    "openstack": (None, "planned", "generic Cinder/Nova (non-VHI) — reuse vhi auth, upload via volume attach"),
    "proxmox": (None, "planned", "qm create + importdisk / direct storage write"),
    "nutanix": (None, "planned", "Prism v3/v4 VM + disk from image"),
    "vmware": (None, "planned", "CreateVM_Task + VDDK/ovftool, pvscsi"),
    "aws": (None, "planned", "PutSnapshotBlock -> register AMI"),
    "azure": (None, "planned", "managed disk page-blob upload -> VM"),
}


# Endpoint form schema per provider type, rendered by the web UI's "Add endpoint" form.
# Field: name, label, kind (text|url|password|bool|int|json|textarea), secret (-> credentials, encrypted),
# required, help, default. Keys must match what the adapter reads from its config.
FIELDS: dict[str, list[dict[str, Any]]] = {
    "nutanix": [
        {"name": "prism_element", "label": "Prism Element URL", "kind": "url", "required": True,
         "help": "https://<PE_IP>:9440 (v1/v2: PD snapshots, vm_disk_info)"},
        {"name": "prism_central", "label": "Prism Central URL", "kind": "url",
         "help": "optional; v3 calls go here (defaults to Prism Element)"},
        {"name": "username", "label": "Username", "kind": "text", "required": True},
        {"name": "password", "label": "Password", "kind": "password", "secret": True, "required": True},
        {"name": "verify_tls", "label": "Verify TLS", "kind": "bool", "default": True},
        {"name": "transport", "label": "Disk transport", "kind": "text", "default": "auto",
         "help": "auto (iSCSI via Prism Central v4 when available, else NFS), iscsi, or nfs"},
        {"name": "protection_domain", "label": "Protection domain (NFS transport only)", "kind": "text",
         "help": "not needed for iSCSI (PC >= 7.3 / AOS >= 6.8); required for the NFS transport"},
        {"name": "protect_vm_if_missing", "label": "Add VM to PD if missing", "kind": "bool", "default": False},
        {"name": "nfs_host", "label": "NFS address (optional)", "kind": "text",
         "help": "leave empty: workers mount containers from the Prism Element host automatically "
                 "(whitelist the worker IP on the cluster once)"},
        {"name": "nfs_root", "label": "Pre-mounted root (optional)", "kind": "text",
         "help": "only if you mount containers yourself: <nfs_root>/<container>"},
        {"name": "crt", "label": "Change tracking for replicas (CRT)", "kind": "bool", "default": True,
         "help": "replica syncs read only changed regions (iSCSI transport); falls back to compare on any error"},
        {"name": "crt_base_keep_hours", "label": "Keep CRT base recovery point (hours)", "kind": "int", "default": 336,
         "help": "must be longer than the time between syncs, or the next sync falls back to compare"},
    ],
    "vhi": [
        {"name": "auth_url", "label": "Keystone URL", "kind": "url", "required": True,
         "help": "https://<VHI_ADMIN_VIP>:5000/v3"},
        {"name": "username", "label": "Username", "kind": "text", "required": True},
        {"name": "password", "label": "Password", "kind": "password", "secret": True, "required": True},
        {"name": "user_domain_name", "label": "User domain", "kind": "text", "default": "Default"},
        {"name": "project_id", "label": "Project ID", "kind": "text",
         "help": "the project's ID - or leave empty and fill Project name + Project domain"},
        {"name": "project_name", "label": "Project name", "kind": "text",
         "help": "used only when Project ID is empty"},
        {"name": "project_domain_name", "label": "Project domain", "kind": "text",
         "help": "domain of the project (defaults to the user domain)"},
        {"name": "verify_tls", "label": "Verify TLS", "kind": "bool", "default": True},
        {"name": "volume_type", "label": "Volume type (optional)", "kind": "text", "resource": "storage",
         "help": "leave empty to use the cluster's default volume type"},
        {"name": "multiattach_volume_type", "label": "Multi-attach volume type", "kind": "text", "resource": "storage",
         "help": "optional: Cinder type with multiattach=\"<is> True\", for cluster shared disks"},
        {"name": "write_mode", "label": "Disk write mode", "kind": "select", "default": "auto",
         "options": ["auto", "helper_vm", "vstorage"],
         "help": "helper_vm: a temporary VM in VHI writes the disks (worker can run anywhere, like Coriolis); "
                 "vstorage: worker runs on a VHI node; auto: vstorage if mounted here, else helper_vm"},
        {"name": "helper_image", "label": "Helper VM image", "kind": "text", "resource": "images",
         "help": "helper_vm: Linux cloud image with python3 + cloud-init (e.g. Ubuntu 22.04 / Rocky 9)"},
        {"name": "helper_network", "label": "Helper VM network", "kind": "text", "resource": "networks",
         "help": "helper_vm: network for the helper; the worker must reach it over SSH (port 22)"},
        {"name": "helper_floating_network", "label": "Helper floating-IP network", "kind": "text",
         "resource": "external_networks",
         "help": "helper_vm, optional: allocate a floating IP from this external network"},
        {"name": "helper_flavor", "label": "Helper VM flavor", "kind": "text", "resource": "flavors",
         "help": "optional: default is the smallest flavor with 2 vCPU / 2 GiB"},
        {"name": "helper_security_groups", "label": "Helper security group", "kind": "text", "default": "default",
         "resource": "security_groups",
         "help": "the helper VM uses this existing group (default: the project's 'default'); it must allow "
                 "SSH (TCP 22) from the Margarita worker. Nothing is created."},
        {"name": "vstorage_mount", "label": "vStorage mount", "kind": "text", "default": "/mnt/vstorage",
         "help": "vstorage mode only"},
        {"name": "staging_dir", "label": "Staging directory", "kind": "text", "default": "/var/lib/vmigrate/staging",
         "help": "local work area (staged images in vstorage mode; helper keys in helper_vm mode)"},
        {"name": "availability_zone", "label": "Availability zone", "kind": "text"},
    ],
    "vmware": [
        {"name": "host", "label": "vCenter / ESXi host", "kind": "text", "required": True,
         "help": "vcenter.example.com (vCenter recommended)"},
        {"name": "username", "label": "Username", "kind": "text", "required": True,
         "help": "a role with VM inventory read, snapshot create/remove and VirtualMachine.Provisioning.DiskRandomRead"},
        {"name": "password", "label": "Password", "kind": "password", "secret": True, "required": True},
        {"name": "verify_tls", "label": "Verify TLS", "kind": "bool", "default": True},
        {"name": "transport", "label": "Disk transport", "kind": "select", "default": "auto",
         "options": ["auto", "vddk", "openvix", "http"],
         "help": "vddk (nbdkit + VDDK), openvix (OpenVixDiskLib over NFC, vSphere 8, no VDDK), http (flat VMDKs "
                 "without prior snapshots), or auto"},
        {"name": "vddk_libdir", "label": "VDDK directory", "kind": "text",
         "help": "unpacked VDDK (contains lib64/libvixDiskLib.so); download from Broadcom"},
        {"name": "vddk_transports", "label": "VDDK transports", "kind": "text", "default": "nbdssl:nbd",
         "help": "add hotadd only when the worker is a VM on the same vSphere"},
        {"name": "thumbprint", "label": "TLS thumbprint (SHA-1)", "kind": "text",
         "help": "optional pin AA:BB:...; required when Verify TLS is off"},
    ],
    "hyperv": [
        {"name": "host", "label": "Hyper-V host", "kind": "text", "required": True,
         "help": "the host (or cluster node) that owns the VM"},
        {"name": "username", "label": "Username", "kind": "text", "required": True,
         "help": "DOMAIN\\user in Hyper-V Administrators (or local Administrators)"},
        {"name": "password", "label": "Password", "kind": "password", "secret": True, "required": True},
        {"name": "winrm_transport", "label": "WinRM auth", "kind": "text", "default": "ntlm",
         "help": "ntlm, kerberos or credssp"},
        {"name": "port", "label": "WinRM port", "kind": "int", "default": 5986},
        {"name": "use_ssl", "label": "WinRM over HTTPS", "kind": "bool", "default": True},
        {"name": "verify_tls", "label": "Verify TLS", "kind": "bool", "default": True},
        {"name": "share_map", "label": "Storage share map", "kind": "json",
         "help": '{"C:\\ClusterStorage\\Volume1": "/mnt/hv/csv1"}: where this worker mounts the VM storage'},
        {"name": "rct", "label": "RCT incremental (experimental)", "kind": "bool", "default": False,
         "help": "replica syncs read only changed blocks; unverified, falls back to compare on any error"},
        {"name": "rct_service_url", "label": "rct-service URL (optional)", "kind": "url",
         "help": "https://<host>:6677 — Cloudbase rct-service on the Hyper-V host: reads + RCT over HTTPS, no share_map"},
        {"name": "rct_service_auth_key", "label": "rct-service auth key", "kind": "password", "secret": True},
        {"name": "rct_service_fingerprint", "label": "rct-service cert SHA-256", "kind": "text",
         "help": "pins the service's self-signed certificate (hex)"},
    ],
    "physical": [
        {"name": "host", "label": "Server address", "kind": "text", "required": True, "help": "IP or DNS name"},
        {"name": "mode", "label": "Read mode", "kind": "select", "options": ["cold", "lvm-snapshot"], "default": "cold",
         "help": "cold: server booted from a rescue ISO; lvm-snapshot: live, root and data on LVM"},
        {"name": "ssh_user", "label": "SSH user", "kind": "text", "default": "root",
         "help": "root, or a user with passwordless sudo (python3, dd, lvcreate, lvremove, mount)"},
        {"name": "ssh_private_key", "label": "SSH private key", "kind": "textarea", "secret": True, "required": True,
         "help": "OpenSSH private key (PEM); stored encrypted"},
        {"name": "ssh_known_hosts", "label": "Pinned host key", "kind": "textarea", "required": True,
         "help": "known_hosts line (ssh-keyscan output verified on the server console)"},
        {"name": "ssh_port", "label": "SSH port", "kind": "int", "default": 22},
        {"name": "cow_percent", "label": "Snapshot space %", "kind": "int", "default": 10,
         "help": "lvm-snapshot: copy-on-write space per LV (min 1 GiB); needs that much free space in the VG"},
        {"name": "remount_ro", "label": "Remount plain partitions read-only", "kind": "bool", "default": True,
         "help": "lvm-snapshot: /boot etc. are read-only while copying"},
    ],
    "proxmox": [
        {"name": "url", "label": "API URL", "kind": "url", "required": True, "help": "https://pve1.example.com:8006"},
        {"name": "token_id", "label": "API token ID", "kind": "text", "required": True,
         "help": "user@realm!tokenname with VM.Audit, VM.Snapshot, Datastore.Audit, Sys.Audit"},
        {"name": "token_secret", "label": "API token secret", "kind": "password", "secret": True, "required": True},
        {"name": "verify_tls", "label": "Verify TLS", "kind": "bool", "default": True},
        {"name": "ssh_user", "label": "SSH user", "kind": "text", "default": "root",
         "help": "root, or a user with passwordless sudo for pvesm/qemu-nbd/lvchange/zfs/rbd"},
        {"name": "ssh_private_key", "label": "SSH private key", "kind": "textarea", "secret": True, "required": True,
         "help": "OpenSSH private key (PEM); stored encrypted"},
        {"name": "ssh_known_hosts", "label": "Pinned host keys", "kind": "textarea", "required": True,
         "help": "known_hosts lines for every node (ssh-keyscan output verified on the node console)"},
        {"name": "ssh_port", "label": "SSH port", "kind": "int", "default": 22},
        {"name": "node_addresses", "label": "Node addresses", "kind": "json",
         "help": '{"pve1": "10.0.0.11"}; default: node names resolve via DNS'},
    ],
    "openstack": [
        {"name": "auth_url", "label": "Keystone URL", "kind": "url", "required": True},
        {"name": "username", "label": "Username", "kind": "text", "required": True},
        {"name": "password", "label": "Password", "kind": "password", "secret": True, "required": True},
        {"name": "user_domain_name", "label": "User domain", "kind": "text", "default": "Default"},
        {"name": "project_id", "label": "Project ID", "kind": "text",
         "help": "the project's ID - or leave empty and fill Project name + Project domain"},
        {"name": "project_name", "label": "Project name", "kind": "text",
         "help": "used only when Project ID is empty"},
        {"name": "project_domain_name", "label": "Project domain", "kind": "text",
         "help": "domain of the project (defaults to the user domain)"},
        {"name": "region_name", "label": "Region", "kind": "text"},
        {"name": "verify_tls", "label": "Verify TLS", "kind": "bool", "default": True},
        {"name": "transport", "label": "Disk transport", "kind": "text", "default": "rbd",
         "help": "rbd (Ceph-backed cloud) or attach (this worker runs as an instance in the same cloud)"},
        {"name": "ceph_access", "label": "Ceph access", "kind": "text", "default": "local",
         "help": "local (worker has ceph.conf + keyring) or ssh (through ceph_host)"},
        {"name": "ceph_conf", "label": "ceph.conf path", "kind": "text", "default": "/etc/ceph/ceph.conf"},
        {"name": "ceph_user", "label": "Ceph client id", "kind": "text", "default": "cinder"},
        {"name": "ceph_keyring", "label": "Ceph keyring path", "kind": "text"},
        {"name": "cinder_pool", "label": "Cinder RBD pool", "kind": "text", "default": "volumes"},
        {"name": "ceph_host", "label": "Ceph host (ssh access)", "kind": "text"},
        {"name": "ssh_user", "label": "SSH user", "kind": "text", "default": "root"},
        {"name": "ssh_private_key", "label": "SSH private key", "kind": "textarea", "secret": True},
        {"name": "ssh_known_hosts", "label": "Pinned host keys", "kind": "textarea"},
        {"name": "worker_server_id", "label": "Worker server ID (attach)", "kind": "text",
         "help": "default: read from the metadata service"},
    ],
    "aws": [
        {"name": "region", "label": "Region", "kind": "text", "required": True, "help": "e.g. eu-west-1"},
        {"name": "access_key_id", "label": "Access key ID", "kind": "password", "secret": True,
         "help": "leave empty to use the worker's own IAM role"},
        {"name": "secret_access_key", "label": "Secret access key", "kind": "password", "secret": True},
        {"name": "role_arn", "label": "Role to assume", "kind": "text",
         "help": "optional: arn:aws:iam::<account>:role/<name> in the source account"},
        {"name": "external_id", "label": "External ID", "kind": "password", "secret": True},
        {"name": "ignore_instance_store", "label": "Ignore instance store", "kind": "bool", "default": False,
         "help": "only if instance-store disks hold scratch data: EBS snapshots never include them"},
        {"name": "read_workers", "label": "Parallel block reads per disk", "kind": "int", "default": 16},
    ],
    "fake": [
        {"name": "vms", "label": "VMs", "kind": "json", "help": "source side: [{name, os, disks: [{size, boot}]}]"},
        {"name": "secondary_enumeration_cap", "label": "Secondary enumeration cap", "kind": "int",
         "help": "set to 10 to reproduce the Coriolis bug"},
        {"name": "cbt", "label": "Change tracking (CBT)", "kind": "bool", "default": False,
         "help": "replicas read only changed regions; off = full read + compare"},
        {"name": "generation", "label": "Change generation", "kind": "int",
         "help": "raise it to simulate guest writes between replica syncs (~1/8 of blocks per step)"},
        {"name": "staging_dir", "label": "Staging directory", "kind": "text", "help": "target side"},
        {"name": "persist", "label": "Persist registry", "kind": "bool", "default": True},
    ],
}


def _load(table: dict[str, tuple[str | None, str, str]], kind: str, type_name: str) -> type:
    if type_name not in table:
        raise ConfigError(f"unknown {kind} provider type {type_name!r}; known: {sorted(table)}")
    path, status, notes = table[type_name]
    if path is None:
        raise ProviderNotImplemented(f"{kind} provider {type_name!r} is {status} ({notes}) — not implemented yet")
    mod, cls = path.split(":")
    try:
        return getattr(importlib.import_module(mod), cls)
    except ImportError as e:
        raise ConfigError(
            f"{kind} provider {type_name!r} needs extra dependencies: pip install 'vmigrate[{type_name}]' ({e})"
        ) from e


def build_source(name: str, cfg: Mapping[str, Any], ctx: RunContext) -> SourceProvider:
    cls = _load(SOURCES, "source", cfg["type"])
    return cls(name, cfg, ctx)


def build_target(name: str, cfg: Mapping[str, Any], ctx: RunContext) -> TargetProvider:
    cls = _load(TARGETS, "target", cfg["type"])
    return cls(name, cfg, ctx)


def support_matrix() -> list[tuple[str, str, str, str]]:
    rows = [("source", k, v[1], v[2]) for k, v in SOURCES.items() if k != "fake"]
    rows += [("target", k, v[1], v[2]) for k, v in TARGETS.items() if k != "fake"]
    return rows
